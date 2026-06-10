"""Throwaway preview server: boots the coordinator with a seeded fake farm so
the dashboard can be screenshotted. Not part of the product."""
import os
import time
import tempfile

os.environ["RENDERHIVE_DATA_DIR"] = os.path.join(tempfile.gettempdir(), "rh_ui_preview")

import coordinator as coord

now = time.time()

coord.WORKERS.update({
    "ws-a-gpu0": {
        "id": "ws-a-gpu0", "name": "Workstation-A-gpu0", "hostname": "Workstation-A",
        "gpu_name": "RTX 4090 (GPU 0)", "os": "Windows 11", "blender_version": "Blender 5.1.0",
        "status": "rendering", "current_job": "job_a", "current_frame": 37, "progress": 64,
        "frames_done": 142, "render_times": [21.0] * 12, "last_seen": now,
    },
    "ws-b-gpu0": {
        "id": "ws-b-gpu0", "name": "Workstation-B-gpu0", "hostname": "Workstation-B",
        "gpu_name": "RTX 4070 Ti Super (GPU 0)", "os": "Windows 11", "blender_version": "Blender 5.0.2",
        "status": "rendering", "current_job": "job_a", "current_frame": 38, "progress": 22,
        "frames_done": 96, "render_times": [33.0] * 12, "last_seen": now,
    },
    "ws-b-gpu1": {
        "id": "ws-b-gpu1", "name": "Workstation-B-gpu1", "hostname": "Workstation-B",
        "gpu_name": "RTX 4070 Ti Super (GPU 1)", "os": "Windows 11", "blender_version": "Blender 5.0.2",
        "status": "idle", "current_job": None, "current_frame": None, "progress": 0,
        "frames_done": 88, "render_times": [34.0] * 12, "last_seen": now,
    },
    "ws-c-gpu0": {
        "id": "ws-c-gpu0", "name": "RenderBox-C-gpu0", "hostname": "RenderBox-C",
        "gpu_name": "RTX 3090 (GPU 0)", "os": "Windows 10", "blender_version": "Blender 4.4.1",
        "status": "idle", "paused": True, "current_job": None, "current_frame": None,
        "progress": 0, "frames_done": 51, "render_times": [44.0] * 10, "last_seen": now,
    },
    "ws-d-gpu0": {
        "id": "ws-d-gpu0", "name": "Laptop-D-gpu0", "hostname": "Laptop-D",
        "gpu_name": "RTX 5060 Ti (GPU 0)", "os": "Windows 11", "blender_version": "Blender 5.1.0",
        "status": "offline", "current_job": None, "current_frame": None, "progress": 0,
        "frames_done": 12, "render_times": [61.0] * 6, "last_seen": now - 600,
    },
})


def _frames(n, done, rendering=0, failed=0):
    out = {}
    for i in range(1, n + 1):
        if i <= done:
            out[str(i)] = {"status": "done", "worker": "ws-a-gpu0", "render_time": 21.0 + i % 7,
                           "attempts": 1, "progress": 100,
                           "started_at": now - 4000 + i * 25, "finished_at": now - 3970 + i * 25}
        elif i <= done + rendering:
            out[str(i)] = {"status": "assigned", "worker": "ws-b-gpu0", "render_time": None,
                           "attempts": 1, "progress": 40, "started_at": now - 12,
                           "last_progress_at": now}
        elif i <= done + rendering + failed:
            out[str(i)] = {"status": "failed", "worker": None, "render_time": None,
                           "attempts": 3, "progress": 0, "last_error": "CUDA out of memory"}
        else:
            out[str(i)] = {"status": "pending", "worker": None, "render_time": None,
                           "attempts": 0, "progress": 0}
    return out


coord.JOBS.update({
    "job_a": {
        "id": "job_a", "name": "shot_010_final", "blend_filename": "shot_010.blend",
        "blend_path": None, "shared_path": None,
        "frame_start": 1, "frame_end": 120, "frame_step": 1,
        "engine": "CYCLES", "device": "OPTIX", "samples": "256",
        "format": "PNG", "ext": "png", "priority": 8,
        "output_dir": str(coord.OUTPUT_DIR / "shot_010_final"),
        "status": "rendering", "created_at": now - 4200,
        "frames": _frames(120, done=36, rendering=2, failed=1),
    },
    "job_b": {
        "id": "job_b", "name": "turntable_v2", "blend_filename": "turntable.blend",
        "blend_path": None, "shared_path": None,
        "frame_start": 1, "frame_end": 240, "frame_step": 2,
        "engine": "BLENDER_EEVEE_NEXT", "device": "OPTIX", "samples": "",
        "format": "PNG", "ext": "png", "priority": 5,
        "output_dir": str(coord.OUTPUT_DIR / "turntable_v2"),
        "status": "queued", "created_at": now - 1200,
        "frames": _frames(120, done=0),
    },
    "job_c": {
        "id": "job_c", "name": "smoke_sim_preview", "blend_filename": "smoke.blend",
        "blend_path": None, "shared_path": None,
        "frame_start": 1, "frame_end": 60, "frame_step": 1,
        "engine": "CYCLES", "device": "OPTIX", "samples": "64",
        "format": "OPEN_EXR", "ext": "exr", "priority": 2,
        "output_dir": str(coord.OUTPUT_DIR / "smoke_sim_preview"),
        "status": "paused", "created_at": now - 600,
        "frames": _frames(60, done=10),
    },
})


class FakeDiscovery:
    node_id = "self-node-001"
    node_name = "Workstation-A"
    local_ip = "192.168.1.10"
    http_port = 8099

    def get_peers(self):
        t = time.time()
        return [
            {"node_id": "peer-b", "ip": "192.168.1.22", "port": 8080,
             "name": "Workstation-B", "last_seen": t - 2},
            {"node_id": "peer-c", "ip": "192.168.1.31", "port": 8080,
             "name": "RenderBox-C", "last_seen": t - 4},
            {"node_id": "peer-d", "ip": "192.168.1.45", "port": 8080,
             "name": "Laptop-D", "last_seen": t - 28},
        ]

    def broadcast_wake(self):
        pass


coord.DISCOVERY = FakeDiscovery()


def _keep_fresh():
    while True:
        t = time.time()
        for wid, w in coord.WORKERS.items():
            if w["status"] != "offline":
                w["last_seen"] = t
        time.sleep(5)


import threading
threading.Thread(target=_keep_fresh, daemon=True).start()
coord.app.run(host="127.0.0.1", port=8099, threaded=True)
