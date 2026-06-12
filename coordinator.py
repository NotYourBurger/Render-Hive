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
import math
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
BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_dir():
    """User data lives in Documents/RenderHive (override: RENDERHIVE_DATA_DIR)."""
    env = os.environ.get("RENDERHIVE_DATA_DIR")
    if env:
        return Path(env)
    docs = Path.home() / "Documents"
    base = docs if docs.exists() else Path.home()
    return base / "RenderHive"


DATA_DIR      = _resolve_data_dir()
BLEND_DIR     = DATA_DIR / "blends"
OUTPUT_DIR    = DATA_DIR / "output"
PREVIEW_DIR   = DATA_DIR / "previews"
STATE_FILE    = DATA_DIR / "state.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

WORKER_TIMEOUT     = 60    # seconds without heartbeat before a worker is offline
FRAME_TIMEOUT      = 1800  # hard cap on a single frame render
STALL_TIMEOUT      = 300   # no progress update for this long -> frame requeued
ORPHAN_GRACE       = 30    # worker reports idle while frame assigned this long -> requeue
MAX_FRAME_ATTEMPTS = 3
PREFETCH_THRESHOLD = 75   # % progress that triggers prefetch
PREFETCH_DEADLINE  = 90   # seconds before stale prefetch is reclaimed
DEFAULT_PRIORITY   = 5    # job priority 1 (lowest) .. 10 (highest)
HONEY_START        = 100  # welcome stipend for a fresh node's honey jar
HONEY_PER_FRAME    = 1    # honey earned (worker) / charged (job owner) per frame
HONEY_LOAN_INTEREST = 0.5  # borrow N, owe N * 1.5 (no cap on loan size)
HONEY_LOAN_OVERDUE_INTEREST = 1.0  # miss the deadline: interest rises to 100%
HONEY_LOAN_DAYS    = 7    # repay deadline; overdue blocks job posting
PACK_SCRIPT        = BASE_DIR / "pack_deps.py"
PACK_TIMEOUT       = 1800  # dependency packing can copy many GB
PREVIEW_SCRIPT     = BASE_DIR / "preview_frame.py"
PREVIEW_TIMEOUT    = 120   # converting one frame to a JPEG preview
# Formats browsers can display in an <img> tag; everything else (EXR, TIFF)
# is converted to a cached JPEG preview before serving to the dashboard
BROWSER_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
# File types stored uncompressed in the pack zip (already compressed formats)
PACK_STORED_EXTENSIONS = {
    ".vdb", ".abc",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg",
    ".mp3", ".ogg", ".oga", ".flac", ".aac", ".m4a", ".opus",
    ".jpg", ".jpeg", ".png", ".webp", ".jp2", ".j2k", ".exr", ".dds",
}


def _migrate_legacy_data():
    """One-time copy of the old farm_data folder (next to the program) into
    the new per-user data directory."""
    legacy = BASE_DIR / "farm_data"
    if os.environ.get("RENDERHIVE_DATA_DIR"):
        return
    if not legacy.exists() or STATE_FILE.exists():
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for item in ("config.json", "state.json", "settings.json"):
            src = legacy / item
            if src.exists() and not (DATA_DIR / item).exists():
                if item == "config.json":
                    # node_id must be unique per machine. A farm_data folder
                    # that came from a git clone or a copied install carries
                    # another PC's id, which makes peers ignore each other.
                    cfg = json.loads(src.read_text(encoding="utf-8"))
                    cfg.pop("node_id", None)
                    (DATA_DIR / item).write_text(
                        json.dumps(cfg, indent=2), encoding="utf-8")
                else:
                    shutil.copy2(src, DATA_DIR / item)
        for sub in ("blends", "output"):
            src = legacy / sub
            if src.exists():
                shutil.copytree(src, DATA_DIR / sub, dirs_exist_ok=True)
        print(f"  migrated user data: {legacy} -> {DATA_DIR}")
    except Exception as e:
        print(f"warn: data migration failed: {e}")


_migrate_legacy_data()
for d in (DATA_DIR, BLEND_DIR, OUTPUT_DIR, PREVIEW_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # 8 GB

LOCK = threading.Lock()
JOBS = {}
WORKERS = {}
# This node's honey jar (render-credit economy). Each frame a worker of this
# node renders — for any coordinator on the LAN — earns 1 honey, banked here
# via /api/honey/earn. Each finished frame of a job posted on THIS coordinator
# costs 1 honey. An empty jar blocks new job submissions, so contributing
# render power to the hive is what buys render power from it. Rendering your
# own job on your own GPU earns back what it costs (net zero), so a solo node
# never runs dry. A node that really needs to render can take one loan at a
# time from the hive bank: borrow N, owe 1.5N, due in 7 days — miss the
# deadline and the interest doubles to 100% (owe 2N) and job posting freezes
# until the loan is repaid.
HONEY = {"balance": HONEY_START, "earned": 0, "spent": 0, "loan": None}


def _loan_interest(amount, rate):
    return math.ceil(amount * rate)


def _check_loan_overdue():
    """LOCK must be held. Once the repay deadline passes, the loan's interest
    rises from 50% to 100% of the borrowed amount — applied exactly once,
    and computed from the original amount so partial repayments don't shrink
    the penalty. Returns True when the bump was just applied (caller should
    save_state once outside the lock)."""
    loan = HONEY.get("loan")
    if not loan or loan.get("penalized") or time.time() <= loan["due_at"]:
        return False
    loan["penalized"] = True
    loan["owed"] += (_loan_interest(loan["amount"], HONEY_LOAN_OVERDUE_INTEREST)
                     - _loan_interest(loan["amount"], HONEY_LOAN_INTEREST))
    return True
WORKER_SSE_QUEUES:     dict = {}  # wid -> queue.Queue (connected idle workers)
DASHBOARD_SSE_CLIENTS: list = []  # list of queue.Queue (connected dashboard tabs)

# Set by renderhive.py before app starts
DISCOVERY = None      # PeerDiscovery instance
BLENDER_PATH = None   # path to blender executable


# ---------------------------------------------------------------------------
# Blender detection (for probing blend files)
# ---------------------------------------------------------------------------
def detect_blender():
    """Return path to the newest installed Blender (4.0+). Falls back to PATH."""
    # 1. Explicit override via environment variable
    env = os.environ.get("BLENDER")
    if env and Path(env).exists():
        return env
    # 2. Scan Windows install directory for the highest Blender version
    base = Path(r"C:\Program Files\Blender Foundation")
    if base.exists():
        candidates = []
        for d in base.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r"Blender (\d+)\.(\d+)", d.name)
            if m:
                major, minor = int(m.group(1)), int(m.group(2))
                if major >= 4:
                    exe = d / "blender.exe"
                    if exe.exists():
                        candidates.append(((major, minor), str(exe)))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)  # newest first
            chosen = candidates[0][1]
            if len(candidates) > 1:
                all_paths = [c[1] for c in candidates]
                print(f"  Found Blender installs: {all_paths}")
                print(f"  Using: {chosen}")
            return chosen
    # 3. Fallback: search PATH
    found = shutil.which("blender")
    if found:
        print(f"  WARNING: No Blender 4+ found in Program Files, using: {found}")
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
# Dependency packing (videos, image sequences, VDB, Alembic, ...)
# ---------------------------------------------------------------------------
def pack_blend_job(job_id):
    """Background worker: run pack_deps.py inside Blender to localize every
    external file the blend references, zip the resulting project folder and
    flip the job from 'packing' to 'queued'. Packing was explicitly requested,
    so on failure the job is FAILED (rendering anyway would silently produce
    pink frames); the Retry button re-attempts packing."""
    import zipfile
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "packing":
            return
        # Shared-path jobs are packed at their ORIGINAL location so the
        # blend's relative ("//...") asset paths still resolve. Uploaded
        # blends are packed from the upload copy, which only works for
        # absolute asset paths.
        src_path = job.get("shared_path") or job["blend_path"]
        src_is_shared = bool(job.get("shared_path"))
        blend_filename = job["blend_filename"]
        job["pack_progress"] = "collecting files"

    pack_dir = BLEND_DIR / f"{job_id}_pack"
    zip_path = BLEND_DIR / f"{job_id}_pack.zip"
    manifest = None
    error = None

    blender = BLENDER_PATH or detect_blender()
    if not blender:
        error = "Blender not found on this machine"
    elif not src_path or not os.path.exists(src_path):
        error = f"blend file not found: {src_path}"
    else:
        try:
            shutil.rmtree(pack_dir, ignore_errors=True)
            pack_dir.mkdir(parents=True, exist_ok=True)
            # The upload is stored as <job_id>_<name>.blend; pass the original
            # name so the packed blend is saved as workers expect it
            cmd = [blender, "-b", "--factory-startup", str(src_path),
                   "-P", str(PACK_SCRIPT), "--", str(pack_dir), blend_filename]
            result = subprocess.run(
                cmd, capture_output=True, timeout=PACK_TIMEOUT,
                encoding="utf-8", errors="replace")
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            for line in output.splitlines():
                if line.startswith("RENDERHIVE_PACK:"):
                    manifest = json.loads(line[len("RENDERHIVE_PACK:"):])
                    break
            if manifest is None:
                # Surface the tail of Blender's output — that's where the
                # Python traceback ends up when the pack script crashes
                tail = [l for l in output.strip().splitlines() if l.strip()]
                for line in tail[-10:]:
                    print(f"  pack output: {line}")
                error = "pack script produced no result"
                if tail:
                    error += f" (last output: {tail[-1][-300:]})"
            elif not manifest.get("ok"):
                error = manifest.get("error", "pack script failed")
            elif manifest.get("missing"):
                # The whole point of packing is that every dependency travels.
                # A file we couldn't find WOULD render pink — fail loudly.
                miss = manifest["missing"]
                shown = ", ".join(os.path.basename(m) for m in miss[:5])
                if len(miss) > 5:
                    shown += f" … and {len(miss) - 5} more"
                n_word = (f"{len(miss)} dependenc"
                          f"{'y' if len(miss) == 1 else 'ies'}")
                if manifest.get("missing_relative") and not src_is_shared:
                    # The classic upload failure: relative paths only resolve
                    # at the blend's original location, not in the upload copy
                    error = (
                        f"{n_word} could not be found ({shown}) because this "
                        ".blend stores RELATIVE paths, which break when only "
                        "the .blend is uploaded. Submit via 'Shared Path' "
                        "(the .blend's full path on this PC) — or in Blender "
                        "run File > External Data > Make All Paths Absolute, "
                        "save, and re-upload.")
                else:
                    error = (
                        f"{n_word} could not be found: {shown}. Restore the "
                        "missing files (or relink them in Blender), save, "
                        "and submit again.")
            elif not (pack_dir / blend_filename).exists():
                error = "packed blend file missing from pack folder"
        except subprocess.TimeoutExpired:
            error = f"packing timed out after {PACK_TIMEOUT}s"
        except Exception as e:
            error = str(e)

    if not error:
        try:
            files = [p for p in sorted(pack_dir.rglob("*")) if p.is_file()]
            total = sum(p.stat().st_size for p in files) or 1
            done = 0
            tmp_zip = zip_path.with_suffix(".zip.tmp")
            with zipfile.ZipFile(tmp_zip, "w") as z:
                for p in files:
                    # Already-compressed formats (VDB, video, PNG, EXR, ...)
                    # gain almost nothing from deflate but make zipping
                    # minutes slower on multi-GB caches — store those as-is
                    if p.suffix.lower() in PACK_STORED_EXTENSIONS:
                        z.write(p, p.relative_to(pack_dir).as_posix(),
                                compress_type=zipfile.ZIP_STORED)
                    else:
                        z.write(p, p.relative_to(pack_dir).as_posix(),
                                compress_type=zipfile.ZIP_DEFLATED,
                                compresslevel=1)
                    done += p.stat().st_size
                    with LOCK:
                        j2 = JOBS.get(job_id)
                        if j2 and j2["status"] == "packing":
                            j2["pack_progress"] = \
                                f"zipping {int(done * 100 / total)}%"
            os.replace(tmp_zip, zip_path)
        except Exception as e:
            error = f"could not zip packed project: {e}"
    shutil.rmtree(pack_dir, ignore_errors=True)

    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "packing":
            # Deleted or cancelled while packing — discard the work
            try:
                zip_path.unlink()
            except OSError:
                pass
            return
        job.pop("pack_progress", None)
        if error:
            job["pack_error"] = error
            if manifest and manifest.get("missing"):
                job["pack_missing"] = manifest["missing"]
            job["status"] = "failed"
            for fr in job["frames"].values():
                if fr["status"] == "pending":
                    fr["status"] = "failed"
                    fr["last_error"] = f"dependency packing failed: {error}"
            print(f"  [PACK] job {job_id}: packing failed — {error}")
            _broadcast_dashboard({"type": "job_packed", "job_id": job_id})
        else:
            job["packed_zip"] = str(zip_path)
            if manifest.get("missing"):
                job["pack_missing"] = manifest["missing"]
                print(f"  [PACK] job {job_id}: {len(manifest['missing'])} "
                      "dependencies were not found on disk")
            mb = manifest.get("bytes", 0) / 1048576
            print(f"  [PACK] job {job_id}: {manifest.get('copied', 0)} files "
                  f"collected, project is {mb:.1f} MB")
            job["status"] = "queued"
            _recompute_job_statuses()
            for _wid in list(WORKER_SSE_QUEUES.keys()):
                _push_worker_event(_wid, {"type": "work_available"})
            _broadcast_dashboard({"type": "job_packed", "job_id": job_id})
    save_state()
    if not error and DISCOVERY:
        DISCOVERY.broadcast_wake()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_state():
    """Atomic write: temp file + rename so a crash can't corrupt state.json."""
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump({"jobs": JOBS, "honey": HONEY}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("warn: could not save state:", e)


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.load(open(STATE_FILE))
            JOBS.update(data.get("jobs", {}))
            honey = data.get("honey", {})
            for k in HONEY:
                if isinstance(honey.get(k), (int, float)):
                    HONEY[k] = honey[k]
            loan = honey.get("loan")
            if isinstance(loan, dict) and all(
                    isinstance(loan.get(k), (int, float))
                    for k in ("amount", "owed", "taken_at", "due_at")):
                HONEY["loan"] = loan
            for job in JOBS.values():
                job.setdefault("priority", DEFAULT_PRIORITY)
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
# Settings (per-node, persisted)
# ---------------------------------------------------------------------------
def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(settings):
    try:
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print("warn: could not save settings:", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
EXT_FOR_FORMAT = {
    "PNG": "png", "JPEG": "jpg", "OPEN_EXR": "exr",
    "OPEN_EXR_MULTILAYER": "exr", "TIFF": "tif", "WEBP": "webp",
}

# Serialize preview conversions: each one launches a headless Blender, and a
# burst of thumbnail requests must not spawn a Blender per frame
PREVIEW_LOCK = threading.Lock()


def ensure_preview(job, frame_num):
    """Return the path of a browser-viewable JPEG preview for a finished
    EXR/TIFF frame, converting (via headless Blender) and caching it the
    first time. Returns None when the frame file is gone or conversion
    fails — the dashboard then simply hides that thumbnail."""
    out_file = Path(job["output_dir"]) / f"{job['name']}_{frame_num:04d}.{job['ext']}"
    if not out_file.exists():
        return None
    prev_file = PREVIEW_DIR / job["id"] / f"{frame_num:04d}.jpg"
    with PREVIEW_LOCK:
        # mtime check so a re-rendered (retried) frame gets a fresh preview
        if prev_file.exists() and prev_file.stat().st_mtime >= out_file.stat().st_mtime:
            return prev_file
        blender = BLENDER_PATH or detect_blender()
        if not blender:
            print(f"  preview failed for {out_file.name}: Blender not found")
            return None
        prev_file.parent.mkdir(parents=True, exist_ok=True)
        cmd = [blender, "-b", "--factory-startup",
               "-P", str(PREVIEW_SCRIPT), "--", str(out_file), str(prev_file)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=PREVIEW_TIMEOUT,
                encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"  preview failed for {out_file.name}: {e}")
            return None
        if not prev_file.exists():
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            tail = [l for l in output.strip().splitlines() if l.strip()]
            detail = tail[-1][-200:] if tail else "no output"
            print(f"  preview failed for {out_file.name}: {detail}")
            return None
        return prev_file


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


def _notify_idle_workers_sse(exclude_wid=None):
    """Push work_available to all idle SSE-connected workers. Call inside LOCK."""
    for _wid in list(WORKER_SSE_QUEUES.keys()):
        if _wid == exclude_wid:
            continue
        w = WORKERS.get(_wid)
        if w and w["status"] == "idle" and not w.get("paused"):
            _push_worker_event(_wid, {"type": "work_available"})


def _scheduler_jobs():
    """Jobs in dispatch order: priority (high first), then submit time (FIFO).
    Excludes jobs that can't accept work. Call inside LOCK."""
    skip = ("cancelled", "done", "paused", "packing")
    return [j for j in sorted(JOBS.values(),
                              key=lambda j: (-j.get("priority", DEFAULT_PRIORITY),
                                             j["created_at"]))
            if j["status"] not in skip]


def _pick_job_for_worker(wid, jobs_with_pending):
    """Among the highest-priority jobs with pending frames, prefer the job the
    worker rendered last (its blend file is already cached on that machine)."""
    if not jobs_with_pending:
        return None
    best = jobs_with_pending[0]
    w = WORKERS.get(wid)
    last_job = w.get("last_job") if w else None
    if last_job and last_job != best["id"]:
        for job in jobs_with_pending:
            if job.get("priority", DEFAULT_PRIORITY) != best.get("priority", DEFAULT_PRIORITY):
                break  # never let affinity override priority
            if job["id"] == last_job:
                return job
    return best


def _assignment_payload(job, fno):
    return {
        "job_id": job["id"], "frame": int(fno),
        "blend_filename": job["blend_filename"],
        "shared_path": job.get("shared_path"),
        "blend_url": f"/api/jobs/{job['id']}/blend",
        "engine": job["engine"], "device": job["device"],
        "samples": job["samples"], "format": job["format"],
        "packed": bool(job.get("packed_zip")),
    }


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
    if not w or w.get("paused"):
        return []
    already = sum(
        1 for j in JOBS.values() for f in j["frames"].values()
        if f.get("worker") == wid and f.get("prefetch_deadline") and f["status"] == "assigned"
    )
    n_want = _prefetch_count(w) - already
    if n_want <= 0:
        return []
    remaining_pending = sum(
        1 for j in _scheduler_jobs()
        for f in j["frames"].values() if f["status"] == "pending"
    )
    active_workers = sum(1 for w2 in WORKERS.values() if w2["status"] == "rendering")
    if remaining_pending <= active_workers:
        return []
    assignments = []
    now = time.time()
    for job in _scheduler_jobs():
        for k, fr in sorted(job["frames"].items(), key=lambda kv: int(kv[0])):
            if fr["status"] != "pending":
                continue
            fr.update({
                "status": "assigned", "worker": wid,
                "started_at": now, "last_progress_at": now, "progress": 0,
                "attempts": fr.get("attempts", 0) + 1,
                "prefetch_deadline": now + PREFETCH_DEADLINE,
            })
            assignments.append(_assignment_payload(job, k))
            n_want -= 1
            if n_want <= 0:
                break
        if n_want <= 0:
            break
    return assignments


def _job_fps(job_id):
    """Combined frames/sec of workers currently rendering this job (inside LOCK)."""
    fps = 0.0
    for w in WORKERS.values():
        if w["status"] == "rendering" and w.get("current_job") == job_id:
            avg = _worker_avg_speed(w)
            if avg and avg > 0:
                fps += 1.0 / avg
    return fps if fps > 0 else None


def job_summary(job):
    now = time.time()
    frames = job["frames"]
    total = len(frames)
    done = sum(1 for f in frames.values() if f["status"] == "done")
    failed = sum(1 for f in frames.values() if f["status"] == "failed")
    rendering = sum(1 for f in frames.values() if f["status"] == "assigned")
    times = [f["render_time"] for f in frames.values()
             if f["status"] == "done" and f.get("render_time")]
    avg = sum(times) / len(times) if times else None
    remaining = sum(1 for f in frames.values()
                    if f["status"] in ("pending", "assigned"))
    # Credit partial progress of in-flight frames so the ETA shrinks smoothly
    partial = sum(f.get("progress", 0) for f in frames.values()
                  if f["status"] == "assigned") / 100.0
    effective_remaining = max(0.0, remaining - partial)
    # Prefer the speed of workers actually on this job; fall back to farm speed
    fps = _job_fps(job["id"]) or _farm_fps()
    if fps and fps > 0 and effective_remaining > 0:
        eta = effective_remaining / fps
    elif avg and effective_remaining:
        active = max(1, sum(1 for w in WORKERS.values() if w["status"] == "rendering"))
        eta = avg * effective_remaining / active
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
    # Elapsed wall-clock time for the job
    starts = [f["started_at"] for f in frames.values() if f.get("started_at")]
    finishes = [f["finished_at"] for f in frames.values() if f.get("finished_at")]
    if starts:
        if job["status"] in ("done", "failed", "cancelled") and finishes:
            elapsed = max(finishes) - min(starts)
        else:
            elapsed = now - min(starts)
    else:
        elapsed = None
    return {
        "id": job["id"], "name": job["name"], "status": job["status"],
        "blend_filename": job["blend_filename"],
        "frame_start": job["frame_start"], "frame_end": job["frame_end"],
        "frame_step": job["frame_step"],
        "priority": job.get("priority", DEFAULT_PRIORITY),
        "engine": job["engine"] or "(from file)",
        "device": job["device"] or "(from file)",
        "samples": job["samples"] or "(from file)",
        "total": total, "done": done, "failed": failed, "rendering": rendering,
        "avg_render_time": round(avg, 1) if avg else None,
        "eta_seconds": round(eta) if eta else None,
        "eta_confidence": confidence,
        "elapsed_seconds": round(elapsed) if elapsed else None,
        "farm_fps": round(fps, 4) if fps else None,
        "created_at": job["created_at"],
        "shared_path": job.get("shared_path"),
        "output_dir": job.get("output_dir"),
        "packed": bool(job.get("packed_zip")),
        "pack_error": job.get("pack_error"),
        "pack_missing": job.get("pack_missing"),
        "pack_progress": job.get("pack_progress"),
    }


def reap_dead_workers_and_frames():
    while True:
        time.sleep(10)
        _reap_once()


def _reap_once(now=None):
    """One pass of dead-worker / lost-frame recovery. Called by the reaper
    thread every 10s; callable directly from tests."""
    if now is None:
        now = time.time()
    with LOCK:
        for w in WORKERS.values():
            if w["status"] != "offline" and now - w["last_seen"] > WORKER_TIMEOUT:
                w["status"] = "offline"
                w["current_job"] = None
                w["current_frame"] = None
                w["progress"] = 0
        frames_recovered = False
        for job in JOBS.values():
            if job["status"] not in ("rendering", "queued", "paused"):
                continue
            for fno, fr in job["frames"].items():
                if fr["status"] != "assigned":
                    continue
                worker = WORKERS.get(fr["worker"])
                stale_worker = (worker is None or worker["status"] == "offline")
                # Orphaned: the worker is alive and reports idle, yet this
                # frame is still assigned to it (worker crashed mid-render
                # or silently dropped the assignment).
                orphaned = (worker is not None and worker["status"] == "idle"
                            and fr.get("prefetch_deadline") is None
                            and now - fr.get("started_at", now) > ORPHAN_GRACE)
                # Stalled: worker still heartbeats but the render makes no
                # progress (hung Blender, dead GPU).
                stalled = (fr.get("prefetch_deadline") is None
                           and now - fr.get("last_progress_at",
                                            fr.get("started_at", now)) > STALL_TIMEOUT)
                # Prefetch frames use a short deadline; normal frames use FRAME_TIMEOUT
                deadline = fr.get("prefetch_deadline")
                if deadline is not None:
                    stale_frame = now > deadline
                    if stale_frame and fr.get("progress", 0) == 0:
                        fr["attempts"] = max(0, fr.get("attempts", 1) - 1)
                    if stale_frame:
                        fr.pop("prefetch_deadline", None)
                else:
                    stale_frame = now - fr.get("started_at", now) > FRAME_TIMEOUT
                if stale_worker or stale_frame or orphaned or stalled:
                    if orphaned:
                        # Not the frame's fault — don't burn an attempt
                        fr["attempts"] = max(0, fr.get("attempts", 1) - 1)
                        print(f"  [RECOVER] frame {fno} orphaned by idle "
                              f"worker {fr['worker']} — requeued")
                    elif stalled and not stale_worker and not stale_frame:
                        print(f"  [RECOVER] frame {fno} stalled on "
                              f"worker {fr['worker']} — requeued")
                    if fr.get("attempts", 0) >= MAX_FRAME_ATTEMPTS:
                        fr["status"] = "failed"
                    else:
                        fr["status"] = "pending"
                        frames_recovered = True
                    fr["worker"] = None
                    fr["progress"] = 0
                    fr.pop("prefetch_deadline", None)
        if frames_recovered:
            _notify_idle_workers_sse()
        # Work stealing: reclaim low-progress frames from abnormally slow workers
        # Only when at least one idle worker is waiting for work
        idle_workers = [w for w in WORKERS.values()
                        if w["status"] == "idle" and not w.get("paused")
                        and now - w["last_seen"] < WORKER_TIMEOUT]
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
    # Wake remote workers (UDP) so recovered frames get picked up quickly
    if frames_recovered and DISCOVERY:
        DISCOVERY.broadcast_wake()


def _recompute_job_statuses():
    for job in JOBS.values():
        if job["status"] in ("cancelled", "paused", "packing"):
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
            if offline:
                shown_status = "offline"
            elif w.get("paused") and w["status"] != "rendering":
                shown_status = "paused"
            else:
                shown_status = w["status"]
            workers.append({
                "id": w["id"], "name": w["name"],
                "hostname": w.get("hostname", "?"),
                "gpu_name": w.get("gpu_name", "?"),
                "os": w.get("os", "?"),
                "blender_version": w.get("blender_version", "?"),
                "status": shown_status,
                "paused": bool(w.get("paused")),
                "current_job": w["current_job"],
                "current_job_name": cjob_name,
                "current_frame": w["current_frame"],
                "progress": w["progress"],
                "frames_done": w["frames_done"],
                "honey_earned": w.get("honey_earned", 0),
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
        loan_penalized = _check_loan_overdue()
        honey = dict(HONEY)
        jobs = [job_summary(j) for j in
                sorted(JOBS.values(), key=lambda x: x["created_at"], reverse=True)]
        # Queue-aware ETA: jobs are dispatched in priority/FIFO order, so a
        # queued job can't start until the active jobs ahead of it finish.
        summaries_by_id = {s["id"]: s for s in jobs}
        farm = _farm_fps()
        cum = 0.0
        for job in _scheduler_jobs():
            s = summaries_by_id.get(job["id"])
            if not s:
                continue
            own = s["eta_seconds"]
            if own is None and farm and farm > 0:
                rem = sum(1 for f in job["frames"].values()
                          if f["status"] in ("pending", "assigned"))
                own = rem / farm if rem else None
            if s["status"] == "queued" and s["rendering"] == 0 and own is not None:
                s["eta_seconds"] = round(own + cum)
                s["eta_queued_behind"] = round(cum) if cum > 0 else None
            if own is not None:
                cum += own

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

    if loan_penalized:
        save_state()

    node_id = DISCOVERY.node_id if DISCOVERY else "local"
    node_name = DISCOVERY.node_name if DISCOVERY else socket.gethostname()

    return jsonify({
        "node_id": node_id,
        "node_name": node_name,
        "server_time": now,
        "honey": honey,
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
    with LOCK:
        penalized = _check_loan_overdue()
        out_of_honey = HONEY["balance"] <= 0
        loan = HONEY.get("loan")
        loan_overdue = bool(loan and time.time() > loan["due_at"])
        loan_owed = loan["owed"] if loan else 0
    if penalized:
        save_state()
    if loan_overdue:
        return jsonify({"error": f"Your honey loan is overdue ({loan_owed} "
                        "honey owed — interest rose to 100%)! The hive bank "
                        "has frozen your jar — repay the loan before posting "
                        "new render jobs."}), 402
    if out_of_honey:
        return jsonify({"error": "Out of honey! Each rendered frame of your "
                        "jobs costs 1 honey and your jar is empty. Leave "
                        "RenderHive running so your GPUs render frames for "
                        "the hive — every frame they finish earns 1 honey. "
                        "Really stuck? Take a honey loan from the 🍯 panel "
                        "on the dashboard."}), 402
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
    pack_deps = form.get("pack_deps") in ("1", "true", "on")
    shared_path = (form.get("shared_path") or "").strip()
    output_dir_override = (form.get("output_dir") or "").strip()
    try:
        priority = max(1, min(10, int(form.get("priority") or DEFAULT_PRIORITY)))
    except ValueError:
        priority = DEFAULT_PRIORITY

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

    if output_dir_override:
        job_out = Path(output_dir_override)
    else:
        default_root = (load_settings().get("default_output_dir") or "").strip()
        job_out = (Path(default_root) if default_root else OUTPUT_DIR) / name
    try:
        job_out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create output directory "
                        f"'{job_out}': {e}"}), 400
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

    # Packing works for both sources. Shared-path is actually the better one:
    # the blend is opened at its original location, so relative asset paths
    # ("//textures/x.mp4") resolve. After packing, the project is fully
    # self-contained — workers no longer need to reach the shared path.
    do_pack = pack_deps and (blend_path is not None or bool(shared_path))

    job = {
        "id": job_id, "name": name,
        "blend_filename": blend_filename, "blend_path": blend_path,
        "shared_path": shared_path or None,
        "frame_start": frame_start, "frame_end": frame_end,
        "frame_step": frame_step,
        "engine": engine, "device": device, "samples": samples,
        "format": out_format, "ext": ext,
        "output_dir": str(job_out),
        "priority": priority,
        "status": "packing" if do_pack else "queued",
        "created_at": time.time(),
        "frames": frames,
    }
    with LOCK:
        JOBS[job_id] = job
        _recompute_job_statuses()
        if not do_pack:
            for _wid in list(WORKER_SSE_QUEUES.keys()):
                _push_worker_event(_wid, {"type": "work_available"})
        _broadcast_dashboard({"type": "job_submitted", "job_id": job_id})
    save_state()

    if do_pack:
        threading.Thread(target=pack_blend_job, args=(job_id,),
                         daemon=True, name=f"pack-{job_id}").start()
    # Wake all workers so they immediately poll (for non-SSE workers too)
    elif DISCOVERY:
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
    d = request.get_json(force=True, silent=True) or {}
    delete_outputs = bool(d.get("delete_outputs"))
    with LOCK:
        job = JOBS.pop(job_id, None)
    if job:
        for key in ("blend_path", "packed_zip"):
            p = job.get(key)
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        shutil.rmtree(PREVIEW_DIR / job_id, ignore_errors=True)
    if job and delete_outputs and job.get("output_dir"):
        # Only delete output folders we manage — never a user-chosen custom path
        out = Path(job["output_dir"]).resolve()
        try:
            if out.is_relative_to(OUTPUT_DIR.resolve()) and out != OUTPUT_DIR.resolve():
                shutil.rmtree(out, ignore_errors=True)
        except (OSError, ValueError):
            pass
    save_state()
    return jsonify({"ok": bool(job)})


@app.route("/api/jobs/<job_id>/retry", methods=["POST"])
def api_retry(job_id):
    """Retry all failed frames in a job. A job that failed because dependency
    packing failed re-attempts the packing first."""
    repack = False
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
        if job.get("pack_error") and (job.get("blend_path")
                                      or job.get("shared_path")):
            job.pop("pack_error", None)
            job["status"] = "packing"
            repack = True
        _recompute_job_statuses()
    save_state()
    if repack:
        threading.Thread(target=pack_blend_job, args=(job_id,),
                         daemon=True, name=f"pack-{job_id}").start()
    elif DISCOVERY:
        DISCOVERY.broadcast_wake()
    return jsonify({"ok": True, "retried": retried, "repacking": repack})


@app.route("/api/jobs/<job_id>/pause", methods=["POST"])
def api_pause(job_id):
    """Stop handing out new frames for this job. In-flight frames finish."""
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        if job["status"] not in ("queued", "rendering"):
            return jsonify({"error": f"cannot pause a {job['status']} job"}), 400
        job["status"] = "paused"
        _broadcast_dashboard({"type": "job_paused", "job_id": job_id})
    save_state()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/resume", methods=["POST"])
def api_resume(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        if job["status"] != "paused":
            return jsonify({"error": "job is not paused"}), 400
        job["status"] = "queued"
        _recompute_job_statuses()
        _notify_idle_workers_sse()
        _broadcast_dashboard({"type": "job_resumed", "job_id": job_id})
    save_state()
    if DISCOVERY:
        DISCOVERY.broadcast_wake()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/priority", methods=["POST"])
def api_set_priority(job_id):
    d = request.get_json(force=True, silent=True) or {}
    try:
        priority = max(1, min(10, int(d.get("priority", DEFAULT_PRIORITY))))
    except (TypeError, ValueError):
        return jsonify({"error": "priority must be an integer 1-10"}), 400
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        job["priority"] = priority
    save_state()
    return jsonify({"ok": True, "priority": priority})


@app.route("/api/jobs/<job_id>/frames/<int:frame_num>/requeue", methods=["POST"])
def api_requeue_frame(job_id, frame_num):
    """Force a single frame back into the queue (failed, stuck or done)."""
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        fr = job["frames"].get(str(frame_num))
        if not fr:
            abort(404)
        prev_worker = fr.get("worker")
        fr.update({"status": "pending", "worker": None, "progress": 0,
                   "attempts": 0, "last_error": None})
        fr.pop("prefetch_deadline", None)
        _recompute_job_statuses()
        _notify_idle_workers_sse()
    save_state()
    if DISCOVERY:
        DISCOVERY.broadcast_wake()
    return jsonify({"ok": True, "frame": frame_num, "was_on": prev_worker})


@app.route("/api/jobs/<job_id>/blend")
def api_get_blend(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    # Packed jobs ship a zip of the whole project (blend + deps folder)
    if job.get("packed_zip") and os.path.exists(job["packed_zip"]):
        return send_file(job["packed_zip"], as_attachment=True,
                         download_name=f"{job_id}_pack.zip")
    if not job.get("blend_path"):
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


@app.route("/api/workers/<wid>/pause", methods=["POST"])
def api_worker_pause(wid):
    """Enable/disable a render node. Paused workers get no new frames."""
    d = request.get_json(force=True, silent=True) or {}
    paused = bool(d.get("paused", True))
    with LOCK:
        w = WORKERS.get(wid)
        if not w:
            abort(404)
        w["paused"] = paused
        if not paused:
            _push_worker_event(wid, {"type": "work_available"})
    return jsonify({"ok": True, "paused": paused})


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

        if not w.get("paused"):
            jobs_with_pending = [
                job for job in _scheduler_jobs()
                if any(v["status"] == "pending" for v in job["frames"].values())
            ]
            job = _pick_job_for_worker(wid, jobs_with_pending)
            if job:
                now = time.time()
                fno = min(int(k) for k, v in job["frames"].items()
                          if v["status"] == "pending")
                fr = job["frames"][str(fno)]
                fr["status"] = "assigned"
                fr["worker"] = wid
                fr["started_at"] = now
                fr["last_progress_at"] = now
                fr["attempts"] = fr.get("attempts", 0) + 1
                fr["progress"] = 0
                w["status"] = "rendering"
                w["current_job"] = job["id"]
                w["current_frame"] = fno
                w["last_job"] = job["id"]
                w["progress"] = 0
                job["status"] = "rendering"
                assignment = _assignment_payload(job, fno)
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
            w["status"] = "rendering"
            if d.get("job_id"):
                w["current_job"] = d["job_id"]
                w["current_frame"] = d.get("frame")
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
                        fr["last_progress_at"] = time.time()
                        if not cancel and progress >= PREFETCH_THRESHOLD:
                            assignments = _try_prefetch_frames(wid)
                            if assignments:
                                prefetch = assignments
    return jsonify({"ok": True, "cancel": cancel, "prefetch": prefetch})


@app.route("/api/workers/<wid>/complete", methods=["POST"])
def api_complete(wid):
    job_id = request.form.get("job_id")
    try:
        frame = int(request.form.get("frame", ""))
    except ValueError:
        return jsonify({"error": "missing or invalid frame"}), 400
    if not job_id:
        return jsonify({"error": "missing job_id"}), 400
    try:
        render_time = float(request.form.get("render_time", 0))
    except ValueError:
        render_time = 0.0
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
            if job["ext"].lower() not in BROWSER_IMAGE_EXTS:
                # Convert EXR/TIFF to a JPEG preview now (in the background)
                # so the dashboard thumbnail is instant when first viewed
                threading.Thread(target=ensure_preview, args=(job, frame),
                                 daemon=True,
                                 name=f"preview-{job_id}-{frame}").start()

    with LOCK:
        fr = job["frames"].get(str(frame))
        paid = bool(fr and not is_cancelled)
        if paid:
            fr["status"] = "done"
            fr["render_time"] = render_time
            fr["worker"] = wid
            fr["progress"] = 100
            fr["finished_at"] = time.time()
            fr.pop("prefetch_deadline", None)
            # The job owner (this node) pays for the render; the response
            # tells the worker how much honey it earned so it can bank it
            # with its own coordinator
            HONEY["balance"] -= HONEY_PER_FRAME
            HONEY["spent"]   += HONEY_PER_FRAME
        if w:
            w["status"] = "idle"
            w["current_job"] = None
            w["current_frame"] = None
            w["progress"] = 0
            w["last_seen"] = time.time()
            if paid:
                w["honey_earned"] = w.get("honey_earned", 0) + HONEY_PER_FRAME
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
        # Notify completing worker + all other idle workers about available work
        _push_worker_event(wid, {"type": "work_available"})
        _notify_idle_workers_sse(exclude_wid=wid)
        if not is_cancelled:
            _broadcast_dashboard({"type": "frame_done", "job_id": job_id, "frame": frame})
    save_state()
    return jsonify({"ok": True, "honey": HONEY_PER_FRAME if paid else 0})


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
# Honey (render-credit economy)
# ---------------------------------------------------------------------------
@app.route("/api/honey")
def api_honey():
    with LOCK:
        penalized = _check_loan_overdue()
        result = dict(HONEY)
    if penalized:
        save_state()
    return jsonify(result)


@app.route("/api/honey/earn", methods=["POST"])
def api_honey_earn():
    """Bank honey a worker of this node earned by rendering a frame for any
    coordinator on the LAN. Workers call their home coordinator after every
    finished frame."""
    d = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(d.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if not 0 < amount <= 100:
        return jsonify({"error": "invalid amount"}), 400
    with LOCK:
        HONEY["balance"] += amount
        HONEY["earned"]  += amount
        balance = HONEY["balance"]
    save_state()
    return jsonify({"ok": True, "balance": balance})


@app.route("/api/honey/loan", methods=["POST"])
def api_honey_loan():
    """Take a loan from the hive bank: borrow N now (any size), owe N * 1.5,
    due in HONEY_LOAN_DAYS. Missing the deadline raises the interest to 100%
    and freezes job posting until the loan is repaid. One loan at a time."""
    d = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(d.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({"error": "loan must be a positive amount of honey"}), 400
    with LOCK:
        if HONEY.get("loan"):
            return jsonify({"error": "You already have an outstanding loan — "
                            "repay it before taking another."}), 400
        now = time.time()
        HONEY["loan"] = {
            "amount": amount,
            "owed": amount + _loan_interest(amount, HONEY_LOAN_INTEREST),
            "taken_at": now,
            "due_at": now + HONEY_LOAN_DAYS * 86400,
            "penalized": False,
        }
        HONEY["balance"] += amount
        result = dict(HONEY)
    save_state()
    return jsonify({"ok": True, **result})


@app.route("/api/honey/repay", methods=["POST"])
def api_honey_repay():
    """Pay down the outstanding loan from the jar balance. With no explicit
    amount, pays as much as the balance covers; the loan clears at 0 owed."""
    d = request.get_json(force=True, silent=True) or {}
    with LOCK:
        _check_loan_overdue()  # settle at the penalized rate, not the old owed
        loan = HONEY.get("loan")
        if not loan:
            return jsonify({"error": "no outstanding loan"}), 400
        try:
            amount = int(d.get("amount",
                                min(HONEY["balance"], loan["owed"])))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid amount"}), 400
        amount = min(amount, loan["owed"], HONEY["balance"])
        if amount <= 0:
            return jsonify({"error": "no honey available to repay with — "
                            "render some frames for the hive first"}), 400
        HONEY["balance"] -= amount
        loan["owed"] -= amount
        if loan["owed"] <= 0:
            HONEY["loan"] = None
        result = dict(HONEY)
    save_state()
    return jsonify({"ok": True, "repaid": amount, **result})


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------
@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    s = load_settings()
    return jsonify({
        "default_output_dir": s.get("default_output_dir", ""),
        "data_dir": str(DATA_DIR),
        "managed_output_dir": str(OUTPUT_DIR),
    })


@app.route("/api/settings", methods=["POST"])
def api_set_settings():
    d = request.get_json(force=True, silent=True) or {}
    s = load_settings()
    if "default_output_dir" in d:
        path = (d.get("default_output_dir") or "").strip()
        if path:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return jsonify({"error": f"Cannot use '{path}': {e}"}), 400
        s["default_output_dir"] = path
    save_settings(s)
    return jsonify({"ok": True, "settings": s})


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
    """Serve a single completed frame image directly (no ZIP required).

    Browsers cannot decode EXR/TIFF, so for those formats the dashboard
    preview is a cached JPEG conversion; pass ?raw=1 to download the
    original frame file instead."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    out_file = Path(job["output_dir"]) / f"{job['name']}_{frame_num:04d}.{job['ext']}"
    if not out_file.exists():
        abort(404)
    raw = request.args.get("raw") in ("1", "true")
    if raw or job["ext"].lower() in BROWSER_IMAGE_EXTS:
        return send_file(str(out_file), as_attachment=raw)
    prev_file = ensure_preview(job, frame_num)
    if not prev_file:
        abort(404)  # dashboard hides thumbnails that 404
    return send_file(str(prev_file), mimetype="image/jpeg")


# ---------------------------------------------------------------------------
# Main (standalone mode - also usable via renderhive.py)
# ---------------------------------------------------------------------------
def create_app(discovery=None, blender_path=None):
    """Factory: set up globals and return the Flask app."""
    global DISCOVERY, BLENDER_PATH
    DISCOVERY = discovery
    BLENDER_PATH = blender_path or detect_blender()
    load_state()
    # Restart dependency packing for jobs interrupted by a shutdown
    with LOCK:
        stuck = [j["id"] for j in JOBS.values() if j["status"] == "packing"]
    for job_id in stuck:
        threading.Thread(target=pack_blend_job, args=(job_id,),
                         daemon=True, name=f"pack-{job_id}").start()
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
