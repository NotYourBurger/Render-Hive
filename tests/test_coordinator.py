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
        coord.HONEY.update(
            {"balance": coord.HONEY_START, "earned": 0, "spent": 0,
             "loan": None})
    coord.DISCOVERY = None
    coord.BLENDER_PATH = None
    if coord.SETTINGS_FILE.exists():
        coord.SETTINGS_FILE.unlink()
    yield
    if coord.SETTINGS_FILE.exists():
        coord.SETTINGS_FILE.unlink()


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
# Honey (render-credit economy)
# ---------------------------------------------------------------------------

class TestHoney:
    def _complete_one_frame(self, client, wid="w1"):
        _register_worker(client, wid)
        job_id, job = _submit_job(client, "honeyjob", 1, 3)
        client.post(f"/api/workers/{wid}/next", json={"id": wid, "name": wid})
        return job_id, client.post(f"/api/workers/{wid}/complete", data={
            "job_id": job_id, "frame": 1, "render_time": 5.0})

    def test_status_includes_honey(self, client):
        d = client.get("/api/status").get_json()
        assert d["honey"] == {"balance": coord.HONEY_START,
                              "earned": 0, "spent": 0, "loan": None}

    def test_honey_endpoint(self, client):
        d = client.get("/api/honey").get_json()
        assert d["balance"] == coord.HONEY_START

    def test_complete_frame_costs_honey(self, client):
        _, r = self._complete_one_frame(client)
        assert r.get_json()["honey"] == 1
        with LOCK:
            assert coord.HONEY["balance"] == coord.HONEY_START - 1
            assert coord.HONEY["spent"] == 1

    def test_complete_credits_worker_honey_earned(self, client):
        self._complete_one_frame(client)
        with LOCK:
            assert coord.WORKERS["w1"]["honey_earned"] == 1
        d = client.get("/api/status").get_json()
        assert d["workers"][0]["honey_earned"] == 1

    def test_cancelled_complete_costs_no_honey(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "honeyjob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post(f"/api/jobs/{job_id}/cancel")
        r = client.post("/api/workers/w1/complete", data={
            "job_id": job_id, "frame": 1, "render_time": 5.0})
        assert r.get_json()["honey"] == 0
        with LOCK:
            assert coord.HONEY["balance"] == coord.HONEY_START
            assert coord.WORKERS["w1"].get("honey_earned", 0) == 0

    def test_earn_endpoint_credits(self, client):
        r = client.post("/api/honey/earn", json={"amount": 1, "worker": "w1"})
        assert r.status_code == 200
        assert r.get_json()["balance"] == coord.HONEY_START + 1
        with LOCK:
            assert coord.HONEY["earned"] == 1

    def test_earn_endpoint_rejects_invalid_amounts(self, client):
        for bad in (0, -5, "abc", 101, None):
            r = client.post("/api/honey/earn", json={"amount": bad})
            assert r.status_code == 400, f"amount={bad!r}"
        with LOCK:
            assert coord.HONEY["balance"] == coord.HONEY_START

    def test_submit_blocked_when_out_of_honey(self, client):
        with LOCK:
            coord.HONEY["balance"] = 0
        r = client.post("/api/jobs", data={"name": "blocked"})
        assert r.status_code == 402
        assert "honey" in r.get_json()["error"].lower()

    def test_submit_allowed_with_positive_honey(self, client):
        # With honey in the jar the gate passes through — the request then
        # fails for the usual reason (no blend file), not for honey
        r = client.post("/api/jobs", data={"name": "allowed"})
        assert r.status_code == 400

    def test_self_render_round_trip_is_net_zero(self, client):
        # A node rendering its own job: pays 1 on complete, its worker banks
        # the 1 it earned right back — solo nodes never run out of honey
        self._complete_one_frame(client)
        client.post("/api/honey/earn", json={"amount": 1, "worker": "w1"})
        with LOCK:
            assert coord.HONEY["balance"] == coord.HONEY_START

    def test_honey_persisted_across_state_reload(self, client):
        with LOCK:
            coord.HONEY.update({"balance": 7, "earned": 42, "spent": 135})
        coord.save_state()
        with LOCK:
            coord.HONEY.update({"balance": coord.HONEY_START,
                                "earned": 0, "spent": 0})
        coord.load_state()
        with LOCK:
            assert coord.HONEY == {"balance": 7, "earned": 42, "spent": 135,
                                   "loan": None}


# ---------------------------------------------------------------------------
# Honey loans
# ---------------------------------------------------------------------------

class TestHoneyLoan:
    def test_take_loan(self, client):
        before = time.time()
        r = client.post("/api/honey/loan", json={"amount": 50})
        assert r.status_code == 200
        d = r.get_json()
        assert d["balance"] == coord.HONEY_START + 50
        loan = d["loan"]
        assert loan["amount"] == 50
        assert loan["owed"] == 75  # 50% interest
        assert loan["penalized"] is False
        assert loan["due_at"] == pytest.approx(
            before + coord.HONEY_LOAN_DAYS * 86400, abs=5)

    def test_no_loan_cap(self, client):
        r = client.post("/api/honey/loan", json={"amount": 100000})
        assert r.status_code == 200
        d = r.get_json()
        assert d["balance"] == coord.HONEY_START + 100000
        assert d["loan"]["owed"] == 150000

    def test_interest_rounds_up_for_odd_amounts(self, client):
        d = client.post("/api/honey/loan", json={"amount": 5}).get_json()
        assert d["loan"]["owed"] == 8  # 5 + ceil(2.5)

    def test_loan_rejects_invalid_amounts(self, client):
        for bad in (0, -10, "abc", None):
            r = client.post("/api/honey/loan", json={"amount": bad})
            assert r.status_code == 400, f"amount={bad!r}"
        with LOCK:
            assert coord.HONEY["balance"] == coord.HONEY_START
            assert coord.HONEY["loan"] is None

    def test_second_loan_blocked_until_repaid(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        r = client.post("/api/honey/loan", json={"amount": 10})
        assert r.status_code == 400
        assert "outstanding" in r.get_json()["error"].lower()
        with LOCK:
            assert coord.HONEY["balance"] == coord.HONEY_START + 50
            assert coord.HONEY["loan"]["amount"] == 50

    def test_repay_in_full_clears_loan(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        # balance 150, owed 75 -> default repay pays the whole debt
        r = client.post("/api/honey/repay", json={})
        assert r.status_code == 200
        d = r.get_json()
        assert d["repaid"] == 75
        assert d["balance"] == coord.HONEY_START - 25
        assert d["loan"] is None
        # a new loan is allowed again
        assert client.post("/api/honey/loan",
                           json={"amount": 10}).status_code == 200

    def test_repay_partial(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        r = client.post("/api/honey/repay", json={"amount": 30})
        d = r.get_json()
        assert d["repaid"] == 30
        assert d["loan"]["owed"] == 45
        assert d["balance"] == coord.HONEY_START + 20

    def test_repay_capped_by_balance(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        with LOCK:
            coord.HONEY["balance"] = 40  # owed 75, can only cover 40
        d = client.post("/api/honey/repay", json={}).get_json()
        assert d["repaid"] == 40
        assert d["balance"] == 0
        assert d["loan"]["owed"] == 35

    def test_repay_without_loan_400(self, client):
        r = client.post("/api/honey/repay", json={})
        assert r.status_code == 400

    def test_repay_with_empty_jar_400(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        with LOCK:
            coord.HONEY["balance"] = 0
        r = client.post("/api/honey/repay", json={})
        assert r.status_code == 400
        with LOCK:
            assert coord.HONEY["loan"]["owed"] == 75

    def test_overdue_doubles_interest_once(self, client):
        client.post("/api/honey/loan", json={"amount": 50})  # owed 75
        with LOCK:
            coord.HONEY["loan"]["due_at"] = time.time() - 60
        d = client.get("/api/honey").get_json()
        assert d["loan"]["owed"] == 100  # interest 50% -> 100% of 50
        assert d["loan"]["penalized"] is True
        # applied exactly once — a second look doesn't bump it again
        d = client.get("/api/honey").get_json()
        assert d["loan"]["owed"] == 100

    def test_overdue_penalty_uses_original_amount(self, client):
        client.post("/api/honey/loan", json={"amount": 50})  # owed 75
        client.post("/api/honey/repay", json={"amount": 30})  # owed 45
        with LOCK:
            coord.HONEY["loan"]["due_at"] = time.time() - 60
        d = client.get("/api/honey").get_json()
        # penalty is the extra 50% of the BORROWED 50, not of what's left
        assert d["loan"]["owed"] == 70

    def test_overdue_penalty_rounding_odd_amount(self, client):
        client.post("/api/honey/loan", json={"amount": 5})  # owed 8
        with LOCK:
            coord.HONEY["loan"]["due_at"] = time.time() - 60
        d = client.get("/api/honey").get_json()
        assert d["loan"]["owed"] == 10  # 100% interest of 5 -> 2x5 total

    def test_status_applies_overdue_penalty(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        with LOCK:
            coord.HONEY["loan"]["due_at"] = time.time() - 60
        d = client.get("/api/status").get_json()
        assert d["honey"]["loan"]["owed"] == 100

    def test_repay_settles_at_penalized_rate(self, client):
        client.post("/api/honey/loan", json={"amount": 50})  # owed 75
        with LOCK:
            coord.HONEY["loan"]["due_at"] = time.time() - 60
            coord.HONEY["balance"] = 75
        # Repaying the pre-penalty 75 must NOT clear the loan: the deadline
        # passed, so the debt is 100 and 25 remains owed
        d = client.post("/api/honey/repay", json={}).get_json()
        assert d["repaid"] == 75
        assert d["loan"]["owed"] == 25

    def test_overdue_loan_blocks_submission(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        with LOCK:
            coord.HONEY["loan"]["due_at"] = time.time() - 60
        r = client.post("/api/jobs", data={"name": "frozen"})
        assert r.status_code == 402
        assert "loan" in r.get_json()["error"].lower()

    def test_active_loan_does_not_block_submission(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        # Gate passes (loan not due yet); fails for the usual no-blend reason
        r = client.post("/api/jobs", data={"name": "ok"})
        assert r.status_code == 400

    def test_repaying_overdue_loan_unfreezes_submission(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        with LOCK:
            coord.HONEY["loan"]["due_at"] = time.time() - 60
        client.post("/api/honey/repay", json={})
        r = client.post("/api/jobs", data={"name": "thawed"})
        assert r.status_code == 400  # back to the normal no-blend error

    def test_loan_persisted_across_state_reload(self, client):
        client.post("/api/honey/loan", json={"amount": 50})
        with LOCK:
            saved = dict(coord.HONEY["loan"])
        coord.save_state()
        with LOCK:
            coord.HONEY["loan"] = None
        coord.load_state()
        with LOCK:
            assert coord.HONEY["loan"] == saved


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
        coord._reap_once()
        assert coord.WORKERS["w1"]["status"] == "offline"

    def test_reap_requeues_stale_assigned_frame(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        # Age the frame past the timeout
        with LOCK:
            job["frames"]["1"]["started_at"] = time.time() - coord.FRAME_TIMEOUT - 1
        coord._reap_once()
        assert job["frames"]["1"]["status"] == "pending"

    def test_reap_requeues_frame_of_offline_worker(self, client):
        """A crashed/offline PC's assigned frames go back to the queue."""
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "job1", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        with LOCK:
            coord.WORKERS["w1"]["last_seen"] -= coord.WORKER_TIMEOUT + 1
        coord._reap_once()
        assert coord.WORKERS["w1"]["status"] == "offline"
        assert job["frames"]["1"]["status"] == "pending"
        assert job["frames"]["1"]["worker"] is None


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

    # -- EXR/TIFF preview conversion ------------------------------------
    def _exr_job(self, client, tmp_path, name="exrtest"):
        """Job with one finished EXR frame on disk; previews are wiped so a
        cached JPEG from a previous test run can't leak in."""
        job_id, job = _submit_job(client, name, 1, 1)
        out_dir = tmp_path / name
        out_dir.mkdir()
        exr_file = out_dir / f"{name}_0001.exr"
        exr_file.write_bytes(b"\x76\x2f\x31\x01 fake exr payload")
        with LOCK:
            job["output_dir"] = str(out_dir)
            job["format"] = "OPEN_EXR"
            job["ext"] = "exr"
        import shutil
        shutil.rmtree(coord.PREVIEW_DIR / job_id, ignore_errors=True)
        return job_id, exr_file

    @staticmethod
    def _fake_blender_run(cmd, **kwargs):
        """Stand-in for the headless Blender call: writes the JPEG preview
        at the destination path (the script's last argument)."""
        Path(cmd[-1]).write_bytes(b"\xff\xd8\xff\xe0 fake jpeg")
        return MagicMock(stdout="RENDERHIVE_PREVIEW: ok", stderr="")

    def test_exr_preview_served_as_jpeg(self, client, tmp_path):
        """An EXR frame is converted (via Blender) and served as JPEG."""
        job_id, _ = self._exr_job(client, tmp_path)
        with patch.object(coord, "BLENDER_PATH", "blender"), \
             patch.object(coord.subprocess, "run",
                          side_effect=self._fake_blender_run) as m:
            r = client.get(f"/api/jobs/{job_id}/frames/1/image")
        assert r.status_code == 200
        assert r.mimetype == "image/jpeg"
        assert r.data.startswith(b"\xff\xd8")
        r.close()
        cmd = m.call_args[0][0]
        assert str(coord.PREVIEW_SCRIPT) in cmd

    def test_exr_preview_cached(self, client, tmp_path):
        """The JPEG conversion runs once; later requests hit the cache."""
        job_id, _ = self._exr_job(client, tmp_path, "exrcache")
        with patch.object(coord, "BLENDER_PATH", "blender"), \
             patch.object(coord.subprocess, "run",
                          side_effect=self._fake_blender_run) as m:
            client.get(f"/api/jobs/{job_id}/frames/1/image").close()
            r2 = client.get(f"/api/jobs/{job_id}/frames/1/image")
        assert r2.status_code == 200
        r2.close()
        assert m.call_count == 1

    def test_exr_preview_regenerated_after_rerender(self, client, tmp_path):
        """A frame re-rendered after the preview was made gets a fresh one."""
        job_id, exr_file = self._exr_job(client, tmp_path, "exrregen")
        with patch.object(coord, "BLENDER_PATH", "blender"), \
             patch.object(coord.subprocess, "run",
                          side_effect=self._fake_blender_run) as m:
            client.get(f"/api/jobs/{job_id}/frames/1/image").close()
            future = time.time() + 60
            os.utime(exr_file, (future, future))
            client.get(f"/api/jobs/{job_id}/frames/1/image").close()
        assert m.call_count == 2

    def test_exr_raw_param_serves_original(self, client, tmp_path):
        """?raw=1 downloads the actual EXR file, no conversion involved."""
        job_id, exr_file = self._exr_job(client, tmp_path, "exrraw")
        with patch.object(coord.subprocess, "run") as m:
            r = client.get(f"/api/jobs/{job_id}/frames/1/image?raw=1")
        assert r.status_code == 200
        assert r.data == exr_file.read_bytes()
        r.close()
        m.assert_not_called()

    def test_exr_preview_failure_404(self, client, tmp_path):
        """If Blender produces no JPEG the endpoint 404s (dashboard hides
        the thumbnail) instead of serving glitched raw EXR bytes."""
        job_id, _ = self._exr_job(client, tmp_path, "exrfail")
        with patch.object(coord, "BLENDER_PATH", "blender"), \
             patch.object(coord.subprocess, "run",
                          return_value=MagicMock(stdout="", stderr="Error")):
            r = client.get(f"/api/jobs/{job_id}/frames/1/image")
        assert r.status_code == 404

    def test_exr_preview_no_blender_404(self, client, tmp_path):
        """No Blender on the coordinator -> 404, not a crash."""
        job_id, _ = self._exr_job(client, tmp_path, "exrnoblender")
        with patch.object(coord, "detect_blender", return_value=None):
            r = client.get(f"/api/jobs/{job_id}/frames/1/image")
        assert r.status_code == 404

    def test_png_served_directly_without_conversion(self, client, tmp_path):
        """Browser-displayable formats never go through Blender."""
        job_id, job = _submit_job(client, "pngdirect", 1, 1)
        out_dir = tmp_path / "pngdirect"
        out_dir.mkdir()
        (out_dir / "pngdirect_0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        with LOCK:
            job["output_dir"] = str(out_dir)
        with patch.object(coord.subprocess, "run") as m:
            r = client.get(f"/api/jobs/{job_id}/frames/1/image")
        assert r.status_code == 200
        r.close()
        m.assert_not_called()

    def test_delete_job_removes_previews(self, client, tmp_path):
        """Deleting a job also deletes its cached previews."""
        job_id, _ = self._exr_job(client, tmp_path, "exrdel")
        with patch.object(coord, "BLENDER_PATH", "blender"), \
             patch.object(coord.subprocess, "run",
                          side_effect=self._fake_blender_run):
            client.get(f"/api/jobs/{job_id}/frames/1/image").close()
        prev_dir = coord.PREVIEW_DIR / job_id
        assert prev_dir.exists()
        client.post(f"/api/jobs/{job_id}/delete", json={})
        assert not prev_dir.exists()

    def test_complete_exr_frame_spawns_preview_thread(self, client, tmp_path):
        """Uploading a finished EXR frame pre-generates its preview in the
        background so the dashboard thumbnail is instant."""
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "exrcomplete", 1, 1)
        with LOCK:
            job["output_dir"] = str(tmp_path / "exrcomplete")
            job["format"] = "OPEN_EXR"
            job["ext"] = "exr"
            job["frames"]["1"]["status"] = "assigned"
            job["frames"]["1"]["worker"] = "w1"
        with patch.object(coord.threading, "Thread") as mthread:
            r = client.post(f"/api/workers/w1/complete", data={
                "job_id": job_id, "frame": "1", "render_time": "1.0",
                "image": (io.BytesIO(b"exr-bytes"), "frame.exr"),
            }, content_type="multipart/form-data")
        assert r.status_code == 200
        targets = [c.kwargs.get("target") for c in mthread.call_args_list]
        assert coord.ensure_preview in targets

    def test_complete_png_frame_no_preview_thread(self, client, tmp_path):
        """PNG frames need no conversion, so no preview thread is spawned."""
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "pngcomplete", 1, 1)
        with LOCK:
            job["output_dir"] = str(tmp_path / "pngcomplete")
            job["frames"]["1"]["status"] = "assigned"
            job["frames"]["1"]["worker"] = "w1"
        with patch.object(coord.threading, "Thread") as mthread:
            r = client.post(f"/api/workers/w1/complete", data={
                "job_id": job_id, "frame": "1", "render_time": "1.0",
                "image": (io.BytesIO(b"png-bytes"), "frame.png"),
            }, content_type="multipart/form-data")
        assert r.status_code == 200
        targets = [c.kwargs.get("target") for c in mthread.call_args_list]
        assert coord.ensure_preview not in targets


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
            fr["last_progress_at"] = time.time()   # still reporting -> not stalled
            coord.WORKERS["slow_w"]["status"] = "rendering"
            # idle_w is idle and recently seen
            coord.WORKERS["idle_w"]["status"] = "idle"
            coord.WORKERS["idle_w"]["last_seen"] = time.time()

        coord._reap_once()

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
            fr["last_progress_at"] = time.time()  # still reporting -> not stalled
            coord.WORKERS["slow_w2"]["status"] = "rendering"
            # No idle workers exist

        coord._reap_once()

        with LOCK:
            fr = coord.JOBS[job_id]["frames"]["1"]
        # Should remain assigned (no idle worker to steal for)
        assert fr["status"] == "assigned"

    def test_no_steal_to_paused_idle_worker(self, client):
        """A paused idle worker doesn't count as a steal target."""
        _register_worker(client, "slow_w3")
        _register_worker(client, "paused_w")
        job_id, _ = _submit_job(client, "pausedstealtest", 1, 3)

        with LOCK:
            coord.WORKERS["slow_w3"]["render_times"] = [300.0] * 5
            coord.WORKERS["paused_w"]["render_times"] = [30.0] * 5
            fr = coord.JOBS[job_id]["frames"]["1"]
            fr["status"] = "assigned"
            fr["worker"] = "slow_w3"
            fr["progress"] = 3
            fr["started_at"] = time.time() - 1000
            fr["last_progress_at"] = time.time()
            coord.WORKERS["slow_w3"]["status"] = "rendering"
            coord.WORKERS["paused_w"]["status"] = "idle"
            coord.WORKERS["paused_w"]["paused"] = True
            coord.WORKERS["paused_w"]["last_seen"] = time.time()

        coord._reap_once()

        with LOCK:
            fr = coord.JOBS[job_id]["frames"]["1"]
        assert fr["status"] == "assigned"


# ---------------------------------------------------------------------------
# Scheduler: priority, FIFO, blend affinity
# ---------------------------------------------------------------------------

class TestScheduler:
    def test_higher_priority_job_dispatched_first(self, client):
        _register_worker(client, "w1")
        _, job_a = _submit_job(client, "older_normal", 1, 3)
        _, job_b = _submit_job(client, "newer_urgent", 1, 3)
        with LOCK:
            job_a["created_at"] = 100.0
            job_b["created_at"] = 200.0
            job_a["priority"] = 5
            job_b["priority"] = 9
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"]["job_id"] == job_b["id"]

    def test_fifo_within_same_priority(self, client):
        _register_worker(client, "w1")
        _, job_a = _submit_job(client, "first", 1, 3)
        _, job_b = _submit_job(client, "second", 1, 3)
        with LOCK:
            job_a["created_at"] = 100.0
            job_b["created_at"] = 200.0
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"]["job_id"] == job_a["id"]

    def test_blend_affinity_same_priority(self, client):
        """Worker keeps pulling from the job whose blend it already cached."""
        _register_worker(client, "w1")
        _, job_a = _submit_job(client, "jobA", 1, 3)
        _, job_b = _submit_job(client, "jobB", 1, 3)
        with LOCK:
            job_a["created_at"] = 100.0
            job_b["created_at"] = 200.0
            coord.WORKERS["w1"]["last_job"] = job_b["id"]
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"]["job_id"] == job_b["id"]

    def test_affinity_never_overrides_priority(self, client):
        _register_worker(client, "w1")
        _, job_a = _submit_job(client, "highprio", 1, 3)
        _, job_b = _submit_job(client, "cached", 1, 3)
        with LOCK:
            job_a["created_at"] = 100.0
            job_a["priority"] = 8
            job_b["created_at"] = 50.0
            job_b["priority"] = 5
            coord.WORKERS["w1"]["last_job"] = job_b["id"]
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"]["job_id"] == job_a["id"]

    def test_last_job_recorded_on_assignment(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "affjob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert coord.WORKERS["w1"]["last_job"] == job_id

    def test_paused_job_not_dispatched(self, client):
        _register_worker(client, "w1")
        _, job = _submit_job(client, "pausedjob", 1, 3)
        with LOCK:
            job["status"] = "paused"
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is None

    def test_job_priority_in_summary(self, client):
        _, job = _submit_job(client, "pjob", 1, 3)
        with LOCK:
            job["priority"] = 7
        d = client.get("/api/status").get_json()
        assert d["jobs"][0]["priority"] == 7


# ---------------------------------------------------------------------------
# Crash recovery: orphaned and stalled frames
# ---------------------------------------------------------------------------

class TestOrphanRecovery:
    def test_orphaned_frame_requeued(self, client):
        """Worker reports idle while a frame is still assigned to it."""
        _register_worker(client, "w1")
        _, job = _submit_job(client, "orphanjob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        with LOCK:
            coord.WORKERS["w1"]["status"] = "idle"
            job["frames"]["1"]["started_at"] = time.time() - coord.ORPHAN_GRACE - 1
            job["frames"]["1"]["last_progress_at"] = time.time()
        coord._reap_once()
        assert job["frames"]["1"]["status"] == "pending"
        assert job["frames"]["1"]["worker"] is None

    def test_orphan_requeue_does_not_burn_attempt(self, client):
        _register_worker(client, "w1")
        _, job = _submit_job(client, "orphanjob2", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert job["frames"]["1"]["attempts"] == 1
        with LOCK:
            coord.WORKERS["w1"]["status"] = "idle"
            job["frames"]["1"]["started_at"] = time.time() - coord.ORPHAN_GRACE - 1
            job["frames"]["1"]["last_progress_at"] = time.time()
        coord._reap_once()
        assert job["frames"]["1"]["attempts"] == 0

    def test_fresh_assignment_not_treated_as_orphan(self, client):
        """Within the grace period an idle-reporting worker keeps its frame."""
        _register_worker(client, "w1")
        _, job = _submit_job(client, "gracejob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        with LOCK:
            coord.WORKERS["w1"]["status"] = "idle"
        coord._reap_once()
        assert job["frames"]["1"]["status"] == "assigned"

    def test_rendering_worker_not_orphaned(self, client):
        _register_worker(client, "w1")
        _, job = _submit_job(client, "renderingjob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        with LOCK:
            job["frames"]["1"]["started_at"] = time.time() - coord.ORPHAN_GRACE - 1
            job["frames"]["1"]["last_progress_at"] = time.time()
        coord._reap_once()
        assert job["frames"]["1"]["status"] == "assigned"


class TestStallRecovery:
    def test_stalled_frame_requeued(self, client):
        """Worker heartbeats but render makes no progress -> frame requeued."""
        _register_worker(client, "w1")
        _, job = _submit_job(client, "stalljob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        now = time.time()
        with LOCK:
            job["frames"]["1"]["started_at"] = now - coord.STALL_TIMEOUT - 60
            job["frames"]["1"]["last_progress_at"] = now - coord.STALL_TIMEOUT - 1
        coord._reap_once()
        assert job["frames"]["1"]["status"] == "pending"

    def test_progressing_frame_not_stalled(self, client):
        _register_worker(client, "w1")
        _, job = _submit_job(client, "okjob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        now = time.time()
        with LOCK:
            job["frames"]["1"]["started_at"] = now - coord.STALL_TIMEOUT - 60
            job["frames"]["1"]["last_progress_at"] = now - 5
        coord._reap_once()
        assert job["frames"]["1"]["status"] == "assigned"

    def test_progress_report_refreshes_stall_timer(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "refreshjob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        before = job["frames"]["1"]["last_progress_at"]
        time.sleep(0.01)
        client.post("/api/workers/w1/progress",
                    json={"job_id": job_id, "frame": 1, "progress": 42})
        assert job["frames"]["1"]["last_progress_at"] > before

    def test_progress_marks_worker_rendering(self, client):
        _register_worker(client, "w1")
        job_id, _ = _submit_job(client, "wstatusjob", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post("/api/workers/w1/heartbeat", json={"status": "idle"})
        client.post("/api/workers/w1/progress",
                    json={"job_id": job_id, "frame": 1, "progress": 10})
        assert coord.WORKERS["w1"]["status"] == "rendering"


# ---------------------------------------------------------------------------
# Job pause / resume
# ---------------------------------------------------------------------------

class TestJobPauseResume:
    def test_pause_sets_status(self, client):
        job_id, job = _submit_job(client, "pjob1", 1, 3)
        r = client.post(f"/api/jobs/{job_id}/pause")
        assert r.status_code == 200
        assert job["status"] == "paused"

    def test_paused_job_survives_recompute(self, client):
        job_id, job = _submit_job(client, "pjob2", 1, 3)
        client.post(f"/api/jobs/{job_id}/pause")
        with LOCK:
            coord._recompute_job_statuses()
        assert job["status"] == "paused"

    def test_resume_requeues(self, client):
        job_id, job = _submit_job(client, "pjob3", 1, 3)
        client.post(f"/api/jobs/{job_id}/pause")
        r = client.post(f"/api/jobs/{job_id}/resume")
        assert r.status_code == 200
        assert job["status"] == "queued"

    def test_pause_done_job_rejected(self, client):
        job_id, job = _submit_job(client, "pjob4", 1, 1)
        with LOCK:
            job["frames"]["1"]["status"] = "done"
            coord._recompute_job_statuses()
        r = client.post(f"/api/jobs/{job_id}/pause")
        assert r.status_code == 400

    def test_resume_non_paused_rejected(self, client):
        job_id, _ = _submit_job(client, "pjob5", 1, 3)
        r = client.post(f"/api/jobs/{job_id}/resume")
        assert r.status_code == 400

    def test_pause_nonexistent_404(self, client):
        assert client.post("/api/jobs/nope/pause").status_code == 404

    def test_inflight_frame_completes_while_paused(self, client):
        """Pause stops new dispatch but in-flight frames still land."""
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "pjob6", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        client.post(f"/api/jobs/{job_id}/pause")
        client.post("/api/workers/w1/complete",
                    data={"job_id": job_id, "frame": "1", "render_time": "5.0"})
        assert job["frames"]["1"]["status"] == "done"
        assert job["status"] == "paused"


# ---------------------------------------------------------------------------
# Worker pause / resume
# ---------------------------------------------------------------------------

class TestWorkerPause:
    def test_pause_worker(self, client):
        _register_worker(client, "w1")
        r = client.post("/api/workers/w1/pause", json={"paused": True})
        assert r.status_code == 200
        assert coord.WORKERS["w1"]["paused"] is True

    def test_paused_worker_gets_no_frames(self, client):
        _register_worker(client, "w1")
        _submit_job(client, "wpjob", 1, 3)
        client.post("/api/workers/w1/pause", json={"paused": True})
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is None

    def test_resumed_worker_gets_frames(self, client):
        _register_worker(client, "w1")
        _submit_job(client, "wpjob2", 1, 3)
        client.post("/api/workers/w1/pause", json={"paused": True})
        client.post("/api/workers/w1/pause", json={"paused": False})
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is not None

    def test_paused_status_shown(self, client):
        _register_worker(client, "w1")
        client.post("/api/workers/w1/pause", json={"paused": True})
        d = client.get("/api/status").get_json()
        assert d["workers"][0]["status"] == "paused"
        assert d["workers"][0]["paused"] is True

    def test_pause_unknown_worker_404(self, client):
        assert client.post("/api/workers/ghost/pause",
                           json={"paused": True}).status_code == 404

    def test_paused_worker_gets_no_prefetch(self, client):
        _register_worker(client, "w1")
        _submit_job(client, "wpjob3", 1, 5)
        with LOCK:
            coord.WORKERS["w1"]["paused"] = True
            assignments = coord._try_prefetch_frames("w1")
        assert assignments == []


# ---------------------------------------------------------------------------
# Frame requeue endpoint
# ---------------------------------------------------------------------------

class TestFrameRequeue:
    def test_requeue_failed_frame(self, client):
        job_id, job = _submit_job(client, "rqjob", 1, 3)
        with LOCK:
            job["frames"]["2"].update({"status": "failed", "attempts": 3,
                                       "last_error": "boom"})
        r = client.post(f"/api/jobs/{job_id}/frames/2/requeue")
        assert r.status_code == 200
        fr = job["frames"]["2"]
        assert fr["status"] == "pending"
        assert fr["attempts"] == 0
        assert fr["last_error"] is None

    def test_requeue_stuck_assigned_frame(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "rqjob2", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        r = client.post(f"/api/jobs/{job_id}/frames/1/requeue")
        assert r.status_code == 200
        assert r.get_json()["was_on"] == "w1"
        assert job["frames"]["1"]["status"] == "pending"

    def test_requeue_unknown_frame_404(self, client):
        job_id, _ = _submit_job(client, "rqjob3", 1, 3)
        assert client.post(f"/api/jobs/{job_id}/frames/99/requeue").status_code == 404

    def test_requeue_unknown_job_404(self, client):
        assert client.post("/api/jobs/nope/frames/1/requeue").status_code == 404


# ---------------------------------------------------------------------------
# Priority endpoint
# ---------------------------------------------------------------------------

class TestPriorityEndpoint:
    def test_set_priority(self, client):
        job_id, job = _submit_job(client, "priojob", 1, 3)
        r = client.post(f"/api/jobs/{job_id}/priority", json={"priority": 9})
        assert r.status_code == 200
        assert job["priority"] == 9

    def test_priority_clamped(self, client):
        job_id, job = _submit_job(client, "priojob2", 1, 3)
        client.post(f"/api/jobs/{job_id}/priority", json={"priority": 99})
        assert job["priority"] == 10
        client.post(f"/api/jobs/{job_id}/priority", json={"priority": -5})
        assert job["priority"] == 1

    def test_invalid_priority_400(self, client):
        job_id, _ = _submit_job(client, "priojob3", 1, 3)
        r = client.post(f"/api/jobs/{job_id}/priority", json={"priority": "abc"})
        assert r.status_code == 400

    def test_priority_unknown_job_404(self, client):
        assert client.post("/api/jobs/nope/priority",
                           json={"priority": 5}).status_code == 404


# ---------------------------------------------------------------------------
# Settings API + default output directory
# ---------------------------------------------------------------------------

class TestSettingsAPI:
    def test_get_settings_defaults(self, client):
        d = client.get("/api/settings").get_json()
        assert d["default_output_dir"] == ""
        assert d["data_dir"] == str(coord.DATA_DIR)
        assert d["managed_output_dir"] == str(coord.OUTPUT_DIR)

    def test_set_and_get_default_output_dir(self, client, tmp_path):
        target = str(tmp_path / "renders")
        r = client.post("/api/settings", json={"default_output_dir": target})
        assert r.status_code == 200
        d = client.get("/api/settings").get_json()
        assert d["default_output_dir"] == target

    def test_invalid_output_dir_rejected(self, client):
        r = client.post("/api/settings",
                        json={"default_output_dir": "ZZ:\\no\\such\\drive"})
        assert r.status_code == 400

    def test_submit_uses_default_output_dir(self, client, tmp_path):
        target = str(tmp_path / "renders")
        client.post("/api/settings", json={"default_output_dir": target})
        r = client.post("/api/jobs", data={
            "name": "settingsjob", "shared_path": "X:/proj/scene.blend",
            "frame_start": "1", "frame_end": "2", "format": "PNG",
        })
        assert r.status_code == 200
        job_id = r.get_json()["job_id"]
        out = Path(coord.JOBS[job_id]["output_dir"])
        assert out == Path(target) / "settingsjob"
        assert out.exists()

    def test_explicit_output_dir_beats_default(self, client, tmp_path):
        client.post("/api/settings",
                    json={"default_output_dir": str(tmp_path / "default")})
        explicit = str(tmp_path / "explicit")
        r = client.post("/api/jobs", data={
            "name": "explicitjob", "shared_path": "X:/proj/scene.blend",
            "frame_start": "1", "frame_end": "2", "format": "PNG",
            "output_dir": explicit,
        })
        assert r.status_code == 200
        job_id = r.get_json()["job_id"]
        assert Path(coord.JOBS[job_id]["output_dir"]) == Path(explicit)

    def test_job_priority_from_submit_form(self, client, tmp_path):
        r = client.post("/api/jobs", data={
            "name": "subprio", "shared_path": "X:/proj/scene.blend",
            "frame_start": "1", "frame_end": "2", "format": "PNG",
            "priority": "8",
        })
        assert r.status_code == 200
        job_id = r.get_json()["job_id"]
        assert coord.JOBS[job_id]["priority"] == 8


# ---------------------------------------------------------------------------
# Job deletion with outputs
# ---------------------------------------------------------------------------

class TestDeleteOutputs:
    def test_delete_with_outputs_removes_managed_dir(self, client):
        job_id, job = _submit_job(client, "deljob", 1, 2)
        out = Path(coord.OUTPUT_DIR) / "deljob"
        out.mkdir(parents=True, exist_ok=True)
        (out / "deljob_0001.png").write_bytes(b"fake")
        client.post(f"/api/jobs/{job_id}/delete", json={"delete_outputs": True})
        assert not out.exists()

    def test_delete_without_flag_keeps_outputs(self, client):
        job_id, job = _submit_job(client, "deljob2", 1, 2)
        out = Path(coord.OUTPUT_DIR) / "deljob2"
        out.mkdir(parents=True, exist_ok=True)
        (out / "deljob2_0001.png").write_bytes(b"fake")
        client.post(f"/api/jobs/{job_id}/delete")
        assert out.exists()
        import shutil as _sh
        _sh.rmtree(out, ignore_errors=True)

    def test_delete_never_removes_custom_output_dir(self, client, tmp_path):
        job_id, job = _submit_job(client, "deljob3", 1, 2)
        custom = tmp_path / "my_renders"
        custom.mkdir()
        (custom / "frame.png").write_bytes(b"fake")
        with LOCK:
            job["output_dir"] = str(custom)
        client.post(f"/api/jobs/{job_id}/delete", json={"delete_outputs": True})
        assert custom.exists()
        assert (custom / "frame.png").exists()


# ---------------------------------------------------------------------------
# Improved ETA
# ---------------------------------------------------------------------------

class TestImprovedETA:
    def _setup_rendering_job(self, client, name, n_frames=5, speed=10.0):
        _register_worker(client, f"{name}_w")
        job_id, job = _submit_job(client, name, 1, n_frames)
        with LOCK:
            w = coord.WORKERS[f"{name}_w"]
            w["render_times"] = [speed] * 5
            w["status"] = "rendering"
            w["current_job"] = job_id
            fr = job["frames"]["1"]
            fr["status"] = "assigned"
            fr["worker"] = f"{name}_w"
            job["status"] = "rendering"
        return job_id, job

    def test_eta_uses_job_workers_speed(self, client):
        job_id, job = self._setup_rendering_job(client, "etajob", 5, 10.0)
        with LOCK:
            s = coord.job_summary(job)
        # 5 remaining frames at 0.1 fps -> 50s
        assert s["eta_seconds"] == 50

    def test_eta_credits_partial_progress(self, client):
        job_id, job = self._setup_rendering_job(client, "etajob2", 5, 10.0)
        with LOCK:
            job["frames"]["1"]["progress"] = 50
            s = coord.job_summary(job)
        # 5 - 0.5 = 4.5 remaining -> 45s
        assert s["eta_seconds"] == 45

    def test_queued_job_eta_includes_queue_wait(self, client):
        job_id, job = self._setup_rendering_job(client, "active", 5, 10.0)
        _, queued = _submit_job(client, "waiting", 1, 5)
        with LOCK:
            job["created_at"] = 100.0
            queued["created_at"] = 200.0
        d = client.get("/api/status").get_json()
        by_name = {j["name"]: j for j in d["jobs"]}
        active, waiting = by_name["active"], by_name["waiting"]
        assert active["eta_seconds"] == 50
        # queued job: 50s queue wait + 50s own work
        assert waiting["eta_seconds"] == 100
        assert waiting["eta_queued_behind"] == 50

    def test_elapsed_seconds_reported(self, client):
        job_id, job = self._setup_rendering_job(client, "eljob", 3, 10.0)
        with LOCK:
            job["frames"]["1"]["started_at"] = time.time() - 120
            s = coord.job_summary(job)
        assert 118 <= s["elapsed_seconds"] <= 125


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_state_is_valid_json(self, client):
        _submit_job(client, "persjob", 1, 3)
        coord.save_state()
        data = json.loads(coord.STATE_FILE.read_text())
        assert "persjob" in str(data)

    def test_save_state_leaves_no_tmp_file(self, client):
        _submit_job(client, "persjob2", 1, 3)
        coord.save_state()
        assert not coord.STATE_FILE.with_suffix(".json.tmp").exists()

    def test_load_state_defaults_priority(self, client):
        _, job = _submit_job(client, "persjob3", 1, 2)
        with LOCK:
            job.pop("priority", None)
        coord.save_state()
        with LOCK:
            coord.JOBS.clear()
        coord.load_state()
        with LOCK:
            loaded = next(j for j in coord.JOBS.values() if j["name"] == "persjob3")
            assert loaded["priority"] == coord.DEFAULT_PRIORITY
            coord.JOBS.clear()

    def test_load_state_requeues_assigned_frames(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "persjob4", 1, 3)
        client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        coord.save_state()
        with LOCK:
            coord.JOBS.clear()
        coord.load_state()
        with LOCK:
            loaded = next(j for j in coord.JOBS.values() if j["name"] == "persjob4")
            assert loaded["frames"]["1"]["status"] == "pending"
            assert loaded["frames"]["1"]["worker"] is None
            coord.JOBS.clear()

    def test_data_dir_honors_env_override(self):
        assert str(coord.DATA_DIR).startswith(
            os.environ["RENDERHIVE_DATA_DIR"])


# ---------------------------------------------------------------------------
# Dependency packing
# ---------------------------------------------------------------------------

class TestDependencyPacking:
    def _upload_job(self, client, pack="1", **extra):
        """Upload a fake blend with explicit settings (no probe needed)."""
        data = {
            "name": extra.pop("name", "packjob"),
            "frame_start": "1", "frame_end": "3", "format": "PNG",
            "pack_deps": pack,
            "blendfile": (io.BytesIO(b"BLENDER-fake"), "scene.blend"),
        }
        data.update(extra)
        with patch.object(coord.threading, "Thread") as mock_thread:
            r = client.post("/api/jobs", data=data,
                            content_type="multipart/form-data")
        return r, mock_thread

    def test_upload_with_pack_starts_in_packing_status(self, client):
        r, mock_thread = self._upload_job(client, pack="1")
        assert r.status_code == 200
        job_id = r.get_json()["job_id"]
        with LOCK:
            assert coord.JOBS[job_id]["status"] == "packing"
        # A packing thread was spawned for this job
        assert any(kw.get("target") is coord.pack_blend_job
                   for _a, kw in mock_thread.call_args_list)

    def test_upload_without_pack_is_queued(self, client):
        r, mock_thread = self._upload_job(client, pack="0")
        job_id = r.get_json()["job_id"]
        with LOCK:
            assert coord.JOBS[job_id]["status"] == "queued"
        assert not any(kw.get("target") is coord.pack_blend_job
                       for _a, kw in mock_thread.call_args_list)

    def test_shared_path_packs_too(self, client):
        """Shared-path jobs pack from the blend's ORIGINAL location, which is
        the only way relative asset paths can be resolved."""
        with patch.object(coord.threading, "Thread") as mock_thread:
            r = client.post("/api/jobs", data={
                "name": "sharedjob", "frame_start": "1", "frame_end": "2",
                "format": "PNG", "pack_deps": "1",
                "shared_path": r"\server\share\scene.blend",
            })
        assert r.status_code == 200
        job_id = r.get_json()["job_id"]
        with LOCK:
            assert coord.JOBS[job_id]["status"] == "packing"
        assert any(kw.get("target") is coord.pack_blend_job
                   for _a, kw in mock_thread.call_args_list)

    def test_shared_path_without_pack_stays_queued(self, client):
        r = client.post("/api/jobs", data={
            "name": "sharedjob2", "frame_start": "1", "frame_end": "2",
            "format": "PNG", "pack_deps": "0",
            "shared_path": r"\server\share\scene.blend",
        })
        assert r.status_code == 200
        job_id = r.get_json()["job_id"]
        with LOCK:
            assert coord.JOBS[job_id]["status"] == "queued"

    def test_missing_dependencies_fail_the_job(self, client, tmp_path):
        """Packing succeeded technically, but a dependency wasn't found on
        disk — that frame would render pink, so the job must fail."""
        blend = tmp_path / "scene.blend"
        blend.write_bytes(b"BLENDER-fake")
        job_id, job = _submit_job(client, "packmiss", 1, 2)
        with LOCK:
            job["status"] = "packing"
            job["blend_path"] = str(blend)
            job["blend_filename"] = "scene.blend"
        pack_dir = coord.BLEND_DIR / f"{job_id}_pack"

        def fake_run(cmd, **kwargs):
            pack_dir.mkdir(parents=True, exist_ok=True)
            (pack_dir / "scene.blend").write_bytes(b"BLENDER-packed")
            m = MagicMock()
            m.stdout = ("RENDERHIVE_PACK:" + json.dumps(
                {"ok": True, "blend": "scene.blend", "copied": 0, "bytes": 1,
                 "missing": ["C:/projects/textures/clip.mp4"],
                 "warnings": []}))
            m.stderr = ""
            return m

        with patch.object(coord, "detect_blender", return_value="blender.exe"), \
             patch.object(coord.subprocess, "run", side_effect=fake_run):
            coord.pack_blend_job(job_id)
        with LOCK:
            assert job["status"] == "failed"
            assert "clip.mp4" in job["pack_error"]
            assert job["pack_missing"] == ["C:/projects/textures/clip.mp4"]
            assert "packed_zip" not in job

    def _run_pack_with_manifest(self, client, tmp_path, name, manifest_extra,
                                shared_path=None):
        """Drive pack_blend_job with a mocked Blender returning a manifest."""
        blend = tmp_path / "scene.blend"
        blend.write_bytes(b"BLENDER-fake")
        job_id, job = _submit_job(client, name, 1, 2)
        with LOCK:
            job["status"] = "packing"
            job["blend_path"] = str(blend)
            job["blend_filename"] = "scene.blend"
            if shared_path:
                job["shared_path"] = str(blend)  # must exist on disk
        pack_dir = coord.BLEND_DIR / f"{job_id}_pack"

        def fake_run(cmd, **kwargs):
            pack_dir.mkdir(parents=True, exist_ok=True)
            (pack_dir / "scene.blend").write_bytes(b"BLENDER-packed")
            m = MagicMock()
            payload = {"ok": True, "blend": "scene.blend", "copied": 0,
                       "bytes": 1, "missing": [], "warnings": []}
            payload.update(manifest_extra)
            m.stdout = "RENDERHIVE_PACK:" + json.dumps(payload)
            m.stderr = ""
            return m

        with patch.object(coord, "detect_blender", return_value="blender.exe"), \
             patch.object(coord.subprocess, "run", side_effect=fake_run):
            coord.pack_blend_job(job_id)
        return job

    def test_relative_paths_error_mentions_make_paths_absolute(
            self, client, tmp_path):
        """Uploaded blend with missing RELATIVE deps gets the specific
        'Make All Paths Absolute' guidance, not the generic message."""
        job = self._run_pack_with_manifest(client, tmp_path, "relmiss", {
            "missing": ["C:/RenderHive/blends/clip.mp4"],
            "missing_relative": ["//clip.mp4"],
        })
        with LOCK:
            assert job["status"] == "failed"
            assert "Make All Paths Absolute" in job["pack_error"]
            assert "Shared Path" in job["pack_error"]

    def test_shared_path_missing_deps_get_generic_error(
            self, client, tmp_path):
        """Via shared path, relative paths DID resolve — a missing file is
        genuinely gone, so suggesting 'Make Paths Absolute' would mislead."""
        job = self._run_pack_with_manifest(client, tmp_path, "sharedmiss", {
            "missing": ["C:/projects/gone.mp4"],
            "missing_relative": ["//gone.mp4"],
        }, shared_path=True)
        with LOCK:
            assert job["status"] == "failed"
            assert "Make All Paths Absolute" not in job["pack_error"]
            assert "gone.mp4" in job["pack_error"]

    def test_pack_progress_cleared_when_done(self, client, tmp_path):
        job = self._run_pack_with_manifest(client, tmp_path, "prog", {})
        try:
            with LOCK:
                assert job["status"] == "queued"
                assert "pack_progress" not in job
        finally:
            zp = job.get("packed_zip")
            if zp and os.path.exists(zp):
                os.remove(zp)

    def test_packing_job_gets_no_work(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "packing1", 1, 3)
        with LOCK:
            coord.JOBS[job_id]["status"] = "packing"
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"] is None

    def test_recompute_keeps_packing_status(self, client):
        job_id, job = _submit_job(client, "packing2", 1, 3)
        with LOCK:
            coord.JOBS[job_id]["status"] = "packing"
            coord._recompute_job_statuses()
            assert coord.JOBS[job_id]["status"] == "packing"

    def test_assignment_payload_packed_flag(self, client):
        _register_worker(client, "w1")
        job_id, job = _submit_job(client, "packed1", 1, 3)
        with LOCK:
            coord.JOBS[job_id]["packed_zip"] = str(
                coord.BLEND_DIR / f"{job_id}_pack.zip")
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"]["packed"] is True

    def test_assignment_payload_unpacked_by_default(self, client):
        _register_worker(client, "w1")
        _submit_job(client, "packed2", 1, 3)
        r = client.post("/api/workers/w1/next", json={"id": "w1", "name": "w1"})
        assert r.get_json()["assignment"]["packed"] is False

    def test_blend_endpoint_serves_zip_when_packed(self, client):
        job_id, job = _submit_job(client, "packed3", 1, 2)
        zip_path = coord.BLEND_DIR / f"{job_id}_pack.zip"
        zip_path.write_bytes(b"PK\x03\x04fakezip")
        try:
            with LOCK:
                coord.JOBS[job_id]["packed_zip"] = str(zip_path)
            r = client.get(f"/api/jobs/{job_id}/blend")
            assert r.status_code == 200
            assert r.data == b"PK\x03\x04fakezip"
            assert f"{job_id}_pack.zip" in r.headers["Content-Disposition"]
        finally:
            r.close()  # release send_file's handle so Windows allows unlink
            zip_path.unlink()

    def test_delete_removes_packed_zip(self, client):
        job_id, job = _submit_job(client, "packed4", 1, 2)
        zip_path = coord.BLEND_DIR / f"{job_id}_pack.zip"
        zip_path.write_bytes(b"zip")
        with LOCK:
            coord.JOBS[job_id]["packed_zip"] = str(zip_path)
        r = client.post(f"/api/jobs/{job_id}/delete", json={})
        assert r.get_json()["ok"] is True
        assert not zip_path.exists()

    def test_job_summary_exposes_pack_fields(self, client):
        job_id, job = _submit_job(client, "packed5", 1, 2)
        with LOCK:
            job["packed_zip"] = "x.zip"
            job["pack_missing"] = ["C:/gone.mp4"]
            s = coord.job_summary(job)
        assert s["packed"] is True
        assert s["pack_missing"] == ["C:/gone.mp4"]

    def test_pack_failure_fails_the_job(self, client, tmp_path):
        """Packing was explicitly requested, so failure must be fatal —
        rendering anyway would silently produce pink textures."""
        blend = tmp_path / "scene.blend"
        blend.write_bytes(b"BLENDER-fake")
        job_id, job = _submit_job(client, "packfail", 1, 2)
        with LOCK:
            job["status"] = "packing"
            job["blend_path"] = str(blend)
        with patch.object(coord, "detect_blender", return_value=None):
            coord.BLENDER_PATH = None
            coord.pack_blend_job(job_id)
        with LOCK:
            assert job["status"] == "failed"
            assert "Blender not found" in job["pack_error"]
            assert "packed_zip" not in job
            for fr in job["frames"].values():
                assert fr["status"] == "failed"
                assert "dependency packing failed" in fr["last_error"]

    def test_retry_repacks_after_pack_failure(self, client, tmp_path):
        blend = tmp_path / "scene.blend"
        blend.write_bytes(b"BLENDER-fake")
        job_id, job = _submit_job(client, "packretry", 1, 2)
        with LOCK:
            job["status"] = "failed"
            job["blend_path"] = str(blend)
            job["pack_error"] = "Blender not found on this machine"
            for fr in job["frames"].values():
                fr["status"] = "failed"
                fr["last_error"] = "dependency packing failed: ..."
        with patch.object(coord.threading, "Thread") as mock_thread:
            r = client.post(f"/api/jobs/{job_id}/retry")
        d = r.get_json()
        assert d["ok"] is True
        assert d["repacking"] is True
        assert d["retried"] == 2
        with LOCK:
            assert job["status"] == "packing"
            assert "pack_error" not in job
            for fr in job["frames"].values():
                assert fr["status"] == "pending"
        assert any(kw.get("target") is coord.pack_blend_job
                   for _a, kw in mock_thread.call_args_list)

    def test_retry_without_pack_error_does_not_repack(self, client):
        job_id, job = _submit_job(client, "plainretry", 1, 2)
        with LOCK:
            job["frames"]["1"]["status"] = "failed"
        r = client.post(f"/api/jobs/{job_id}/retry")
        d = r.get_json()
        assert d["repacking"] is False
        with LOCK:
            assert job["status"] == "queued"

    def test_pack_blend_job_skips_cancelled_job(self, client, tmp_path):
        job_id, job = _submit_job(client, "packcancel", 1, 2)
        with LOCK:
            job["status"] = "cancelled"
        coord.pack_blend_job(job_id)
        with LOCK:
            assert job["status"] == "cancelled"
            assert "pack_error" not in job

    def test_pack_blend_job_success_path(self, client, tmp_path):
        """Mock the Blender subprocess: it 'creates' the packed project and
        prints the manifest. Verify the zip is built and the job queued."""
        blend = tmp_path / "scene.blend"
        blend.write_bytes(b"BLENDER-fake")
        job_id, job = _submit_job(client, "packok", 1, 2)
        with LOCK:
            job["status"] = "packing"
            job["blend_path"] = str(blend)
            job["blend_filename"] = "scene.blend"
        pack_dir = coord.BLEND_DIR / f"{job_id}_pack"
        zip_path = coord.BLEND_DIR / f"{job_id}_pack.zip"

        def fake_run(cmd, **kwargs):
            # The original filename must be passed so the packed blend isn't
            # saved under the job-id-prefixed upload name
            assert cmd[-1] == "scene.blend"
            (pack_dir / "assets" / "videos").mkdir(parents=True)
            (pack_dir / "scene.blend").write_bytes(b"BLENDER-packed")
            (pack_dir / "assets" / "videos" / "clip.mp4").write_bytes(b"vid")
            m = MagicMock()
            m.stdout = ('RENDERHIVE_PACK:' + json.dumps(
                {"ok": True, "blend": "scene.blend", "copied": 1,
                 "bytes": 3, "missing": [], "warnings": []}))
            m.stderr = ""
            return m

        try:
            with patch.object(coord, "detect_blender",
                              return_value="blender.exe"), \
                 patch.object(coord.subprocess, "run", side_effect=fake_run):
                coord.pack_blend_job(job_id)
            with LOCK:
                assert job["status"] == "queued"
                assert job.get("pack_error") is None
                assert job["packed_zip"] == str(zip_path)
            assert zip_path.exists()
            import zipfile
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
            assert "scene.blend" in names
            assert "assets/videos/clip.mp4" in names
            assert not pack_dir.exists()  # working folder cleaned up
        finally:
            if zip_path.exists():
                zip_path.unlink()
