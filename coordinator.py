#!/usr/bin/env python3
"""
RenderHive - Coordinator
========================
HTTP API server and job coordinator. Every RenderHive node runs one.
- Hosts the web dashboard
- Accepts render jobs (.blend files)
- Hands out frames to workers on a pull basis
- Collects finished frames, tracks progress, requeues dead frames
- Integrates with peer discovery for automatic LAN detection
"""

import os
import io
import re
import json
import time
import uuid
import glob
import queue as _queue
import socket
import shutil
import platform
import statistics
import threading
import argparse
import subprocess
from pathlib import Path

from flask import (
    Flask, request, jsonify, send_file, send_from_directory, abort, Response
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR     = Path(__file__).resolve().parent
DATA_DIR     = BASE_DIR / "farm_data"
BLEND_DIR    = DATA_DIR / "blends"
OUTPUT_DIR   = DATA_DIR / "output"
STATE_FILE   = DATA_DIR / "state.json"

WORKER_TIMEOUT     = 120
FRAME_TIMEOUT      = 1800
MAX_FRAME_ATTEMPTS = 3
PREFETCH_THRESHOLD = 75   # % progress that triggers prefetch
PREFETCH_DEADLINE  = 90   # seconds before stale prefetch is reclaimed

for d in (DATA_DIR, BLEND_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # 8 GB

LOCK = threading.Lock()
JOBS = {}
WORKERS = {}
WORKER_SSE_QUEUES:     dict = {}  # wid -> queue.Queue (connected idle workers)
DASHBOARD_SSE_CLIENTS: list = []  # list of queue.Queue (connected dashboard tabs)

# Set by renderhive.py before app starts
DISCOVERY = None      # PeerDiscovery instance
BLENDER_PATH = None   # path to blender executable


# ---------------------------------------------------------------------------
# Blender detection (for probing blend files)
# ---------------------------------------------------------------------------
BLENDER_50_PATH = r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"


def detect_blender():
    """Return the path to Blender 5.0. Always prefers Blender 5.0."""
    # 1. Explicit override via environment variable
    env = os.environ.get("BLENDER")
    if env and Path(env).exists():
        return env
    # 2. Blender 5.0 at the standard install location (preferred)
    if Path(BLENDER_50_PATH).exists():
        return BLENDER_50_PATH
    # 3. Fallback: search PATH (but warn if it's not 5.0)
    found = shutil.which("blender")
    if found:
        print(f"  WARNING: Blender 5.0 not found at {BLENDER_50_PATH}")
        print(f"           Using fallback: {found}")
        return found
    return None


def probe_blend_file(blend_path):
    """Run Blender in background mode to extract scene settings."""
    blender = BLENDER_PATH or detect_blender()
    if not blender:
        return None
    # Write probe script to a temp file (--python-expr has quoting issues on Windows)
    script_content = (
        "import bpy, json, sys\n"
        "s = bpy.context.scene\n"
        "info = {\n"
        "    'frame_start': s.frame_start,\n"
        "    'frame_end': s.frame_end,\n"
        "    'frame_step': s.frame_step,\n"
        "    'engine': s.render.engine,\n"
        "    'format': s.render.image_settings.file_format,\n"
        "    'resolution_x': s.render.resolution_x,\n"
        "    'resolution_y': s.render.resolution_y,\n"
        "}\n"
        "if s.render.engine == 'CYCLES':\n"
        "    info['samples'] = s.cycles.samples\n"
        "else:\n"
        "    info['samples'] = 0\n"
        "print('RENDERHIVE_PROBE:' + json.dumps(info))\n"
    )
    script_path = DATA_DIR / "_probe_script.py"
    script_path.write_text(script_content, encoding="utf-8")
    try:
        # Use --factory-startup to skip loading user prefs/GPU drivers (much faster)
        cmd = [blender, "-b", "--factory-startup", str(blend_path), "-P", str(script_path)]
        result = subprocess.run(
            cmd, capture_output=True, timeout=60,
            encoding="utf-8", errors="replace"
        )
        output = result.stdout + "\n" + result.stderr
        for line in output.splitlines():
            if line.startswith("RENDERHIVE_PROBE:"):
                return json.loads(line[len("RENDERHIVE_PROBE:"):])
        print(f"  probe: no RENDERHIVE_PROBE marker found in Blender output")
        # Print last few lines for debugging
        for line in (result.stdout + result.stderr).strip().splitlines()[-5:]:
            print(f"  probe output: {line}")
    except subprocess.TimeoutExpired:
        print(f"  probe error: Blender timed out after 60s")
    except Exception as e:
        print(f"  probe error: {e}")
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"jobs": JOBS}, f)
    except Exception as e:
        print("warn: could not save state:", e)


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.load(open(STATE_FILE))
            JOBS.update(data.get("jobs", {}))
            for job in JOBS.values():
                for fr in job["frames"].values():
                    if fr["status"] == "assigned":
                        fr["status"] = "pending"
                        fr["worker"] = None
                        fr.pop("prefetch_deadline", None)
                if job["status"] == "rendering":
                    job["status"] = "queued"
        except Exception as e:
            print("warn: could not load state:", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
EXT_FOR_FORMAT = {
    "PNG": "png", "JPEG": "jpg", "OPEN_EXR": "exr",
    "OPEN_EXR_MULTILAYER": "exr", "TIFF": "tif", "WEBP": "webp",
}


def _worker_avg_speed(w):
    """Mean of last 10 render times, or None if no history."""
    times = w.get("render_times", [])[-10:]
    return sum(times) / len(times) if times else None


def _prefetch_count(w):
    """Number of frames to pre-assign: 1–3 based on worker speed tier."""
    avg = _worker_avg_speed(w)
    if not avg or avg <= 0:
        return 1
    return max(1, min(3, int(PREFETCH_DEADLINE / avg)))


def _push_worker_event(wid, event):
    """Push an SSE event to a connected worker (safe to call inside LOCK)."""
    q = WORKER_SSE_QUEUES.get(wid)
    if q:
        try:
            q.put_nowait(event)
        except _queue.Full:
            pass


def _broadcast_dashboard(event):
    """Push an SSE event to all connected dashboard tabs (safe inside LOCK)."""
    for q in list(DASHBOARD_SSE_CLIENTS):
        try:
            q.put_nowait(event)
        except _queue.Full:
            pass


def _farm_fps():
    """Combined frames/sec of all rendering workers (call inside LOCK)."""
    fps = 0.0
    for w in WORKERS.values():
        if w["status"] == "rendering":
            avg = _worker_avg_speed(w)
            if avg and avg > 0:
                fps += 1.0 / avg
    return fps if fps > 0 else None


def _try_prefetch_frames(wid):
    """Pre-assign 0–N pending frames based on worker speed. Must be called inside LOCK."""
    w = WORKERS.get(wid)
    if not w:
        return []
    already = sum(
        1 for j in JOBS.values() for f in j["frames"].values()
        if f.get("worker") == wid and f.get("prefetch_deadline") and f["status"] == "assigned"
    )
    n_want = _prefetch_count(w) - already
    if n_want <= 0:
        return []
    remaining_pending = sum(
        1 for j in JOBS.values()
        if j["status"] not in ("cancelled", "done")
        for f in j["frames"].values() if f["status"] == "pending"
    )
    active_workers = sum(1 for w2 in WORKERS.values() if w2["status"] == "rendering")
    if remaining_pending <= active_workers:
        return []
    assignments = []
    now = time.time()
    for job in sorted(JOBS.values(), key=lambda x: x["created_at"]):
        if job["status"] in ("cancelled", "done"):
            continue
        for k, fr in sorted(job["frames"].items(), key=lambda kv: int(kv[0])):
            if fr["status"] != "pending":
                continue
            fr.update({
                "status": "assigned", "worker": wid,
                "started_at": now, "progress": 0,
                "attempts": fr.get("attempts", 0) + 1,
                "prefetch_deadline": now + PREFETCH_DEADLINE,
            })
            assignments.append({
                "job_id": job["id"], "frame": int(k),
                "blend_filename": job["blend_filename"],
                "shared_path": job.get("shared_path"),
                "blend_url": f"/api/jobs/{job['id']}/blend",
                "engine": job["engine"], "device": job["device"],
                "samples": job["samples"], "format": job["format"],
            })
            n_want -= 1
            if n_want <= 0:
                break
        if n_want <= 0:
            break
    return assignments


def job_summary(job):
    frames = job["frames"]
    total = len(frames)
    done = sum(1 for f in frames.values() if f["status"] == "done")
    failed = sum(1 for f in frames.values() if f["status"] == "failed")
    rendering = sum(1 for f in frames.values() if f["status"] == "assigned")
    times = [f["render_time"] for f in frames.values()
             if f["status"] == "done" and f.get("render_time")]
    avg = sum(times) / len(times) if times else None
    remaining = total - done - failed
    # Farm-fps ETA: more accurate than naive per-worker average
    fps = _farm_fps()
    if fps and fps > 0 and remaining > 0:
        eta = remaining / fps
    elif avg and remaining:
        active = max(1, sum(1 for w in WORKERS.values() if w["status"] == "rendering"))
        eta = avg * remaining / active
    else:
        eta = None
    # ETA confidence based on coefficient of variation of recent render times
    all_times = [t for w in WORKERS.values() for t in w.get("render_times", [])[-10:]]
    if len(all_times) >= 3:
        mean_t = statistics.mean(all_times)
        stdev_t = statistics.stdev(all_times)
        cv = stdev_t / mean_t if mean_t > 0 else 1.0
        confidence = max(0, min(99, int(100 * (1 - cv))))
    else:
        confidence = None
    return {
        "id": job["id"], "name": job["name"], "status": job["status"],
        "blend_filename": job["blend_filename"],
        "frame_start": job["frame_start"], "frame_end": job["frame_end"],
        "frame_step": job["frame_step"],
        "engine": job["engine"] or "(from file)",
        "device": job["device"] or "(from file)",
        "samples": job["samples"] or "(from file)",
        "total": total, "done": done, "failed": failed, "rendering": rendering,
        "avg_render_time": round(avg, 1) if avg else None,
        "eta_seconds": round(eta) if eta else None,
        "eta_confidence": confidence,
        "farm_fps": round(fps, 4) if fps else None,
        "created_at": job["created_at"],
        "shared_path": job.get("shared_path"),
    }


def reap_dead_workers_and_frames():
    while True:
        time.sleep(10)
        now = time.time()
        with LOCK:
            for w in WORKERS.values():
                if w["status"] != "offline" and now - w["last_seen"] > WORKER_TIMEOUT:
                    w["status"] = "offline"
                    w["current_job"] = None
                    w["current_frame"] = None
                    w["progress"] = 0
            for job in JOBS.values():
                if job["status"] not in ("rendering", "queued"):
                    continue
                for fno, fr in job["frames"].items():
                    if fr["status"] != "assigned":
                        continue
                    worker = WORKERS.get(fr["worker"])
                    stale_worker = (worker is None or worker["status"] == "offline")
                    # Prefetch frames use a short deadline; normal frames use FRAME_TIMEOUT
                    deadline = fr.get("prefetch_deadline")
                    if deadline is not None:
                        stale_frame = now > deadline
                        if stale_frame and fr.get("progress", 0) == 0:
                            fr["attempts"] = max(0, fr.get("attempts", 1) - 1)
                        fr.pop("prefetch_deadline", None)
                    else:
                        stale_frame = now - fr.get("started_at", now) > FRAME_TIMEOUT
                    if stale_worker or stale_frame:
                        if fr.get("attempts", 0) >= MAX_FRAME_ATTEMPTS:
                            fr["status"] = "failed"
                        else:
                            fr["status"] = "pending"
                        fr["worker"] = None
                        fr["progress"] = 0
            # Work stealing: reclaim low-progress frames from abnormally slow workers
            # Only when at least one idle worker is waiting for work
            idle_workers = [w for w in WORKERS.values()
                            if w["status"] == "idle" and now - w["last_seen"] < WORKER_TIMEOUT]
            if idle_workers:
                all_times = [t for w in WORKERS.values()
                             for t in w.get("render_times", [])[-10:] if w.get("render_times")]
                if all_times:
                    farm_avg = statistics.mean(all_times)
                    steal_after = max(farm_avg * 3, 180)
                    for job in JOBS.values():
                        if job["status"] not in ("rendering", "queued"):
                            continue
                        for fno, fr in job["frames"].items():
                            if fr["status"] != "assigned" or fr.get("progress", 0) >= 5:
                                continue
                            if now - fr.get("started_at", now) < steal_after:
                                continue
                            assigned_w = WORKERS.get(fr.get("worker"))
                            if not assigned_w:
                                continue
                            w_avg = _worker_avg_speed(assigned_w)
                            if w_avg and w_avg > farm_avg * 1.5:
                                fr["status"] = "pending"
                                fr["worker"] = None
                                fr["progress"] = 0
                                fr.pop("prefetch_deadline", None)
                                print(f"  [STEAL] frame {fno} reclaimed "
                                      f"(worker avg {round(w_avg)}s vs farm avg {round(farm_avg)}s)")
                                for iw in idle_workers:
                                    _push_worker_event(iw["id"], {"type": "work_available"})
            _recompute_job_statuses()
        save_state()


def _recompute_job_statuses():
    for job in JOBS.values():
        if job["status"] == "cancelled":
            continue
        statuses = [f["status"] for f in job["frames"].values()]
        if all(s in ("done", "failed") for s in statuses):
            job["status"] = "failed" if "failed" in statuses else "done"
        elif any(s == "assigned" for s in statuses):
            job["status"] = "rendering"
        else:
            job["status"] = "queued"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return send_from_directory(BASE_DIR, "dashboard.html")


# ---------------------------------------------------------------------------
# Status API (enhanced)
# ---------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    now = time.time()
    with LOCK:
        workers = []
        hostname_stats = {}  # hostname -> {workers_online, jobs_active}
        for w in sorted(WORKERS.values(), key=lambda x: x["name"]):
            offline = now - w["last_seen"] > WORKER_TIMEOUT
            # compute avg render time for this worker
            rtimes = w.get("render_times", [])
            avg_rt = round(sum(rtimes) / len(rtimes), 1) if rtimes else None
            total_rt = round(sum(rtimes), 1) if rtimes else 0
            # find current job name
            cjob = JOBS.get(w.get("current_job"))
            cjob_name = cjob["name"] if cjob else None
            workers.append({
                "id": w["id"], "name": w["name"],
                "hostname": w.get("hostname", "?"),
                "gpu_name": w.get("gpu_name", "?"),
                "os": w.get("os", "?"),
                "blender_version": w.get("blender_version", "?"),
                "status": "offline" if offline else w["status"],
                "current_job": w["current_job"],
                "current_job_name": cjob_name,
                "current_frame": w["current_frame"],
                "progress": w["progress"],
                "frames_done": w["frames_done"],
                "last_seen_ago": round(now - w["last_seen"]),
                "avg_render_time": avg_rt,
                "total_render_time": total_rt,
            })
            # Accumulate per-hostname stats for peer card enrichment
            h = w.get("hostname", "?")
            if h not in hostname_stats:
                hostname_stats[h] = {"workers_online": 0, "jobs_active": set()}
            if not offline:
                hostname_stats[h]["workers_online"] += 1
            if w.get("current_job") and not offline:
                hostname_stats[h]["jobs_active"].add(w["current_job"])
        jobs = [job_summary(j) for j in
                sorted(JOBS.values(), key=lambda x: x["created_at"], reverse=True)]

    # peers from discovery
    peers = []
    if DISCOVERY:
        for p in DISCOVERY.get_peers():
            pstats = hostname_stats.get(p["name"], {})
            peers.append({
                "node_id": p["node_id"],
                "ip": p["ip"],
                "port": p["port"],
                "name": p["name"],
                "last_seen_ago": round(now - p["last_seen"]),
                "workers_active": pstats.get("workers_online", 0),
                "jobs_active": len(pstats.get("jobs_active", set())),
            })

    node_id = DISCOVERY.node_id if DISCOVERY else "local"
    node_name = DISCOVERY.node_name if DISCOVERY else socket.gethostname()

    return jsonify({
        "node_id": node_id,
        "node_name": node_name,
        "server_time": now,
        "peers": peers,
        "workers": workers,
        "jobs": jobs,
    })


@app.route("/api/peers")
def api_peers():
    if not DISCOVERY:
        return jsonify({"peers": []})
    now = time.time()
    peers = []
    for p in DISCOVERY.get_peers():
        peers.append({
            "node_id": p["node_id"], "ip": p["ip"],
            "port": p["port"], "name": p["name"],
            "last_seen_ago": round(now - p["last_seen"]),
        })
    return jsonify({"peers": peers,
                    "self": {"node_id": DISCOVERY.node_id,
                             "ip": DISCOVERY.local_ip,
                             "port": DISCOVERY.http_port,
                             "name": DISCOVERY.node_name}})


# ---------------------------------------------------------------------------
# Job detail (enhanced with per-frame data)
# ---------------------------------------------------------------------------
@app.route("/api/jobs/<job_id>")
def api_job_detail(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        frames = []
        for k, v in sorted(job["frames"].items(), key=lambda kv: int(kv[0])):
            frames.append({
                "frame": int(k),
                "status": v["status"],
                "worker": v.get("worker"),
                "render_time": v.get("render_time"),
                "attempts": v.get("attempts", 0),
                "progress": v.get("progress", 0),
                "started_at": v.get("started_at"),
                "finished_at": v.get("finished_at"),
                "last_error": v.get("last_error"),
            })
    return jsonify({"summary": job_summary(job), "frames": frames})


# ---------------------------------------------------------------------------
# Job submission (with auto frame detection)
# ---------------------------------------------------------------------------
@app.route("/api/jobs", methods=["POST"])
def api_submit_job():
    job_id = uuid.uuid4().hex[:8]
    form = request.form

    name = (form.get("name") or "").strip() or f"job_{job_id}"
    frame_start = int(form.get("frame_start") or 0)
    frame_end = int(form.get("frame_end") or 0)
    frame_step = max(1, int(form.get("frame_step") or 1))
    engine = (form.get("engine") or "").strip()
    device = (form.get("device") or "").strip()
    samples = (form.get("samples") or "").strip()
    out_format = (form.get("format") or "").strip()
    skip_existing = form.get("skip_existing") in ("1", "true", "on")
    shared_path = (form.get("shared_path") or "").strip()

    blend_filename = None
    blend_path = None

    if shared_path:
        blend_filename = os.path.basename(shared_path)
        probe_path = shared_path
    else:
        if "blendfile" not in request.files:
            return jsonify({"error": "no blend file uploaded and no shared_path"}), 400
        f = request.files["blendfile"]
        if not f.filename:
            return jsonify({"error": "empty filename"}), 400
        blend_filename = os.path.basename(f.filename)
        blend_path = str(BLEND_DIR / f"{job_id}_{blend_filename}")
        f.save(blend_path)
        probe_path = blend_path

    # Auto-detect settings from blend file if not specified
    auto_detect = (frame_start == 0 and frame_end == 0)
    if auto_detect or not out_format:
        probed = probe_blend_file(probe_path)
        if probed:
            if auto_detect:
                frame_start = probed.get("frame_start", 1)
                frame_end = probed.get("frame_end", 250)
                if frame_step <= 1:
                    frame_step = probed.get("frame_step", 1)
            if not engine:
                engine = probed.get("engine", "")
            if not out_format:
                out_format = probed.get("format", "PNG")
            if not samples and probed.get("samples"):
                samples = str(probed["samples"])
        else:
            if auto_detect:
                return jsonify({"error": "Could not detect frame range from blend file. "
                                "Please specify frame_start and frame_end manually, "
                                "or ensure Blender is installed on this machine."}), 400

    if not out_format:
        out_format = "PNG"
    if frame_start == 0:
        frame_start = 1
    if frame_end == 0:
        frame_end = frame_start

    job_out = OUTPUT_DIR / name
    job_out.mkdir(parents=True, exist_ok=True)
    ext = EXT_FOR_FORMAT.get(out_format, "png")

    frames = {}
    for fno in range(frame_start, frame_end + 1, frame_step):
        out_file = job_out / f"{name}_{fno:04d}.{ext}"
        if skip_existing and out_file.exists():
            frames[str(fno)] = {"status": "done", "worker": "skipped",
                                "render_time": 0, "attempts": 0, "progress": 100}
        else:
            frames[str(fno)] = {"status": "pending", "worker": None,
                                "render_time": None, "attempts": 0, "progress": 0}

    job = {
        "id": job_id, "name": name,
        "blend_filename": blend_filename, "blend_path": blend_path,
        "shared_path": shared_path or None,
        "frame_start": frame_start, "frame_end": frame_end,
        "frame_step": frame_step,
        "engine": engine, "device": device, "samples": samples,
        "format": out_format, "ext": ext,
        "output_dir": str(job_out),
        "status": "queued", "created_at": time.time(),
        "frames": frames,
    }
    with LOCK:
        JOBS[job_id] = job
        _recompute_job_statuses()
        for _wid in list(WORKER_SSE_QUEUES.keys()):
            _push_worker_event(_wid, {"type": "work_available"})
        _broadcast_dashboard({"type": "job_submitted", "job_id": job_id})
    save_state()

    # Wake all workers so they immediately poll (for non-SSE workers too)
    if DISCOVERY:
        DISCOVERY.broadcast_wake()

    return jsonify({"ok": True, "job_id": job_id, "frames": len(frames),
                    "frame_start": frame_start, "frame_end": frame_end,
                    "frame_step": frame_step})


# ---------------------------------------------------------------------------
# Blend probe endpoint
# ---------------------------------------------------------------------------
@app.route("/api/blend/probe", methods=["POST"])
def api_probe_blend():
    shared_path = (request.form.get("shared_path") or "").strip()
    if shared_path:
        if not Path(shared_path).exists():
            return jsonify({"ok": False, "error": "File not found at shared path"}), 404
        scene = probe_blend_file(shared_path)
    elif "blendfile" in request.files:
        f = request.files["blendfile"]
        tmp_path = BLEND_DIR / f"probe_{uuid.uuid4().hex[:8]}_{f.filename}"
        f.save(str(tmp_path))
        try:
            scene = probe_blend_file(str(tmp_path))
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    else:
        return jsonify({"ok": False, "error": "No file or path provided"}), 400

    if scene:
        return jsonify({"ok": True, "scene": scene})
    return jsonify({"ok": False, "error": "Could not probe blend file. Is Blender installed?"}), 500


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------
@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_cancel(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        job["status"] = "cancelled"
        affected_workers = set()
        for fr in job["frames"].values():
            if fr["status"] in ("pending", "assigned"):
                if fr.get("worker"):
                    affected_workers.add(fr["worker"])
                fr["status"] = "cancelled"
                fr["worker"] = None
        for awid in affected_workers:
            _push_worker_event(awid, {"type": "cancel", "job_id": job_id})
        _broadcast_dashboard({"type": "job_cancelled", "job_id": job_id})
    save_state()
    # Also wake workers via UDP so those without SSE detect the cancel
    if DISCOVERY:
        DISCOVERY.broadcast_wake()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/delete", methods=["POST"])
def api_delete(job_id):
    with LOCK:
        job = JOBS.pop(job_id, None)
    if job and job.get("blend_path") and os.path.exists(job["blend_path"]):
        try:
            os.remove(job["blend_path"])
        except OSError:
            pass
    save_state()
    return jsonify({"ok": bool(job)})


@app.route("/api/jobs/<job_id>/retry", methods=["POST"])
def api_retry(job_id):
    """Retry all failed frames in a job."""
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        retried = 0
        for fr in job["frames"].values():
            if fr["status"] == "failed":
                fr["status"] = "pending"
                fr["worker"] = None
                fr["progress"] = 0
                fr["attempts"] = 0
                fr["last_error"] = None
                retried += 1
        _recompute_job_statuses()
    save_state()
    if DISCOVERY:
        DISCOVERY.broadcast_wake()
    return jsonify({"ok": True, "retried": retried})


@app.route("/api/jobs/<job_id>/blend")
def api_get_blend(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or not job.get("blend_path"):
        abort(404)
    return send_file(job["blend_path"], as_attachment=True,
                     download_name=job["blend_filename"])


@app.route("/api/jobs/<job_id>/zip")
def api_zip(job_id):
    import zipfile
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    out_dir = Path(job["output_dir"])
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out_dir.glob("*")):
            if p.is_file():
                z.write(p, p.name)
    mem.seek(0)
    return Response(mem.read(), mimetype="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{job["name"]}_frames.zip"'})


# ---------------------------------------------------------------------------
# Worker API
# ---------------------------------------------------------------------------
@app.route("/api/workers/register", methods=["POST"])
def api_register():
    d = request.get_json(force=True, silent=True) or {}
    wid = d.get("id")
    if not wid:
        return jsonify({"error": "missing worker id"}), 400
    with LOCK:
        w = WORKERS.get(wid, {"frames_done": 0, "render_times": []})
        w.update({
            "id": wid, "name": d.get("name", wid),
            "hostname": d.get("hostname", "?"),
            "gpu_name": d.get("gpu_name", "?"),
            "os": d.get("os", "?"),
            "blender_version": d.get("blender_version", "?"),
            "status": "idle", "current_job": None,
            "current_frame": None, "progress": 0,
            "last_seen": time.time(),
        })
        if "render_times" not in w:
            w["render_times"] = []
        WORKERS[wid] = w
    return jsonify({"ok": True})


@app.route("/api/workers/<wid>/heartbeat", methods=["POST"])
def api_heartbeat(wid):
    d = request.get_json(force=True, silent=True) or {}
    with LOCK:
        w = WORKERS.get(wid)
        if not w:
            return jsonify({"reregister": True})
        w["last_seen"] = time.time()
        if "status" in d:
            w["status"] = d["status"]
        if "progress" in d:
            w["progress"] = d["progress"]
    return jsonify({"ok": True})


@app.route("/api/workers/<wid>/next", methods=["POST"])
def api_next_frame(wid):
    """Pull-based scheduling with auto-registration."""
    d = request.get_json(force=True, silent=True) or {}
    with LOCK:
        w = WORKERS.get(wid)
        if not w:
            # Auto-register if worker info provided in request
            if d.get("name"):
                w = {
                    "id": wid, "name": d.get("name", wid),
                    "hostname": d.get("hostname", "?"),
                    "gpu_name": d.get("gpu_name", "?"),
                    "os": d.get("os", "?"),
                    "blender_version": d.get("blender_version", "?"),
                    "status": "idle", "current_job": None,
                    "current_frame": None, "progress": 0,
                    "frames_done": 0, "render_times": [],
                    "last_seen": time.time(),
                }
                WORKERS[wid] = w
            else:
                return jsonify({"reregister": True})
        w["last_seen"] = time.time()

        for job in sorted(JOBS.values(), key=lambda x: x["created_at"]):
            if job["status"] in ("cancelled", "done"):
                continue
            pending = sorted((int(k) for k, v in job["frames"].items()
                              if v["status"] == "pending"))
            if not pending:
                continue
            fno = pending[0]
            fr = job["frames"][str(fno)]
            fr["status"] = "assigned"
            fr["worker"] = wid
            fr["started_at"] = time.time()
            fr["attempts"] = fr.get("attempts", 0) + 1
            fr["progress"] = 0
            w["status"] = "rendering"
            w["current_job"] = job["id"]
            w["current_frame"] = fno
            w["progress"] = 0
            job["status"] = "rendering"
            assignment = {
                "job_id": job["id"], "frame": fno,
                "blend_filename": job["blend_filename"],
                "shared_path": job.get("shared_path"),
                "blend_url": f"/api/jobs/{job['id']}/blend",
                "engine": job["engine"], "device": job["device"],
                "samples": job["samples"], "format": job["format"],
            }
            save_state()
            return jsonify({"assignment": assignment})
        w["status"] = "idle"
        w["current_job"] = None
        w["current_frame"] = None
    return jsonify({"assignment": None})


@app.route("/api/workers/<wid>/progress", methods=["POST"])
def api_progress(wid):
    d = request.get_json(force=True, silent=True) or {}
    cancel = False
    prefetch = None
    with LOCK:
        w = WORKERS.get(wid)
        if w:
            w["last_seen"] = time.time()
            w["progress"] = d.get("progress", 0)
        job = JOBS.get(d.get("job_id"))
        if job:
            if job["status"] == "cancelled":
                cancel = True
            else:
                fr = job["frames"].get(str(d.get("frame")))
                if fr:
                    if fr["status"] == "cancelled":
                        cancel = True
                    elif fr["status"] == "assigned":
                        progress = d.get("progress", 0)
                        fr["progress"] = progress
                        if not cancel and progress >= PREFETCH_THRESHOLD:
                            assignments = _try_prefetch_frames(wid)
                            if assignments:
                                prefetch = assignments
    return jsonify({"ok": True, "cancel": cancel, "prefetch": prefetch})


@app.route("/api/workers/<wid>/complete", methods=["POST"])
def api_complete(wid):
    job_id = request.form["job_id"]
    frame = int(request.form["frame"])
    render_time = float(request.form.get("render_time", 0))
    with LOCK:
        job = JOBS.get(job_id)
        w = WORKERS.get(wid)
    if not job:
        return jsonify({"error": "unknown job"}), 404

    # Pre-check cancel status before doing any disk I/O
    with LOCK:
        fr_check = job["frames"].get(str(frame))
        is_cancelled = fr_check is not None and fr_check["status"] == "cancelled"

    if not is_cancelled:
        out_dir = Path(job["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{job['name']}_{frame:04d}.{job['ext']}"
        if "image" in request.files:
            request.files["image"].save(str(out_file))

    with LOCK:
        fr = job["frames"].get(str(frame))
        if fr and not is_cancelled:
            fr["status"] = "done"
            fr["render_time"] = render_time
            fr["worker"] = wid
            fr["progress"] = 100
            fr["finished_at"] = time.time()
            fr.pop("prefetch_deadline", None)
        if w:
            w["status"] = "idle"
            w["current_job"] = None
            w["current_frame"] = None
            w["progress"] = 0
            w["last_seen"] = time.time()
            if not is_cancelled:
                w["frames_done"] = w.get("frames_done", 0) + 1
                if "render_times" not in w:
                    w["render_times"] = []
                w["render_times"].append(render_time)
                # keep last 100 times for avg
                if len(w["render_times"]) > 100:
                    w["render_times"] = w["render_times"][-100:]
        if not is_cancelled:
            _recompute_job_statuses()
        # Push SSE events regardless of cancel status
        _push_worker_event(wid, {"type": "work_available"})
        if not is_cancelled:
            _broadcast_dashboard({"type": "frame_done", "job_id": job_id, "frame": frame})
    save_state()
    return jsonify({"ok": True})


@app.route("/api/workers/<wid>/fail", methods=["POST"])
def api_fail(wid):
    d = request.get_json(force=True, silent=True) or {}
    job_id = d.get("job_id")
    frame = d.get("frame")
    log = (d.get("log") or "")[-4000:]
    with LOCK:
        job = JOBS.get(job_id)
        w = WORKERS.get(wid)
        if job:
            fr = job["frames"].get(str(frame))
            if fr:
                fr["last_error"] = log
                if fr.get("attempts", 0) >= MAX_FRAME_ATTEMPTS:
                    fr["status"] = "failed"
                else:
                    fr["status"] = "pending"
                fr["worker"] = None
                fr["progress"] = 0
                fr.pop("prefetch_deadline", None)
        if w:
            w["status"] = "idle"
            w["current_job"] = None
            w["current_frame"] = None
            w["last_seen"] = time.time()
        _recompute_job_statuses()
    save_state()
    print(f"[FAIL] worker={wid} job={job_id} frame={frame}\n{log[-500:]}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# SSE endpoints
# ---------------------------------------------------------------------------
@app.route("/api/workers/<wid>/stream")
def api_worker_stream(wid):
    """SSE stream for pushing work_available / cancel events to a specific worker."""
    q = _queue.Queue(maxsize=10)
    with LOCK:
        WORKER_SSE_QUEUES[wid] = q

    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=25)
                    if event is None:
                        return
                    yield f"data: {json.dumps(event)}\n\n"
                except _queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with LOCK:
                if WORKER_SSE_QUEUES.get(wid) is q:
                    WORKER_SSE_QUEUES.pop(wid, None)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/events")
def api_dashboard_events():
    """SSE stream for pushing real-time state change events to dashboard tabs."""
    q = _queue.Queue(maxsize=20)
    DASHBOARD_SSE_CLIENTS.append(q)

    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=25)
                    if event is None:
                        return
                    yield f"data: {json.dumps(event)}\n\n"
                except _queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            try:
                DASHBOARD_SSE_CLIENTS.remove(q)
            except ValueError:
                pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/jobs/<job_id>/frames/<int:frame_num>/image")
def api_frame_image(job_id, frame_num):
    """Serve a single completed frame image directly (no ZIP required)."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    out_file = Path(job["output_dir"]) / f"{job['name']}_{frame_num:04d}.{job['ext']}"
    if not out_file.exists():
        abort(404)
    return send_file(str(out_file))


# ---------------------------------------------------------------------------
# Main (standalone mode - also usable via renderhive.py)
# ---------------------------------------------------------------------------
def create_app(discovery=None, blender_path=None):
    """Factory: set up globals and return the Flask app."""
    global DISCOVERY, BLENDER_PATH
    DISCOVERY = discovery
    BLENDER_PATH = blender_path or detect_blender()
    load_state()
    threading.Thread(target=reap_dead_workers_and_frames, daemon=True).start()
    return app


def main():
    import socket as _socket
    ap = argparse.ArgumentParser(description="RenderHive coordinator")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    create_app()

    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"

    print("=" * 60)
    print("  RenderHive coordinator running")
    print(f"  Dashboard:  http://{ip}:{args.port}")
    print("=" * 60)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
