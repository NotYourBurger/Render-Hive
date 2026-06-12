#!/usr/bin/env python3
"""
RenderHive - Worker
===================
Runs on EVERY PC. Discovers all coordinators on the LAN and pulls
frames from any that have work. For multi-GPU machines, one worker
instance per GPU is spawned automatically.
"""

import os
import re
import io
import sys
import time
import json
import glob
import queue as _queue
import random
import socket
import platform
import argparse
import tempfile
import subprocess
import shutil
import threading
from pathlib import Path

import requests

POLL_IDLE = 3.0

# The script fed to Blender to configure output + GPU per frame
GPU_SETUP_SCRIPT = r'''
import bpy, os
scene = bpy.context.scene

out = os.environ.get("FARM_OUTPUT")
if out:
    scene.render.filepath = out
fmt = os.environ.get("FARM_FORMAT")
if fmt:
    scene.render.image_settings.file_format = fmt
eng = os.environ.get("FARM_ENGINE")
if eng:
    try:
        scene.render.engine = eng
    except Exception as e:
        print("FARM: could not set engine", eng, e)

samples = os.environ.get("FARM_SAMPLES")
if samples and scene.render.engine == "CYCLES":
    try:
        scene.cycles.samples = int(samples)
    except Exception:
        pass

device = os.environ.get("FARM_DEVICE", "")
if scene.render.engine == "CYCLES":
    if device == "CPU":
        scene.cycles.device = "CPU"
    elif device in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = device
        try:
            prefs.refresh_devices()
        except Exception:
            pass
        prefs.get_devices()
        enabled = []
        for d in prefs.devices:
            d.use = (d.type == device)
            if d.use:
                enabled.append(d.name)
        scene.cycles.device = "GPU"
        print("FARM: device", device, "enabled:", enabled)
print("FARM: engine", scene.render.engine, "device", getattr(scene.cycles, "device", "n/a"))
'''


def detect_blender():
    """Return path to the newest installed Blender (4.0+). Falls back to PATH."""
    import re as _re
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
            m = _re.match(r"Blender (\d+)\.(\d+)", d.name)
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


def detect_gpu_name(gpu_index):
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True, stderr=subprocess.DEVNULL)
        names = [n.strip() for n in out.strip().splitlines() if n.strip()]
        if 0 <= gpu_index < len(names):
            return names[gpu_index]
        if names:
            return names[0]
    except Exception:
        pass
    return "GPU"


def detect_gpu_count():
    """Return number of NVIDIA GPUs, or 1 if detection fails."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True, stderr=subprocess.DEVNULL)
        return max(1, len([n for n in out.strip().splitlines() if n.strip()]))
    except Exception:
        return 1


def blender_version(blender):
    try:
        out = subprocess.check_output([blender, "--version"], text=True,
                                      stderr=subprocess.DEVNULL)
        return out.strip().splitlines()[0]
    except Exception:
        return "unknown"


RE_SAMPLE = re.compile(r"Sample\s+(\d+)\s*/\s*(\d+)")
RE_TILES  = re.compile(r"Rendered\s+(\d+)\s*/\s*(\d+)\s+Tiles")


class Worker:
    def __init__(self, gpu_index, device, blender, shared_root,
                 name=None, discovery=None, server=None, home_server=None):
        """
        Args:
            discovery: PeerDiscovery instance (for auto-discovery mode)
            server: explicit coordinator URL (for manual/standalone mode)
            home_server: this node's OWN coordinator URL — honey earned by
                rendering frames (for any coordinator) is banked there
        """
        self.gpu_index = gpu_index
        self.device = device
        self.blender = blender
        self.shared_root = shared_root
        self.discovery = discovery
        self.server = server.rstrip("/") if server else None
        self.home_server = home_server.rstrip("/") if home_server else None
        hostname = socket.gethostname()
        self.gpu_name = detect_gpu_name(gpu_index)
        self.id = name or f"{hostname}-gpu{gpu_index}"
        self.hostname = hostname
        self.os_info = f"{platform.system()} {platform.release()}"
        self.cache = Path(tempfile.gettempdir()) / "renderhive_cache" / self.id
        self.cache.mkdir(parents=True, exist_ok=True)
        self.setup_script = self.cache / "gpu_setup.py"
        self.setup_script.write_text(GPU_SETUP_SCRIPT)
        self.bversion = blender_version(blender)
        self._registered_with = set()  # coordinator URLs we've registered with
        self._wake_event = threading.Event()
        self._prefetch_queue: list = []         # list of (coord_url, assignment) tuples
        self._sse_event_queue = _queue.Queue()  # events pushed from coordinator SSE stream

    def _worker_info(self):
        """Worker info dict sent with registration and /next requests."""
        return {
            "id": self.id, "name": self.id,
            "hostname": self.hostname,
            "gpu_name": f"{self.gpu_name} (GPU {self.gpu_index})",
            "os": self.os_info,
            "blender_version": self.bversion,
        }

    def _get_coordinators(self):
        """Return list of coordinator URLs to poll."""
        if self.server:
            return [self.server]
        if self.discovery:
            return self.discovery.get_all_coordinator_urls()
        return ["http://127.0.0.1:8080"]

    def register_with(self, coord_url):
        """Register with a coordinator if not already done."""
        if coord_url in self._registered_with:
            return
        try:
            requests.post(f"{coord_url}/api/workers/register",
                          json=self._worker_info(), timeout=10)
            self._registered_with.add(coord_url)
        except requests.RequestException:
            pass

    def heartbeat(self, coord_url, status, progress=0):
        try:
            requests.post(f"{coord_url}/api/workers/{self.id}/heartbeat",
                          json={"status": status, "progress": progress},
                          timeout=10)
        except requests.RequestException:
            pass

    def poll_for_work(self):
        """Poll all known coordinators for work. Returns (coord_url, assignment) or (None, None)."""
        coordinators = self._get_coordinators()
        random.shuffle(coordinators)  # distribute load
        for coord_url in coordinators:
            try:
                self.register_with(coord_url)
                r = requests.post(
                    f"{coord_url}/api/workers/{self.id}/next",
                    json=self._worker_info(), timeout=10)
                data = r.json()
                if data.get("reregister"):
                    self._registered_with.discard(coord_url)
                    self.register_with(coord_url)
                    continue
                if data.get("assignment"):
                    return coord_url, data["assignment"]
            except requests.RequestException:
                self._registered_with.discard(coord_url)
            except Exception:
                pass
        return None, None

    def report_progress(self, coord_url, job_id, frame, progress):
        try:
            r = requests.post(f"{coord_url}/api/workers/{self.id}/progress",
                              json={"job_id": job_id, "frame": frame,
                                    "progress": progress}, timeout=10)
            return r.json()
        except requests.RequestException:
            return None

    def report_fail(self, coord_url, job_id, frame, log):
        try:
            requests.post(f"{coord_url}/api/workers/{self.id}/fail",
                          json={"job_id": job_id, "frame": frame, "log": log},
                          timeout=20)
        except requests.RequestException:
            pass

    def _listen_sse(self, coord_url):
        """Background daemon: maintain SSE stream and feed events to _sse_event_queue."""
        try:
            with requests.get(f"{coord_url}/api/workers/{self.id}/stream",
                             stream=True, timeout=(5, None)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if line and line.startswith("data: "):
                        try:
                            event = json.loads(line[6:])
                            self._sse_event_queue.put(("sse", coord_url, event))
                        except Exception:
                            pass
        except Exception:
            pass
        self._sse_event_queue.put(("sse_disconnected", coord_url, None))

    def upload(self, coord_url, job_id, frame, render_time, image_path):
        """Upload a finished frame. Retries; returns True on success."""
        for attempt in range(3):
            try:
                data = {"job_id": job_id, "frame": frame,
                        "render_time": render_time}
                if image_path and Path(image_path).exists():
                    with open(image_path, "rb") as fh:
                        r = requests.post(
                            f"{coord_url}/api/workers/{self.id}/complete",
                            data=data,
                            files={"image": (Path(image_path).name, fh)},
                            timeout=300)
                else:
                    r = requests.post(
                        f"{coord_url}/api/workers/{self.id}/complete",
                        data=data, timeout=300)
                r.raise_for_status()
                try:
                    earned = int(r.json().get("honey", 0))
                except Exception:
                    earned = 0
                if earned > 0:
                    self.bank_honey(earned)
                return True
            except (requests.RequestException, OSError) as e:
                print(f"  upload attempt {attempt + 1}/3 failed: {e}")
                time.sleep(2 * (attempt + 1))
        return False

    def bank_honey(self, amount):
        """Credit honey earned for a rendered frame to this node's own
        coordinator — the jar that gates job submission lives there. Best
        effort: a node without a local coordinator just doesn't bank."""
        if not self.home_server:
            return
        try:
            r = requests.post(f"{self.home_server}/api/honey/earn",
                              json={"amount": amount, "worker": self.id},
                              timeout=10)
            d = r.json()
            if d.get("ok"):
                print(f"  +{amount} honey (jar: {d.get('balance')})")
        except (requests.RequestException, ValueError):
            pass

    def resolve_blend(self, coord_url, assign):
        # A packed project is self-contained — prefer it even when the job
        # was submitted via shared path (the share may not be mounted here)
        if assign.get("packed"):
            return self.resolve_packed_blend(coord_url, assign)
        if assign.get("shared_path"):
            sp = assign["shared_path"]
            if self.shared_root and not os.path.isabs(sp):
                sp = os.path.join(self.shared_root, sp)
            if Path(sp).exists():
                return sp
            print(f"  ! shared_path not found: {sp} (falling back to download)")
        local = self.cache / f"{assign['job_id']}_{assign['blend_filename']}"
        if not local.exists():
            print(f"  downloading {assign['blend_filename']} ...")
            with requests.get(coord_url + assign["blend_url"],
                              stream=True, timeout=600) as r:
                r.raise_for_status()
                tmp = local.with_suffix(local.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                tmp.rename(local)
        return str(local)

    def resolve_packed_blend(self, coord_url, assign):
        """Download the packed project zip (blend + localized dependencies),
        extract it into the cache and return the path of the blend inside.
        The blend's //deps/... relative paths then resolve locally."""
        import zipfile
        jobdir = self.cache / f"{assign['job_id']}_pack"
        blend_local = jobdir / assign["blend_filename"]
        if blend_local.exists():
            return str(blend_local)
        print(f"  downloading packed project for job {assign['job_id']} ...")

        last_ping = [time.time()]

        def ping():
            # Packed projects can be many GB. Without progress updates the
            # coordinator's stall detector would requeue the frame while we
            # are still transferring — keep its last_progress_at fresh.
            if time.time() - last_ping[0] > 10:
                self.report_progress(coord_url, assign["job_id"],
                                     assign["frame"], 0)
                last_ping[0] = time.time()

        ztmp = self.cache / f"{assign['job_id']}_pack.zip.part"
        got = 0
        with requests.get(coord_url + assign["blend_url"],
                          stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(ztmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    ping()
        print(f"  downloaded {got / 1048576:.0f} MB — extracting ...")
        # Extract to a temp dir and rename, so a crash mid-extract can't
        # leave a half-complete project that looks usable
        tmpdir = jobdir.with_name(jobdir.name + ".part")
        shutil.rmtree(tmpdir, ignore_errors=True)
        with zipfile.ZipFile(ztmp) as z:
            for member in z.infolist():
                z.extract(member, tmpdir)
                ping()
        ztmp.unlink()
        shutil.rmtree(jobdir, ignore_errors=True)
        tmpdir.rename(jobdir)
        if not blend_local.exists():
            raise RuntimeError(
                f"packed project did not contain {assign['blend_filename']}")
        return str(blend_local)

    def render_frame(self, coord_url, assign):
        """Render one frame; on any unexpected error tell the coordinator so
        the frame is requeued immediately instead of waiting for a timeout."""
        try:
            self._render_frame_inner(coord_url, assign)
        except Exception as e:
            print(f"  worker error on frame {assign.get('frame')}: {e}")
            self.report_fail(coord_url, assign["job_id"], assign["frame"],
                             f"worker exception: {e}")

    def _render_frame_inner(self, coord_url, assign):
        job_id = assign["job_id"]
        frame = assign["frame"]
        print(f"\n[{time.strftime('%H:%M:%S')}] frame {frame} (job {job_id}) from {coord_url}")

        blend = self.resolve_blend(coord_url, assign)
        outdir = self.cache / "out"
        outdir.mkdir(exist_ok=True)
        for old in outdir.glob("*"):
            old.unlink()
        out_pattern = str(outdir / "frame_####")

        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = str(self.gpu_index)
        env["FARM_OUTPUT"] = out_pattern
        env["FARM_FORMAT"] = assign.get("format") or "PNG"
        env["FARM_DEVICE"] = self.device
        if assign.get("engine"):
            env["FARM_ENGINE"] = assign["engine"]
        if assign.get("samples"):
            env["FARM_SAMPLES"] = str(assign["samples"])

        cmd = [self.blender, "-b", blend,
               "-P", str(self.setup_script),
               "-f", str(frame)]

        start = time.time()
        # Use binary mode + explicit UTF-8 decoding to avoid charmap errors
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        stdout_reader = io.TextIOWrapper(proc.stdout, encoding="utf-8",
                                          errors="replace", newline="")

        was_cancelled = [False]
        last_progress = [0]
        render_done = threading.Event()

        def _heartbeat_and_cancel_check():
            """Background thread: keeps all coordinators updated and checks for cancel."""
            while not render_done.wait(timeout=5):
                # Keep this worker visible on ALL coordinators (fixes "offline" display bug)
                for cu in self._get_coordinators():
                    if cu != coord_url:
                        try:
                            requests.post(
                                f"{cu}/api/workers/{self.id}/heartbeat",
                                json={"status": "rendering",
                                      "progress": last_progress[0]},
                                timeout=5,
                            )
                        except Exception:
                            pass
                # Check SSE cancel events (faster path)
                while True:
                    try:
                        src, cu, event = self._sse_event_queue.get_nowait()
                        if src == "sse" and event and event.get("type") == "cancel":
                            if event.get("job_id") == job_id and not was_cancelled[0]:
                                print(f"  [CANCEL] frame {frame} cancelled via SSE")
                                was_cancelled[0] = True
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                        else:
                            self._sse_event_queue.put((src, cu, event))
                    except _queue.Empty:
                        break
                # Check cancel with the assigning coordinator
                if not was_cancelled[0]:
                    resp = self.report_progress(
                        coord_url, job_id, frame, last_progress[0])
                    if resp and resp.get("cancel"):
                        print(f"  [CANCEL] frame {frame} cancelled — killing Blender")
                        was_cancelled[0] = True
                        try:
                            proc.kill()
                        except Exception:
                            pass

        hb_thread = threading.Thread(target=_heartbeat_and_cancel_check,
                                     daemon=True, name=f"hb-{self.id}-f{frame}")
        hb_thread.start()

        log_tail = []
        last_report = 0
        for line in stdout_reader:
            if was_cancelled[0]:
                break
            log_tail.append(line)
            if len(log_tail) > 400:
                log_tail.pop(0)
            m = RE_SAMPLE.search(line) or RE_TILES.search(line)
            if m and time.time() - last_report > 1.0:
                pct = int(100 * int(m.group(1)) / max(1, int(m.group(2))))
                last_progress[0] = pct
                resp = self.report_progress(coord_url, job_id, frame, pct)
                if resp:
                    if resp.get("cancel"):
                        print(f"  [CANCEL] frame {frame} cancelled — killing Blender")
                        was_cancelled[0] = True
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    elif resp.get("prefetch"):
                        for pa in (resp["prefetch"] or []):
                            if len(self._prefetch_queue) < 4:
                                self._prefetch_queue.append((coord_url, pa))
                                print(f"  [PREFETCH] queued frame {pa['frame']}")
                last_report = time.time()

        proc.wait()
        render_done.set()
        hb_thread.join(timeout=2)
        elapsed = round(time.time() - start, 1)

        if was_cancelled[0]:
            print(f"  [CANCEL] Frame {frame} render stopped after {elapsed}s")
            self._prefetch_queue.clear()
            return  # Coordinator already marked the frame as cancelled

        produced = sorted(outdir.glob("frame_*"))
        if proc.returncode != 0 or not produced:
            log = "".join(log_tail)
            print(f"  FAILED (rc={proc.returncode}, {elapsed}s)")
            self.report_fail(coord_url, job_id, frame, log)
            return
        print(f"  done in {elapsed}s -> uploading {produced[0].name}")
        if not self.upload(coord_url, job_id, frame, elapsed, str(produced[0])):
            print(f"  upload of frame {frame} failed — reporting failure")
            self.report_fail(coord_url, job_id, frame,
                             "frame rendered but upload to coordinator failed")

    def wake(self):
        """Signal this worker to immediately poll for work."""
        self._wake_event.set()

    def run(self):
        print(f"Worker '{self.id}'  GPU={self.gpu_name} (index {self.gpu_index})"
              f"  device={self.device}")
        print(f"Blender: {self.blender} [{self.bversion}]")

        # Wait for at least one coordinator to be available
        print("  waiting for coordinator...")
        while True:
            coordinators = self._get_coordinators()
            for coord_url in coordinators:
                try:
                    self.register_with(coord_url)
                    print(f"  registered with {coord_url}")
                    break
                except Exception:
                    pass
            else:
                time.sleep(2)
                continue
            break

        print("  polling for work (SSE push enabled)...")
        sse_started_for: set = set()

        def _ensure_sse(cu):
            if cu not in sse_started_for:
                sse_started_for.add(cu)
                threading.Thread(target=self._listen_sse, args=(cu,),
                                 daemon=True, name=f"sse-{self.id}").start()

        while True:
            try:
                # Priority 1: prefetch queue (zero latency between frames)
                if self._prefetch_queue:
                    cu, assign = self._prefetch_queue.pop(0)
                    print(f"  [PREFETCH] starting frame {assign['frame']} immediately")
                    self.render_frame(cu, assign)
                    continue

                # Priority 2: process SSE events (non-blocking drain)
                try:
                    src, cu, event = self._sse_event_queue.get_nowait()
                    if src == "sse_disconnected":
                        sse_started_for.discard(cu)
                    elif src == "sse" and event:
                        if event["type"] == "work_available":
                            coord_url, assign = self.poll_for_work()
                            if assign:
                                _ensure_sse(coord_url)
                                self.render_frame(coord_url, assign)
                    continue
                except _queue.Empty:
                    pass

                # Priority 3: ensure SSE connected to all known coordinators
                for cu in self._get_coordinators():
                    _ensure_sse(cu)

                # Priority 4: poll (first frame / belt-and-suspenders before SSE warms up)
                coord_url, assign = self.poll_for_work()
                if assign:
                    self.render_frame(coord_url, assign)
                    continue

                # Idle: heartbeat all coordinators, then wait for SSE event or timeout
                for cu in self._get_coordinators():
                    self.heartbeat(cu, "idle")
                if self.discovery:
                    self.discovery.wait_for_wake(timeout=POLL_IDLE)
                else:
                    try:
                        src, cu, event = self._sse_event_queue.get(timeout=POLL_IDLE)
                        self._sse_event_queue.put((src, cu, event))
                    except _queue.Empty:
                        pass

            except KeyboardInterrupt:
                print("\nstopping.")
                return
            except requests.RequestException as e:
                print("  network error:", e)
                time.sleep(POLL_IDLE)
            except Exception as e:
                print("  worker error:", e)
                time.sleep(POLL_IDLE)


def main():
    ap = argparse.ArgumentParser(description="RenderHive worker")
    ap.add_argument("--server", default=None,
                    help="coordinator URL (e.g. http://192.168.1.10:8080). "
                         "If omitted, uses auto-discovery.")
    ap.add_argument("--gpu", type=int, default=0,
                    help="GPU index to use (run one worker per GPU)")
    ap.add_argument("--device", default="OPTIX",
                    choices=["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL", "CPU"],
                    help="Cycles backend (OPTIX is fastest on RTX cards)")
    ap.add_argument("--name", default=None, help="override worker name")
    ap.add_argument("--blender", default=None, help="path to blender executable")
    ap.add_argument("--shared-root", default=None,
                    help="local mount point of the shared project folder, if used")
    ap.add_argument("--discovery-port", type=int, default=5678,
                    help="UDP port for peer discovery")
    ap.add_argument("--http-port", type=int, default=8080,
                    help="HTTP port of the local coordinator (for discovery)")
    args = ap.parse_args()

    blender = args.blender or detect_blender()
    if not blender:
        print("ERROR: could not find Blender. Pass --blender <path-to-blender.exe>")
        sys.exit(1)

    discovery = None
    if not args.server:
        # Auto-discovery mode
        import uuid
        from discovery import PeerDiscovery
        node_id = uuid.uuid4().hex[:12]
        discovery = PeerDiscovery(node_id, args.http_port,
                                  discovery_port=args.discovery_port)
        discovery.start()
        print(f"  discovery started on UDP port {args.discovery_port}")

    Worker(gpu_index=args.gpu, device=args.device, blender=blender,
           shared_root=args.shared_root, name=args.name,
           discovery=discovery, server=args.server,
           home_server=f"http://127.0.0.1:{args.http_port}").run()


if __name__ == "__main__":
    main()
