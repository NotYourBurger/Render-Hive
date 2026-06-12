"""Smoke-test helper (manual): run the coordinator's real pack_blend_job on a
blend and report per-phase timing and the resulting zip.
Run:  python tests/_smoke_pack_timing.py <blend_path>
Uses an isolated temp data dir; does not touch Documents/RenderHive.
"""
import os
import sys
import time
import tempfile
from pathlib import Path

os.environ["RENDERHIVE_DATA_DIR"] = os.path.join(
    tempfile.gettempdir(), "rh_pack_timing_data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordinator as coord  # noqa: E402

blend = sys.argv[1]
job_id = "timing1"
job = {
    "id": job_id, "name": "timing", "status": "packing",
    "blend_path": None, "shared_path": blend,
    "blend_filename": os.path.basename(blend),
    "frames": {"1": {"status": "pending", "worker": None,
                     "render_time": None, "attempts": 0, "progress": 0}},
    "frame_start": 1, "frame_end": 1, "frame_step": 1,
    "engine": "", "device": "", "samples": "", "format": "PNG", "ext": "png",
    "output_dir": str(coord.OUTPUT_DIR / "timing"),
    "priority": 5, "created_at": time.time(),
}
with coord.LOCK:
    coord.JOBS[job_id] = job

t0 = time.time()
coord.pack_blend_job(job_id)
total = time.time() - t0

print(f"TIMING: total {total:.1f}s")
print(f"  status     : {job['status']}")
print(f"  pack_error : {job.get('pack_error')}")
zp = job.get("packed_zip")
if zp and os.path.exists(zp):
    print(f"  zip        : {zp}  ({os.path.getsize(zp) / 1048576:.0f} MB)")
    os.remove(zp)
