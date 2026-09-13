"""Workflow check: pull run views over the real HTTP API.

Covers QUEUED, RUNNING, COMPLETED, FAILED, and re-planning interception.
Every view is read-only: queries never advance runs or retrigger actions,
and a server restart against the same data directory restores the full
context for a fresh client.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from support import PROJECT_ROOT, SRC_DIR, ApiClient, free_port

N4_CARS = [
    {
        "code": "C-VW-41",
        "kind": "BOX",
        "destination": "N4",
        "loaded": True,
        "length_m": 18,
        "danger_class": "NONE",
    },
    {
        "code": "C-VW-42",
        "kind": "REEFER",
        "destination": "N4",
        "loaded": False,
        "length_m": 20,
        "danger_class": "NONE",
    },
    {
        "code": "C-VW-43",
        "kind": "BOX",
        "destination": "N4",
        "loaded": True,
        "length_m": 19,
        "danger_class": "NONE",
    },
]


class PersistentServer:
    """Server bound to a caller-owned data directory so it can be restarted."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "switchyard.entry.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--data-dir",
                str(data_dir),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.api = ApiClient(self.base_url)

    def wait_ready(self, timeout: float = 8.0) -> None:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise AssertionError(f"server exited early:\n{output}")
            try:
                status, body = self.api.get("/api/health")
                if status == 200 and body.get("ok"):
                    return
            except Exception:
                time.sleep(0.05)
        raise AssertionError("server did not become ready")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()


def seed_queued_run(api: ApiClient) -> str:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-41", "dispatcher": "MA", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-41",
            "route": "RAIL-41",
            "arrival_at": "2026-09-13T08:20:00Z",
            "cars": N4_CARS,
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-41/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-41", "destination": "N4", "car_codes": ["C-VW-42", "C-VW-41"]},
    )
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-41/sequencer",
        {"transfer_code": "X1"},
    )
    return sequenced["pull_run"]["code"]


def _view_step_codes(view: dict) -> list[tuple[str, str]]:
    return [(step["verb"], step["car_code"]) for step in view["steps"]]


def assert_queued(api: ApiClient, run_code: str) -> None:
    detail = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail["code"] == run_code
    assert detail["state"] == "QUEUED"
    assert detail["shift_code"] == "SHIFT-41"
    assert detail["outbound_code"] == "OB-41"
    assert detail["transfer_code"] == "X1"
    assert detail["progress"] == {
        "total_steps": 6,
        "completed_steps": 0,
        "remaining_steps": 6,
        "current_step": 1,
    }
    expected_steps = [
        ("BUFFER", "C-VW-43"),
        ("PULL", "C-VW-42"),
        ("RETURN", "C-VW-43"),
        ("BUFFER", "C-VW-43"),
        ("PULL", "C-VW-41"),
        ("RETURN", "C-VW-43"),
    ]
    assert _view_step_codes(detail) == expected_steps
    statuses = [step["status"] for step in detail["steps"]]
    assert statuses == ["CURRENT", "PENDING", "PENDING", "PENDING", "PENDING", "PENDING"]
    current = detail["current_operation"]
    assert current["step_number"] == 1
    assert current["verb"] == "BUFFER"
    assert current["car_code"] == "C-VW-43"
    assert current["source_code"] == "N4-A"
    assert current["target_code"] == "X1"
    assert current["readiness"] == "WAITING"
    assert current["car"]["state"] == "STANDING"
    assert detail["readiness"] == "WAITING"
    assert detail["blocked_reason"] == "queued: waiting for the first advance"
    outbound = detail["outbound"]
    assert outbound["code"] == "OB-41"
    assert outbound["destination"] == "N4"
    assert outbound["state"] == "PLANNED"
    assert outbound["planned_car_codes"] == ["C-VW-42", "C-VW-41"]
    assert outbound["assembled_car_codes"] == []
    assert outbound["remaining_car_codes"] == ["C-VW-42", "C-VW-41"]
    assert outbound["assembled_count"] == 0
    listing = api.expect_ok("GET", "/api/pull-runs?state=QUEUED")
    assert listing["count"] == 1
    assert listing["pull_runs"][0]["code"] == run_code
    assert listing["state_counts"]["QUEUED"] == 1
    assert listing["filters"] == {"shift_code": None, "states": ["QUEUED"]}
    # Shift filter: matches and misses.
    assert api.expect_ok("GET", "/api/pull-runs?shift=SHIFT-41")["count"] == 1
    assert api.expect_ok("GET", "/api/pull-runs?shift=SHIFT-OTHER")["count"] == 0


def assert_replan_blocked(api: ApiClient, run_code: str) -> None:
    before = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    error = api.expect_error("POST", "/api/outbound-trains/OB-41/sequencer", {"transfer_code": "X1"})
    assert error["code"] in {"VALIDATION_ERROR", "CONFLICT"}, error
    after = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    # Re-planning rejection must leave the run and its progress untouched.
    assert after["state"] == before["state"]
    assert after["progress"] == before["progress"]
    assert _view_step_codes(after) == _view_step_codes(before)


def assert_queries_are_read_only(api: ApiClient, run_code: str) -> None:
    yard_before = api.expect_ok("GET", "/api/yard")
    view_before = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    list_before = api.expect_ok("GET", "/api/pull-runs")
    for _ in range(3):
        api.expect_ok("GET", f"/api/pull-runs/{run_code}")
        api.expect_ok("GET", "/api/pull-runs?state=QUEUED,RUNNING,COMPLETED,FAILED&shift=SHIFT-41")
    yard_after = api.expect_ok("GET", "/api/yard")
    view_after = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    list_after = api.expect_ok("GET", "/api/pull-runs")
    assert yard_after["metrics"]["version"] == yard_before["metrics"]["version"]
    assert view_after["state"] == view_before["state"]
    assert view_after["progress"] == view_before["progress"]
    assert list_after["count"] == list_before["count"]
    assert list_after["state_counts"] == list_before["state_counts"]


def assert_running(api: ApiClient, run_code: str) -> None:
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    assert advanced["executed_steps"] == 1
    assert advanced["completed"] is False
    detail = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail["state"] == "RUNNING"
    assert detail["started_at"] is not None
    assert detail["blocked_reason"] is None
    assert detail["progress"]["completed_steps"] == 1
    assert detail["progress"]["remaining_steps"] == 5
    assert detail["progress"]["current_step"] == 2
    statuses = [step["status"] for step in detail["steps"]]
    assert statuses == ["DONE", "CURRENT", "PENDING", "PENDING", "PENDING", "PENDING"]
    current = detail["current_operation"]
    assert current["verb"] == "PULL"
    assert current["car_code"] == "C-VW-42"
    assert current["readiness"] == "READY"
    assert current["blocked_reason"] is None
    assembled = detail["outbound"]["assembled_car_codes"]
    # The first action was BUFFER, so nothing is assembled yet.
    assert assembled == []
    running = api.expect_ok("GET", "/api/pull-runs?state=RUNNING")
    assert running["count"] == 1
    queued = api.expect_ok("GET", "/api/pull-runs?state=QUEUED")
    assert queued["count"] == 0
    # Advance one more (PULL) and confirm the assembled car shows in the view.
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    detail = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail["progress"]["completed_steps"] == 2
    assert detail["outbound"]["assembled_car_codes"] == ["C-VW-42"]
    assert detail["outbound"]["assembled_cars"][0]["state"] == "ASSEMBLED"
    assert detail["current_operation"]["verb"] == "RETURN"
    # Re-planning an in-flight outbound is rejected too.
    assert_replan_blocked(api, run_code)


def assert_completed(api: ApiClient, run_code: str) -> None:
    # Advancing with more steps than remain completes the run without error.
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 20})
    assert advanced["completed"] is True
    assert advanced["pull_run"]["state"] == "COMPLETED"
    detail = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail["state"] == "COMPLETED"
    assert detail["completed_at"] is not None
    assert detail["failed_at"] is None
    assert detail["error"] is None
    assert detail["blocked_reason"] is None
    assert detail["readiness"] == "READY"
    assert detail["current_operation"] is None
    assert detail["progress"] == {
        "total_steps": 6,
        "completed_steps": 6,
        "remaining_steps": 0,
        "current_step": None,
    }
    assert all(step["status"] == "DONE" for step in detail["steps"])
    outbound = detail["outbound"]
    assert outbound["state"] == "READY"
    assert outbound["assembled_car_codes"] == ["C-VW-42", "C-VW-41"]
    assert outbound["remaining_car_codes"] == []
    assert outbound["assembled_count"] == 2
    completed = api.expect_ok("GET", "/api/pull-runs?state=COMPLETED")
    assert [item["code"] for item in completed["pull_runs"]] == [run_code]
    active = api.expect_ok("GET", "/api/pull-runs?state=QUEUED,RUNNING")
    assert active["count"] == 0
    combined = api.expect_ok("GET", "/api/pull-runs?state=COMPLETED&shift=SHIFT-41")
    assert combined["count"] == 1
    assert api.expect_ok("GET", "/api/pull-runs?state=COMPLETED&shift=SHIFT-OTHER")["count"] == 0
    # Further advances are rejected and do not retrigger any action.
    blocked = api.expect_error("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    assert blocked["code"] == "CONFLICT", blocked
    detail_again = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail_again["progress"] == detail["progress"]
    # Departure follows completion; the view stays linked to the outbound train.
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-41/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    detail = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail["state"] == "COMPLETED"
    assert detail["outbound"]["state"] == "DEPARTED"
    assert detail["outbound"]["departed_at"] is not None


def seed_failed_run(api: ApiClient) -> str:
    api.expect_ok(
        "POST",
        "/api/shifts/SHIFT-41/close",
        {},
    )
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-42", "dispatcher": "NZ", "opened_at": "2026-09-13T18:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-42",
            "route": "RAIL-42",
            "arrival_at": "2026-09-13T18:20:00Z",
            "cars": [
                {
                    "code": "C-VW-44",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-VW-45",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-42/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-42", "destination": "N4", "car_codes": ["C-VW-44"]},
    )
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-42/sequencer",
        {"transfer_code": "X1"},
    )
    return sequenced["pull_run"]["code"]


def inject_top_mismatch(data_dir: Path) -> None:
    """Tamper with persisted stacks to diverge reality from the planned step."""
    state_path = data_dir / "yard-state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    for track in payload["tracks"]:
        if track["code"] == "N4-A":
            track["stack"].append("C-INTRUDER")
    state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def assert_failed(api: ApiClient, run_code: str) -> None:
    error = api.expect_error("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    assert error["code"] == "RESOURCE_BUSY", error
    assert "C-VW-45" in error["message"] or "C-INTRUDER" in error["message"]
    detail = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail["state"] == "FAILED"
    assert detail["failed_at"] is not None
    assert detail["shift_code"] == "SHIFT-42"
    assert detail["readiness"] == "FAILED"
    assert detail["current_operation"]["readiness"] == "FAILED"
    assert detail["current_operation"]["car_code"] == "C-VW-45"
    assert detail["blocked_reason"]
    assert "C-INTRUDER" in detail["blocked_reason"]
    assert detail["error"] == detail["blocked_reason"]
    assert detail["progress"]["completed_steps"] == 0
    statuses = [(step["verb"], step["status"]) for step in detail["steps"]]
    assert statuses[0] == ("BUFFER", "CURRENT")
    assert all(status == "SKIPPED" for _, status in statuses[1:])
    # The failed BUFFER never moved any car.
    assert detail["steps"][0]["car"]["state"] == "STANDING"
    failed = api.expect_ok("GET", "/api/pull-runs?state=FAILED")
    assert [item["code"] for item in failed["pull_runs"]] == [run_code]
    assert failed["state_counts"]["FAILED"] == 1
    shift_only = api.expect_ok("GET", "/api/pull-runs?shift=SHIFT-42")
    assert [item["code"] for item in shift_only["pull_runs"]] == [run_code]
    # Failed runs cannot be re-advanced or re-planned.
    again = api.expect_error("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    assert again["code"] == "CONFLICT", again
    assert_replan_blocked(api, run_code)
    detail_again = api.expect_ok("GET", f"/api/pull-runs/{run_code}")
    assert detail_again["state"] == "FAILED"
    assert detail_again["progress"] == detail["progress"]
    assert detail_again["blocked_reason"] == detail["blocked_reason"]
    # Invalid state filter is a validation error, not a silent match-all.
    bad = api.expect_error("GET", "/api/pull-runs?state=ABANDONED")
    assert bad["code"] == "VALIDATION_ERROR", bad
    missing = api.get("/api/pull-runs/RUN-DOES-NOT-EXIST")
    assert missing[0] == 404


def run() -> None:
    temp_dir = tempfile.TemporaryDirectory(prefix="switchyard-view-")
    data_dir = Path(temp_dir.name) / "data"
    try:
        server = PersistentServer(data_dir)
        server.wait_ready()
        run_code = seed_queued_run(server.api)
        assert_queued(server.api, run_code)
        assert_replan_blocked(server.api, run_code)
        assert_queries_are_read_only(server.api, run_code)
        assert_running(server.api, run_code)
        assert_completed(server.api, run_code)

        # A second shift produces a FAILED run for the failure view.
        failed_code = seed_failed_run(server.api)
        server.stop()

        # Diverge the persisted stack while the service is stopped.
        inject_top_mismatch(data_dir)

        restarted = PersistentServer(data_dir)
        restarted.wait_ready()
        fresh_client = ApiClient(restarted.base_url)  # brand new terminal
        # Both runs are recovered from the same persistent state.
        listing = fresh_client.expect_ok("GET", "/api/pull-runs")
        assert listing["count"] == 2
        assert {item["code"] for item in listing["pull_runs"]} == {run_code, failed_code}
        completed = fresh_client.expect_ok("GET", f"/api/pull-runs/{run_code}")
        assert completed["state"] == "COMPLETED"
        assert completed["outbound"]["state"] == "DEPARTED"
        assert completed["progress"]["completed_steps"] == 6
        assert completed["shift_code"] == "SHIFT-41"
        assert_failed(fresh_client, failed_code)
        # Shift filtering across the two shifts.
        shift_41 = fresh_client.expect_ok("GET", "/api/pull-runs?shift=SHIFT-41")
        assert [item["code"] for item in shift_41["pull_runs"]] == [run_code]
        shift_42 = fresh_client.expect_ok("GET", "/api/pull-runs?shift=SHIFT-42")
        assert [item["code"] for item in shift_42["pull_runs"]] == [failed_code]
        restarted.stop()

        # Restart once more: querying again must still not mutate anything.
        second = PersistentServer(data_dir)
        second.wait_ready()
        yard = second.api.expect_ok("GET", "/api/yard")
        for _ in range(2):
            second.api.expect_ok("GET", f"/api/pull-runs/{run_code}")
            second.api.expect_ok("GET", f"/api/pull-runs/{failed_code}")
            second.api.expect_ok("GET", "/api/pull-runs")
        assert second.api.expect_ok("GET", "/api/yard")["metrics"]["version"] == yard["metrics"]["version"]
        second.stop()
    finally:
        temp_dir.cleanup()


if __name__ == "__main__":
    try:
        run()
    except AssertionError:
        raise SystemExit(1)
    print("OK wf_pull_run_view")
