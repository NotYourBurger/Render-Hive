"""
RenderHive – Coordinator Unit Tests
=====================================
Tests every API endpoint and core business logic using Flask's test client.
Run with:  pytest tests/test_coordinator.py -v
"""
import io
import json
import sys
import os
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Make repo root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordinator as coord
from coordinator import app, LOCK


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    """Clear all shared state before every test."""
    with LOCK:
        coord.JOBS.clear()
        coord.WORKERS.clear()
    coord.DISCOVERY = None
    coord.BLENDER_PATH = None
    yield


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_worker(client, wid="w1", hostname="host1", gpu="RTX 4090"):
    return client.post("/api/workers/register", json={
        "id": wid, "name": wid, "hostname": hostname,
        "gpu_name": gpu, "os": "Windows 11", "blender_version": "Blender 5.0",
    })


def _submit_job(client, name="job1", frame_start=1, frame_end=5):
    """Submit a minimal job directly into JOBS (bypassing file upload)."""
    job_id = f"test_{name}"
    job = {
        "id": job_id, "name": name,
        "blend_filename": "scene.blend", "blend_path": None,
        "shared_path": None,
        "frame_start": frame_start, "frame_end": frame_end, "frame_step": 1,
        "engine": "CYCLES", "device": "OPTIX", "samples": "128",
        "format": "PNG", "ext": "png",
        "output_dir": str(Path(coord.OUTPUT_DIR) / name),
        "status": "queued", "created_at": time.time(),
        "frames": {
            str(f): {"status": "pending", "worker": None,
                     "render_time": None, "attempts": 0, "progress": 0}
            for f in range(frame_start, frame_end + 1)
        },
    }
    with LOCK:
        coord.JOBS[job_id] = job
    return job_id, job


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200

    def test_structure(self, client):
        r = client.get("/api/status")
        d = r.get_json()
        assert "workers" in d
        assert "jobs" in d
        assert "peers" in d
        assert "node_id" in d
        assert "node_name" in d

    def test_empty_state(self, client):
        d = client.get("/api/status").get_json()
        assert d["workers"] == []
        assert d["jobs"] == []
        assert d["peers"] == []

    def test_shows_registered_workers(self, client):
        _register_worker(client, "w1")
        d = client.get("/api/status").get_json()
        assert len(d["workers"]) == 1
        assert d["workers"][0]["id"] == "w1"

    def test_worker_offline_after_timeout(self, client):
        _register_worker(client, "w1")
        # Force last_seen into the past
        with LOCK:
            coord.WORKERS["w1"]["last_seen"] -= coord.WORKER_TIMEOUT + 1
        d = client.get("/api/status").get_json()
        assert d["workers"][0]["status"] == "offline"

    def test_online_workers_not_counted_as_offline(self, client):
        _register_worker(client, "w1")
        _register_worker(client, "w2")
        d = client.get("/api/status").get_json()
        online = [w for w in d["workers"] if w["status"] != "offline"]
        assert len(online) == 2

    def test_jobs_shown_in_status(self, client):
        _submit_job(client, "my_job")
        d = client.get("/api/status").get_json()
        assert len(d["jobs"]) == 1
        assert d["jobs"][0]["name"] == "my_job"


# ---------------------------------------------------------------------------
# Worker registration
# ---------------------------------------------------------------------------

class TestWorkerRegistration:
    def test_register_new_worker(self, client):
        r = _register_worker(client, "w1")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_worker_appears_in_state(self, client):
        _register_worker(client, "w42")
        assert "w42" in coord.WORKERS

    def test_register_updates_existing_worker(self, client):
        _register_worker(client, "w1", gpu="GTX 1080")
        _register_worker(client, "w1", gpu="RTX 4090")
        assert coord.WORKERS["w1"]["gpu_name"] == "RTX 4090"

    def test_register_preserves_frames_done(self, client):
        _register_worker(client, "w1")
        with LOCK:
            coord.WORKERS["w1"]["frames_done"] = 99
        _register_worker(client, "w1")
        assert coord.WORKERS["w1"]["frames_done"] == 99

    def test_register_missing_id_returns_400(self, client):
        r = client.post("/api/workers/register", json={"name": "no_id"})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Frame assignment (/api/workers/<id>/next)
# ---------------------------------------------------------------------------

class TestFrameAssignment:
    def test_assigns_first_pending_frame(self, client):
        _register_worker(client, "w1")
        _submit_job(client, "job1", frame_start=1, frame_end=3)
        r = client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
        d = r.get_json()
        assert d["assignment"] is not None
        assert d["assignment"]["frame"] == 1

    def test_no_assignment_when_no_jobs(self, client):
        _register_worker(client, "w1")
        r = client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is None

    def test_frame_marked_assigned_after_pull(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert coord.JOBS[job_id]["frames"]["1"]["status"] == "assigned"

    def test_worker_status_becomes_rendering(self, client):
        _register_worker(client, "w1")
        _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert coord.WORKERS["w1"]["status"] == "rendering"

    def test_two_workers_get_different_frames(self, client):
        _register_worker(client, "w1")
        _register_worker(client, "w2")
        _submit_job(client, "job1", 1, 5)
        r1 = client.post("/api/workers/w1/next",
                         json={"id": "w1", "name": "w1"}).get_json()
        r2 = client.post("/api/workers/w2/next",
                         json={"id": "w2", "name": "w2"}).get_json()
        assert r1["assignment"]["frame"] != r2["assignment"]["frame"]

    def test_skips_cancelled_job(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post(f"/api/jobs/{job_id}/cancel")
        r = client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is None

    def test_skips_done_job(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 2)
        with LOCK:
            job["status"] = "done"
            for f in job["frames"].values():
                f["status"] = "done"
        r = client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is None

    def test_auto_registers_unknown_worker(self, client):
        _submit_job(client, "job1", 1, 2)
        r = client.post("/api/workers/new_worker/next",
                        json={"id": "new_worker", "name": "new_worker",
                              "hostname": "pc", "gpu_name": "RTX", "os": "Win"})
        assert "new_worker" in coord.WORKERS
        assert r.get_json()["assignment"] is not None

    def test_unknown_worker_without_info_requests_reregister(self, client):
        r = client.post("/api/workers/ghost/next", json={})
        assert r.get_json().get("reregister") is True


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

class TestProgressReporting:
    def test_progress_updates_worker(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/progress",
                    json={"job_id": job_id, "frame": 1, "progress": 55})
        assert coord.WORKERS["w1"]["progress"] == 55

    def test_progress_updates_frame(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/progress",
                    json={"job_id": job_id, "frame": 1, "progress": 72})
        assert job["frames"]["1"]["progress"] == 72

    def test_progress_returns_ok(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 50})
        d = r.get_json()
        assert d["ok"] is True
        assert d["cancel"] is False

    def test_progress_returns_cancel_when_job_cancelled(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post(f"/api/jobs/{job_id}/cancel")
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 30})
        assert r.get_json()["cancel"] is True

    def test_progress_returns_cancel_when_frame_cancelled(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        # Manually cancel just frame 1
        with LOCK:
            job["frames"]["1"]["status"] = "cancelled"
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 40})
        assert r.get_json()["cancel"] is True

    def test_progress_updates_last_seen(self, client):
        _register_worker(client, "w1")
        with LOCK:
            coord.WORKERS["w1"]["last_seen"] = 0
        _submit_job(client, "j1", 1, 1)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/progress",
                    json={"job_id": "test_j1", "frame": 1, "progress": 10})
        assert coord.WORKERS["w1"]["last_seen"] > 0


# ---------------------------------------------------------------------------
# Frame completion
# ---------------------------------------------------------------------------

class TestFrameCompletion:
    def test_complete_marks_frame_done(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete", data={
            "job_id": job_id, "frame": 1, "render_time": 12.5,
        })
        assert job["frames"]["1"]["status"] == "done"
        assert job["frames"]["1"]["render_time"] == 12.5

    def test_complete_worker_becomes_idle(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 5.0})
        assert coord.WORKERS["w1"]["status"] == "idle"
        assert coord.WORKERS["w1"]["current_job"] is None

    def test_complete_increments_frames_done(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 5.0})
        assert coord.WORKERS["w1"]["frames_done"] == 1

    def test_complete_cancelled_frame_does_not_mark_done(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        # Cancel the job
        client.post(f"/api/jobs/{job_id}/cancel")
        # Worker still calls complete (it didn't receive cancel signal in time)
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 5.0})
        # Frame must remain cancelled, not overwritten to done
        assert job["frames"]["1"]["status"] == "cancelled"

    def test_complete_cancelled_frame_still_idles_worker(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post(f"/api/jobs/{job_id}/cancel")
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 5.0})
        assert coord.WORKERS["w1"]["status"] == "idle"

    def test_complete_cancelled_frame_does_not_count_frames_done(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post(f"/api/jobs/{job_id}/cancel")
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 5.0})
        assert coord.WORKERS["w1"]["frames_done"] == 0

    def test_all_frames_done_marks_job_done(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 2)
        # Frame 1
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 3.0})
        # Frame 2
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 2, "render_time": 3.0})
        assert job["status"] == "done"

    def test_complete_unknown_job_returns_404(self, client):
        _register_worker(client, "w1")
        r = client.post("/api/workers/w1/complete",
                        data={"job_id": "nonexistent", "frame": 1,
                              "render_time": 0})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Frame failure
# ---------------------------------------------------------------------------

class TestFrameFailure:
    def test_fail_requeues_frame(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/fail",
                    json={"job_id": job_id, "frame": 1, "log": "error"})
        assert job["frames"]["1"]["status"] == "pending"

    def test_fail_increments_attempts(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/fail",
                    json={"job_id": job_id, "frame": 1, "log": "err"})
        assert job["frames"]["1"]["attempts"] == 1

    def test_fail_after_max_attempts_marks_failed(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 1)
        # Exhaust retries
        for _ in range(coord.MAX_FRAME_ATTEMPTS):
            client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
            client.post("/api/workers/w1/fail",
                        json={"job_id": job_id, "frame": 1, "log": "err"})
        assert job["frames"]["1"]["status"] == "failed"

    def test_fail_idles_worker(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/fail",
                    json={"job_id": job_id, "frame": 1, "log": "err"})
        assert coord.WORKERS["w1"]["status"] == "idle"

    def test_fail_stores_error_log(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/fail",
                    json={"job_id": job_id, "frame": 1, "log": "CRASH: oom"})
        assert "CRASH: oom" in job["frames"]["1"]["last_error"]

    def test_all_frames_failed_marks_job_failed(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 1)
        for _ in range(coord.MAX_FRAME_ATTEMPTS):
            client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
            client.post("/api/workers/w1/fail",
                        json={"job_id": job_id, "frame": 1, "log": "err"})
        assert job["status"] == "failed"


# ---------------------------------------------------------------------------
# Job cancellation
# ---------------------------------------------------------------------------

class TestJobCancellation:
    def test_cancel_marks_job_cancelled(self, client):
        job_id, job = _submit_job(client, "job1", 1, 5)
        r = client.post(f"/api/jobs/{job_id}/cancel")
        assert r.get_json()["ok"] is True
        assert job["status"] == "cancelled"

    def test_cancel_marks_pending_frames_cancelled(self, client):
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post(f"/api/jobs/{job_id}/cancel")
        for fr in job["frames"].values():
            assert fr["status"] == "cancelled"

    def test_cancel_marks_assigned_frames_cancelled(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post(f"/api/jobs/{job_id}/cancel")
        assert job["frames"]["1"]["status"] == "cancelled"

    def test_cancel_preserves_done_frames(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        # Complete frame 1
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 2.0})
        # Cancel job
        client.post(f"/api/jobs/{job_id}/cancel")
        # Frame 1 should remain done
        assert job["frames"]["1"]["status"] == "done"
        # Frames 2 and 3 should be cancelled
        assert job["frames"]["2"]["status"] == "cancelled"

    def test_cancel_nonexistent_job_returns_404(self, client):
        r = client.post("/api/jobs/nope/cancel")
        assert r.status_code == 404

    def test_cancel_stops_new_frame_assignment(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 5)
        client.post(f"/api/jobs/{job_id}/cancel")
        r = client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is None

    def test_progress_after_cancel_returns_cancel_true(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post(f"/api/jobs/{job_id}/cancel")
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 50})
        assert r.get_json()["cancel"] is True


# ---------------------------------------------------------------------------
# Job retry
# ---------------------------------------------------------------------------

class TestJobRetry:
    def test_retry_requeues_failed_frames(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 1)
        for _ in range(coord.MAX_FRAME_ATTEMPTS):
            client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
            client.post("/api/workers/w1/fail",
                        json={"job_id": job_id, "frame": 1, "log": "err"})
        assert job["frames"]["1"]["status"] == "failed"
        r = client.post(f"/api/jobs/{job_id}/retry")
        assert r.get_json()["ok"] is True
        assert job["frames"]["1"]["status"] == "pending"

    def test_retry_resets_attempts_count(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 1)
        for _ in range(coord.MAX_FRAME_ATTEMPTS):
            client.post("/api/workers/w1/next",
                        json={"id": "w1", "name": "w1"})
            client.post("/api/workers/w1/fail",
                        json={"job_id": job_id, "frame": 1, "log": "err"})
        client.post(f"/api/jobs/{job_id}/retry")
        assert job["frames"]["1"]["attempts"] == 0

    def test_retry_counts_retried_frames(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 2)
        # Fail both frames to max
        for fr_num in [1, 2]:
            for _ in range(coord.MAX_FRAME_ATTEMPTS):
                client.post("/api/workers/w1/next",
                            json={"id": "w1", "name": "w1"})
                client.post("/api/workers/w1/fail",
                            json={"job_id": job_id, "frame": fr_num, "log": "err"})
        r = client.post(f"/api/jobs/{job_id}/retry")
        assert r.get_json()["retried"] == 2

    def test_retry_nonexistent_returns_404(self, client):
        r = client.post("/api/jobs/ghost/retry")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Job deletion
# ---------------------------------------------------------------------------

class TestJobDeletion:
    def test_delete_removes_job(self, client):
        job_id, _ = _submit_job(client, "job1")
        r = client.post(f"/api/jobs/{job_id}/delete")
        assert r.get_json()["ok"] is True
        assert job_id not in coord.JOBS

    def test_delete_nonexistent_returns_ok_false(self, client):
        r = client.post("/api/jobs/nope/delete")
        assert r.get_json()["ok"] is False

    def test_deleted_job_not_in_status(self, client):
        job_id, _ = _submit_job(client, "job1")
        client.post(f"/api/jobs/{job_id}/delete")
        d = client.get("/api/status").get_json()
        assert all(j["id"] != job_id for j in d["jobs"])


# ---------------------------------------------------------------------------
# Job detail endpoint
# ---------------------------------------------------------------------------

class TestJobDetail:
    def test_returns_frames(self, client):
        job_id, _ = _submit_job(client, "job1", 1, 5)
        r = client.get(f"/api/jobs/{job_id}")
        d = r.get_json()
        assert "frames" in d
        assert len(d["frames"]) == 5

    def test_frames_sorted_by_number(self, client):
        job_id, _ = _submit_job(client, "job1", 1, 5)
        frames = client.get(f"/api/jobs/{job_id}").get_json()["frames"]
        numbers = [f["frame"] for f in frames]
        assert numbers == sorted(numbers)

    def test_summary_included(self, client):
        job_id, _ = _submit_job(client, "job1", 1, 5)
        d = client.get(f"/api/jobs/{job_id}").get_json()
        assert "summary" in d
        assert d["summary"]["id"] == job_id

    def test_nonexistent_returns_404(self, client):
        r = client.get("/api/jobs/ghost")
        assert r.status_code == 404

    def test_frame_statuses_correct(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 2.0})
        frames = client.get(f"/api/jobs/{job_id}").get_json()["frames"]
        status_map = {f["frame"]: f["status"] for f in frames}
        assert status_map[1] == "done"
        assert status_map[2] == "pending"


# ---------------------------------------------------------------------------
# Worker heartbeat
# ---------------------------------------------------------------------------

class TestWorkerHeartbeat:
    def test_heartbeat_updates_last_seen(self, client):
        _register_worker(client, "w1")
        with LOCK:
            coord.WORKERS["w1"]["last_seen"] = 0
        client.post("/api/workers/w1/heartbeat", json={"status": "idle"})
        assert coord.WORKERS["w1"]["last_seen"] > 0

    def test_heartbeat_unknown_worker_requests_reregister(self, client):
        r = client.post("/api/workers/unknown/heartbeat", json={"status": "idle"})
        assert r.get_json().get("reregister") is True

    def test_heartbeat_updates_status(self, client):
        _register_worker(client, "w1")
        client.post("/api/workers/w1/heartbeat", json={"status": "rendering"})
        assert coord.WORKERS["w1"]["status"] == "rendering"


# ---------------------------------------------------------------------------
# Reap dead workers & frames
# ---------------------------------------------------------------------------

class TestReaper:
    def test_reap_marks_stale_worker_offline(self, client):
        _register_worker(client, "w1")
        with LOCK:
            coord.WORKERS["w1"]["last_seen"] -= coord.WORKER_TIMEOUT + 1
        # Manually run reaper logic (synchronously, single iteration)
        now = time.time()
        with LOCK:
            for w in coord.WORKERS.values():
                if w["status"] != "offline" and now - w["last_seen"] > coord.WORKER_TIMEOUT:
                    w["status"] = "offline"
        assert coord.WORKERS["w1"]["status"] == "offline"

    def test_reap_requeues_stale_assigned_frame(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        # Age the frame past the timeout
        with LOCK:
            job["frames"]["1"]["started_at"] = time.time() - coord.FRAME_TIMEOUT - 1
        now = time.time()
        with LOCK:
            for jb in coord.JOBS.values():
                if jb["status"] not in ("rendering", "queued"):
                    continue
                for fno, fr in jb["frames"].items():
                    if fr["status"] != "assigned":
                        continue
                    stale_frame = now - fr.get("started_at", now) > coord.FRAME_TIMEOUT
                    if stale_frame:
                        if fr.get("attempts", 0) >= coord.MAX_FRAME_ATTEMPTS:
                            fr["status"] = "failed"
                        else:
                            fr["status"] = "pending"
                        fr["worker"] = None
        assert job["frames"]["1"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Peers endpoint
# ---------------------------------------------------------------------------

class TestPeersEndpoint:
    def test_peers_returns_empty_without_discovery(self, client):
        r = client.get("/api/peers")
        assert r.status_code == 200
        assert r.get_json()["peers"] == []

    def test_status_includes_worker_active_count_for_peers(self, client):
        # Simulate a peer worker registered with our coordinator
        _register_worker(client, "peer-gpu0", hostname="PeerNode")
        # Simulate peer discovery
        mock_discovery = MagicMock()
        mock_discovery.get_peers.return_value = [{
            "node_id": "peer123", "ip": "192.168.1.50",
            "port": 8080, "name": "PeerNode",
            "last_seen": time.time(),
        }]
        mock_discovery.node_id = "self001"
        mock_discovery.node_name = "Self"
        coord.DISCOVERY = mock_discovery

        d = client.get("/api/status").get_json()
        peer = next(p for p in d["peers"] if p["node_id"] == "peer123")
        assert peer["workers_active"] == 1

    def teardown_method(self, method):
        coord.DISCOVERY = None


# ---------------------------------------------------------------------------
# Job status transitions
# ---------------------------------------------------------------------------

class TestJobStatusTransitions:
    def test_job_queued_on_submit(self, client):
        job_id, job = _submit_job(client, "job1", 1, 3)
        assert job["status"] == "queued"

    def test_job_rendering_when_frame_assigned(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert job["status"] == "rendering"

    def test_job_queued_when_all_frames_pending(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 2)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/fail",
                    json={"job_id": job_id, "frame": 1, "log": "err"})
        # Frame 1 went back to pending, frame 2 still pending → job queued
        assert job["status"] == "queued"

    def test_job_done_when_all_frames_done(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 1)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 5.0})
        assert job["status"] == "done"

    def test_cancelled_job_status_not_overridden(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post(f"/api/jobs/{job_id}/cancel")
        # Simulate worker completing a frame after cancel
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 2.0})
        assert job["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

class TestDashboard:
    def test_dashboard_returns_200(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_dashboard_is_html(self, client):
        r = client.get("/")
        assert b"RenderHive" in r.data

    def test_dashboard_has_api_polling_script(self, client):
        r = client.get("/")
        assert b"fetchStatus" in r.data

    def test_dashboard_has_cancel_function(self, client):
        r = client.get("/")
        assert b"cancelJob" in r.data

    def test_dashboard_has_submit_form(self, client):
        r = client.get("/")
        assert b"submit-modal" in r.data


# ---------------------------------------------------------------------------
# ZIP download
# ---------------------------------------------------------------------------

class TestZipDownload:
    def test_zip_returns_200_for_valid_job(self, client):
        job_id, job = _submit_job(client, "zip_test", 1, 1)
        # Ensure output dir exists (ZIP of empty dir is still valid)
        Path(job["output_dir"]).mkdir(parents=True, exist_ok=True)
        r = client.get(f"/api/jobs/{job_id}/zip")
        assert r.status_code == 200
        assert r.content_type == "application/zip"

    def test_zip_nonexistent_returns_404(self, client):
        r = client.get("/api/jobs/ghost/zip")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Job summary helper
# ---------------------------------------------------------------------------

class TestJobSummary:
    def test_summary_counts_correct(self, client):
        _register_worker(client, "w1")
        _register_worker(client, "w2")
        job_id, job = _submit_job(client, "job1", 1, 5)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w2/next", json={"id": "w2", "name": "w2"})
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": 1, "render_time": 3.0})
        with app.app_context():
            summary = coord.job_summary(job)
        assert summary["done"] == 1
        assert summary["rendering"] == 1
        assert summary["total"] == 5


# ---------------------------------------------------------------------------
# Prefetch mechanism
# ---------------------------------------------------------------------------

class TestPrefetchMechanism:
    def test_prefetch_returned_at_threshold(self, client):
        """75% progress with plenty of pending frames triggers prefetch."""
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "pjob", 1, 5)
        # assign frame 1
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 75})
        d = r.get_json()
        assert d["ok"] is True
        assert d["prefetch"] is not None
        assert len(d["prefetch"]) >= 1

    def test_prefetch_not_returned_below_threshold(self, client):
        """74% progress must NOT trigger prefetch."""
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "pjob2", 1, 5)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 74})
        d = r.get_json()
        assert d["prefetch"] is None

    def test_prefetch_count_matches_speed_tier(self, client):
        """Fast worker (avg 30s) gets 3 prefetches; slow worker (avg 200s) gets 1."""
        _register_worker(client, "fast")
        _register_worker(client, "slow")
        job_id, _ = _submit_job(client, "speedjob", 1, 10)

        # Set render history
        with LOCK:
            coord.WORKERS["fast"]["render_times"] = [30.0] * 5
            coord.WORKERS["slow"]["render_times"] = [200.0] * 5
        # Assign frame 1 to fast, frame 2 to slow
        client.post("/api/workers/fast/next", json={"id": "fast", "name": "fast"})
        client.post("/api/workers/slow/next", json={"id": "slow", "name": "slow"})

        r = client.post("/api/workers/fast/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 75})
        fast_prefetch = r.get_json()["prefetch"] or []

        r2 = client.post("/api/workers/slow/progress",
                         json={"job_id": job_id, "frame": 2, "progress": 75})
        slow_prefetch = r2.get_json()["prefetch"] or []

        assert len(fast_prefetch) >= len(slow_prefetch)
        assert len(fast_prefetch) <= 3

    def test_prefetch_guard_no_hoard_tail(self, client):
        """When remaining_pending <= active_workers, no prefetch is given."""
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "tailtest", 1, 2)
        # Assign frame 1 — now 1 pending, 1 active_worker
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 75})
        # remaining_pending (1) <= active_workers (1) → no prefetch
        assert r.get_json()["prefetch"] is None

    def test_prefetch_sets_short_deadline(self, client):
        """Pre-assigned frame must have prefetch_deadline ≈ now + PREFETCH_DEADLINE."""
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "dltest", 1, 5)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        before = time.time()
        r = client.post("/api/workers/w1/progress",
                        json={"job_id": job_id, "frame": 1, "progress": 75})
        after = time.time()
        prefetch = (r.get_json()["prefetch"] or [])
        if not prefetch:
            pytest.skip("prefetch guard prevented assignment (pending <= workers)")
        prefetched_frame_num = str(prefetch[0]["frame"])
        with LOCK:
            fr = coord.JOBS[job_id]["frames"][prefetched_frame_num]
        dl = fr.get("prefetch_deadline")
        assert dl is not None
        assert before + coord.PREFETCH_DEADLINE - 2 <= dl <= after + coord.PREFETCH_DEADLINE + 2

    def test_prefetch_not_duplicated(self, client):
        """Calling progress at 75% twice should not create duplicate pre-assignments."""
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "duptest", 1, 5)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/progress",
                    json={"job_id": job_id, "frame": 1, "progress": 75})
        client.post("/api/workers/w1/progress",
                    json={"job_id": job_id, "frame": 1, "progress": 80})
        # Count assigned frames for w1 (excluding frame 1 itself)
        with LOCK:
            prefetched = [
                k for k, f in coord.JOBS[job_id]["frames"].items()
                if f.get("worker") == "w1" and f.get("prefetch_deadline") and f["status"] == "assigned"
            ]
        assert len(prefetched) <= coord._prefetch_count(coord.WORKERS["w1"])


# ---------------------------------------------------------------------------
# Per-frame image endpoint
# ---------------------------------------------------------------------------

class TestFrameImageEndpoint:
    def test_frame_image_serves_file(self, client, tmp_path):
        """GET /api/jobs/{id}/frames/{n}/image returns 200 when file exists."""
        job_id, job = _submit_job(client, "imgtest", 1, 1)
        out_dir = tmp_path / "imgtest"
        out_dir.mkdir()
        img_file = out_dir / "imgtest_0001.png"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header
        with LOCK:
            coord.JOBS[job_id]["output_dir"] = str(out_dir)
            coord.JOBS[job_id]["ext"] = "png"
        r = client.get(f"/api/jobs/{job_id}/frames/1/image")
        assert r.status_code == 200

    def test_frame_image_404_missing_file(self, client, tmp_path):
        """GET returns 404 if the frame file does not exist on disk."""
        job_id, job = _submit_job(client, "missingimg", 1, 1)
        out_dir = tmp_path / "missingimg"
        out_dir.mkdir()
        with LOCK:
            coord.JOBS[job_id]["output_dir"] = str(out_dir)
            coord.JOBS[job_id]["ext"] = "png"
        r = client.get(f"/api/jobs/{job_id}/frames/1/image")
        assert r.status_code == 404

    def test_frame_image_404_unknown_job(self, client):
        """GET returns 404 for unknown job_id."""
        r = client.get("/api/jobs/nonexistent_job/frames/1/image")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# ETA with confidence
# ---------------------------------------------------------------------------

class TestETA:
    def test_eta_uses_farm_fps(self, client):
        """ETA should be based on combined farm FPS, not naive single-worker average."""
        _register_worker(client, "w1")
        _register_worker(client, "w2")
        job_id, job = _submit_job(client, "etajob", 1, 10)
        # Give workers known render histories
        with LOCK:
            coord.WORKERS["w1"]["render_times"] = [60.0] * 5   # 1 frame/min
            coord.WORKERS["w2"]["render_times"] = [60.0] * 5   # 1 frame/min
            coord.WORKERS["w1"]["status"] = "rendering"
            coord.WORKERS["w2"]["status"] = "rendering"
        # Mark 2 frames done, 8 remaining
        with LOCK:
            for fn in ["1", "2"]:
                coord.JOBS[job_id]["frames"][fn]["status"] = "done"
                coord.JOBS[job_id]["frames"][fn]["render_time"] = 60.0
        with app.app_context():
            s = coord.job_summary(job)
        # 2 workers at 1/60 fps each = 2/60 combined = 8 frames → 240s
        assert s["eta_seconds"] is not None
        assert s["eta_seconds"] < 400  # combined fps should give faster ETA than single worker

    def test_eta_confidence_low_variance(self, client):
        """Homogeneous render times should yield high confidence."""
        _register_worker(client, "w1")
        with LOCK:
            coord.WORKERS["w1"]["render_times"] = [30.0] * 10
        job_id, job = _submit_job(client, "conftest", 1, 5)
        with LOCK:
            coord.WORKERS["w1"]["status"] = "rendering"
            coord.JOBS[job_id]["frames"]["1"]["status"] = "done"
            coord.JOBS[job_id]["frames"]["1"]["render_time"] = 30.0
        with app.app_context():
            s = coord.job_summary(job)
        assert s["eta_confidence"] is not None
        assert s["eta_confidence"] >= 90


# ---------------------------------------------------------------------------
# Work stealing
# ---------------------------------------------------------------------------

class TestWorkStealing:
    def _run_reaper_once(self):
        """Manually run one reaper iteration (without sleeping)."""
        now = time.time()
        with LOCK:
            for w in coord.WORKERS.values():
                if w["status"] != "offline" and now - w["last_seen"] > coord.WORKER_TIMEOUT:
                    w["status"] = "offline"
                    w["current_job"] = None
                    w["current_frame"] = None
                    w["progress"] = 0
            for job in coord.JOBS.values():
                if job["status"] not in ("rendering", "queued"):
                    continue
                for fno, fr in job["frames"].items():
                    if fr["status"] != "assigned":
                        continue
                    worker = coord.WORKERS.get(fr["worker"])
                    stale_worker = (worker is None or worker["status"] == "offline")
                    deadline = fr.get("prefetch_deadline")
                    if deadline is not None:
                        stale_frame = now > deadline
                        if stale_frame and fr.get("progress", 0) == 0:
                            fr["attempts"] = max(0, fr.get("attempts", 1) - 1)
                        fr.pop("prefetch_deadline", None)
                    else:
                        stale_frame = now - fr.get("started_at", now) > coord.FRAME_TIMEOUT
                    if stale_worker or stale_frame:
                        if fr.get("attempts", 0) >= coord.MAX_FRAME_ATTEMPTS:
                            fr["status"] = "failed"
                        else:
                            fr["status"] = "pending"
                        fr["worker"] = None
                        fr["progress"] = 0
            # Work stealing
            import statistics as _stats
            idle_workers = [w for w in coord.WORKERS.values()
                            if w["status"] == "idle" and now - w["last_seen"] < coord.WORKER_TIMEOUT]
            if idle_workers:
                all_times = [t for w in coord.WORKERS.values()
                             for t in w.get("render_times", [])[-10:] if w.get("render_times")]
                if all_times:
                    farm_avg = _stats.mean(all_times)
                    steal_after = max(farm_avg * 3, 180)
                    for job in coord.JOBS.values():
                        if job["status"] not in ("rendering", "queued"):
                            continue
                        for fno, fr in job["frames"].items():
                            if fr["status"] != "assigned" or fr.get("progress", 0) >= 5:
                                continue
                            if now - fr.get("started_at", now) < steal_after:
                                continue
                            assigned_w = coord.WORKERS.get(fr.get("worker"))
                            if not assigned_w:
                                continue
                            w_avg = coord._worker_avg_speed(assigned_w)
                            if w_avg and w_avg > farm_avg * 1.5:
                                fr["status"] = "pending"
                                fr["worker"] = None
                                fr["progress"] = 0
                                fr.pop("prefetch_deadline", None)
            coord._recompute_job_statuses()

    def test_reaper_steals_slow_frame(self, client):
        """Frame stuck on slow worker is reclaimed when a faster idle worker exists."""
        _register_worker(client, "slow_w")
        _register_worker(client, "idle_w")
        job_id, _ = _submit_job(client, "stealtest", 1, 3)

        with LOCK:
            # slow_w has very high render times; idle_w has fast times
            coord.WORKERS["slow_w"]["render_times"] = [300.0] * 5
            coord.WORKERS["idle_w"]["render_times"] = [30.0] * 5
            # Assign frame 1 to slow_w and back-date started_at so steal triggers
            fr = coord.JOBS[job_id]["frames"]["1"]
            fr["status"] = "assigned"
            fr["worker"] = "slow_w"
            fr["progress"] = 3
            fr["started_at"] = time.time() - 1000  # well past steal_after
            coord.WORKERS["slow_w"]["status"] = "rendering"
            # idle_w is idle and recently seen
            coord.WORKERS["idle_w"]["status"] = "idle"
            coord.WORKERS["idle_w"]["last_seen"] = time.time()

        self._run_reaper_once()

        with LOCK:
            fr = coord.JOBS[job_id]["frames"]["1"]
        assert fr["status"] == "pending"
        assert fr["worker"] is None

    def test_reaper_no_steal_when_no_idle_worker(self, client):
        """Frame on slow worker is NOT stolen when there are no idle workers."""
        _register_worker(client, "slow_w2")
        job_id, _ = _submit_job(client, "nostealtest", 1, 3)

        with LOCK:
            coord.WORKERS["slow_w2"]["render_times"] = [300.0] * 5
            fr = coord.JOBS[job_id]["frames"]["1"]
            fr["status"] = "assigned"
            fr["worker"] = "slow_w2"
            fr["progress"] = 3
            fr["started_at"] = time.time() - 1000
            coord.WORKERS["slow_w2"]["status"] = "rendering"
            # No idle workers exist

        self._run_reaper_once()

        with LOCK:
            fr = coord.JOBS[job_id]["frames"]["1"]
        # Should remain assigned (no idle worker to steal for)
        assert fr["status"] == "assigned"
