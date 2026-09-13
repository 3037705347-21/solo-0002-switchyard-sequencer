"""Workflow check: idempotent batched pull-run advancement.

Covers four scenarios on POST /api/pull-runs/{code}/advance:

1. single step with a request identity,
2. a consecutive multi-step batch,
3. duplicate submissions of the same request identity (no extra moves),
4. a batch blocked mid-way by a resource conflict, which must commit a
   clear executed boundary, replay safely, and resume from that boundary.

The final assertions prove a completed run never re-assembles cars when
its completing request is retried.
"""

from __future__ import annotations

from typing import Any

from support import ApiClient, run_check

RUN_A = "RUN-OB-41"
RUN_B = "RUN-OB-42"


def _bay_metrics(yard: dict[str, Any], code: str = "X1") -> dict[str, Any]:
    for bay in yard["metrics"]["transfer_bays"]:
        if bay["code"] == code:
            return bay
    raise AssertionError(f"bay {code} missing from yard metrics")


def _track_metrics(yard: dict[str, Any], code: str) -> dict[str, Any]:
    for track in yard["metrics"]["track_metrics"]:
        if track["code"] == code:
            return track
    raise AssertionError(f"track {code} missing from yard metrics")


def _setup(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-04", "dispatcher": "MA", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-41",
            "route": "RAIL-41",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": [
                {"code": "C-N4-41", "kind": "BOX", "destination": "N4", "loaded": True, "length_m": 18, "danger_class": "NONE"},
                {"code": "C-N4-42", "kind": "BOX", "destination": "N4", "loaded": False, "length_m": 18, "danger_class": "NONE"},
                {"code": "C-N4-43", "kind": "HOPPER", "destination": "N4", "loaded": True, "length_m": 16, "danger_class": "NONE"},
                {"code": "C-N4-44", "kind": "BOX", "destination": "N4", "loaded": False, "length_m": 18, "danger_class": "NONE"},
                {"code": "C-E7-41", "kind": "BOX", "destination": "E7", "loaded": True, "length_m": 18, "danger_class": "NONE"},
                {"code": "C-E7-42", "kind": "FLAT", "destination": "E7", "loaded": False, "length_m": 19, "danger_class": "NONE"},
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-41/classify", {})
    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-41", "destination": "N4", "car_codes": ["C-N4-41"]})
    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-42", "destination": "E7", "car_codes": ["C-E7-41"]})
    planned_a = api.expect_ok("POST", "/api/outbound-trains/OB-41/sequencer", {"transfer_code": "X1"})
    planned_b = api.expect_ok("POST", "/api/outbound-trains/OB-42/sequencer", {"transfer_code": "X1"})
    assert planned_a["pull_run"]["code"] == RUN_A
    assert planned_b["pull_run"]["code"] == RUN_B
    verbs_a = [step["verb"] for step in planned_a["pull_run"]["steps"]]
    assert verbs_a == ["BUFFER", "BUFFER", "BUFFER", "PULL", "RETURN", "RETURN", "RETURN"]
    verbs_b = [step["verb"] for step in planned_b["pull_run"]["steps"]]
    assert verbs_b == ["BUFFER", "PULL", "RETURN"]


def _scenario_single_step(api: ApiClient) -> None:
    first = api.expect_ok("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 1, "request_id": "REQ-41-1"})
    assert first["replayed"] is False
    assert first["request_id"] == "REQ-41-1"
    assert first["executed_steps"] == 1
    assert first["completed"] is False
    assert first["steps_executed"] == [
        {
            "index": 0,
            "verb": "BUFFER",
            "car_code": "C-N4-44",
            "source_code": "N4-A",
            "target_code": "X1",
            "detail": "buffered C-N4-44 to X1",
        }
    ]
    assert first["before"] == {"run_state": "QUEUED", "current_step": 0, "remaining": 7, "assembled_car_codes": []}
    assert first["after"]["run_state"] == "RUNNING"
    assert first["after"]["current_step"] == 1
    assert first["remaining"] == 6


def _scenario_multi_step(api: ApiClient) -> None:
    batch = api.expect_ok("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 2, "request_id": "REQ-41-2"})
    assert batch["replayed"] is False
    assert batch["executed_steps"] == 2
    moves = [(step["index"], step["verb"], step["car_code"]) for step in batch["steps_executed"]]
    assert moves == [(1, "BUFFER", "C-N4-43"), (2, "BUFFER", "C-N4-42")]
    assert batch["before"]["current_step"] == 1
    assert batch["after"]["current_step"] == 3
    assert batch["remaining"] == 4


def _scenario_duplicate_request(api: ApiClient) -> None:
    yard_before = api.expect_ok("GET", "/api/yard")
    replay = api.expect_ok("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 2, "request_id": "REQ-41-2"})
    assert replay["replayed"] is True
    assert replay["executed_steps"] == 2
    assert replay["after"]["current_step"] == 3
    # a retry with a different step count still returns the recorded outcome
    replayed_other_steps = api.expect_ok("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 5, "request_id": "REQ-41-2"})
    assert replayed_other_steps["replayed"] is True
    assert replayed_other_steps["executed_steps"] == 2
    yard_after = api.expect_ok("GET", "/api/yard")
    assert yard_after["metrics"]["version"] == yard_before["metrics"]["version"]
    assert yard_after["metrics"]["transfer_bays"] == yard_before["metrics"]["transfer_bays"]
    assert _bay_metrics(yard_after)["cars"] == 3


def _scenario_blocked_step(api: ApiClient) -> None:
    # another run parks a car on top of the shared transfer bay
    other = api.expect_ok("POST", f"/api/pull-runs/{RUN_B}/advance", {"steps": 1, "request_id": "REQ-42-1"})
    assert other["steps_executed"][0]["car_code"] == "C-E7-42"
    # the batch pulls its planned car, then blocks on the occupied bay top
    blocked = api.expect_error("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 3, "request_id": "REQ-41-4"})
    assert blocked["code"] == "RESOURCE_BUSY"
    details = blocked["details"]
    assert details["request_id"] == "REQ-41-4"
    assert details["executed_steps"] == 1
    assert [(s["index"], s["verb"]) for s in details["steps_executed"]] == [(3, "PULL")]
    assert details["failed_step"]["index"] == 4
    assert details["failed_step"]["verb"] == "RETURN"
    assert details["boundary"] == {"run_state": "RUNNING", "current_step": 4, "remaining": 3}
    # the executed boundary is committed: the pulled car is assembled exactly once
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["assembled"] == 1
    assert _bay_metrics(yard)["cars"] == 4
    assert _bay_metrics(yard)["top_car"] == "C-E7-42"
    # replaying the blocked request reports the recorded outcome without moving
    version_before = yard["metrics"]["version"]
    replay = api.expect_error("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 3, "request_id": "REQ-41-4"})
    assert replay["code"] == "RESOURCE_BUSY"
    assert replay["details"]["replayed"] is True
    assert replay["details"]["executed_steps"] == 1
    # a fresh identity resumes from the boundary and is still blocked, moving nothing
    retry = api.expect_error("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 3, "request_id": "REQ-41-5"})
    assert retry["details"]["executed_steps"] == 0
    assert retry["details"]["boundary"]["current_step"] == 4
    yard = api.expect_ok("GET", "/api/yard")
    assert _bay_metrics(yard)["cars"] == 4
    # clear the blocker by finishing the other run, then resume to completion
    done_b = api.expect_ok("POST", f"/api/pull-runs/{RUN_B}/advance", {"steps": 2, "request_id": "REQ-42-2"})
    assert done_b["completed"] is True
    assert done_b["outbound"]["state"] == "READY"
    resumed = api.expect_ok("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 3, "request_id": "REQ-41-6"})
    assert resumed["before"]["current_step"] == 4
    assert resumed["executed_steps"] == 3
    assert resumed["completed"] is True
    assert resumed["pull_run"]["state"] == "COMPLETED"
    assert resumed["outbound"]["assembled_car_codes"] == ["C-N4-41"]
    assert version_before > 0  # sanity: the blocked request did commit earlier


def _scenario_completed_replay(api: ApiClient) -> None:
    yard_before = api.expect_ok("GET", "/api/yard")
    again = api.expect_ok("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 3, "request_id": "REQ-41-6"})
    assert again["replayed"] is True
    assert again["completed"] is True
    assert again["outbound"]["assembled_car_codes"] == ["C-N4-41"]
    yard_after = api.expect_ok("GET", "/api/yard")
    assert yard_after["metrics"]["version"] == yard_before["metrics"]["version"]
    # a new identity on a completed run is rejected, with or without a request id
    conflict = api.expect_error("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 1, "request_id": "REQ-41-7"})
    assert conflict["code"] == "CONFLICT"
    conflict = api.expect_error("POST", f"/api/pull-runs/{RUN_A}/advance", {"steps": 1})
    assert conflict["code"] == "CONFLICT"
    # retries never duplicated assembly or buffering
    counts = yard_after["metrics"]["car_state_counts"]
    assert counts["assembled"] == 2
    assert counts["standing"] == 4
    assert _bay_metrics(yard_after)["cars"] == 0
    assert _track_metrics(yard_after, "N4-A")["cars"] == 3


def run(api: ApiClient) -> None:
    _setup(api)
    _scenario_single_step(api)
    _scenario_multi_step(api)
    _scenario_duplicate_request(api)
    _scenario_blocked_step(api)
    _scenario_completed_replay(api)


if __name__ == "__main__":
    raise SystemExit(run_check("wf_advance_idempotency", run))
