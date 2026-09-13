"""Workflow check: closure precheck previews blockers without mutating state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support import ApiClient, RunningServer

STATE_FILE = "yard-state.json"


def car_payload(code: str, destination: str = "N4", kind: str = "BOX", length_m: int = 18) -> dict[str, Any]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length_m,
        "danger_class": "NONE",
    }


def open_shift(api: ApiClient, code: str) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": code, "dispatcher": "QA", "opened_at": "2026-09-08T08:00:00Z"},
    )


def receive_intake(api: ApiClient, code: str, cars: list[dict[str, Any]]) -> None:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {"code": code, "route": f"RAIL-{code}", "arrival_at": "2026-09-08T09:00:00Z", "cars": cars},
    )


def precheck(api: ApiClient, shift_code: str) -> dict[str, Any]:
    return api.expect_ok("POST", f"/api/shifts/{shift_code}/precheck", {})


def kind_code_pairs(blockers: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(item["kind"], item["code"]) for item in blockers]


def close_blockers(api: ApiClient, shift_code: str) -> list[dict[str, Any]]:
    error = api.expect_error("POST", f"/api/shifts/{shift_code}/close", {})
    assert error["code"] == "RESOURCE_BUSY"
    return list(error["details"]["blockers"])


def assert_matches_close(preview: dict[str, Any], actual: list[dict[str, Any]]) -> None:
    assert kind_code_pairs(preview["blockers"]) == kind_code_pairs(actual)
    assert [item["message"] for item in preview["blockers"]] == [item["message"] for item in actual]


def assert_unchanged(api: ApiClient, before: dict[str, Any]) -> None:
    after = api.expect_ok("GET", "/api/yard")["metrics"]
    for key in ("version", "event_count", "car_state_counts"):
        assert after[key] == before[key], f"precheck changed metrics.{key}"


def scenario_no_blockers(api: ApiClient) -> None:
    open_shift(api, "SHIFT-P1")
    receive_intake(api, "INT-P1", [car_payload("C-P1-1"), car_payload("C-P1-2")])
    api.expect_ok("POST", "/api/intake-trains/INT-P1/classify", {})
    before = api.expect_ok("GET", "/api/yard")["metrics"]
    preview = precheck(api, "SHIFT-P1")
    assert preview["shift"]["state"] == "OPEN"
    assert preview["ready"] is True
    assert preview["blocker_count"] == 0
    assert preview["blockers"] == []
    assert_unchanged(api, before)
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-P1/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    error = api.expect_error("POST", "/api/shifts/SHIFT-P1/precheck", {})
    assert error["code"] == "RESOURCE_BUSY"
    error = api.expect_error("POST", "/api/shifts/SHIFT-NOPE/precheck", {})
    assert error["code"] == "NOT_FOUND"


def scenario_single_blocker(api: ApiClient) -> None:
    open_shift(api, "SHIFT-P2")
    receive_intake(api, "INT-P2", [car_payload("C-P2-1")])
    api.expect_ok("POST", "/api/intake-trains/INT-P2/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-P2", "destination": "N4", "car_codes": ["C-P2-1"]},
    )
    before = api.expect_ok("GET", "/api/yard")["metrics"]
    preview = precheck(api, "SHIFT-P2")
    assert preview["ready"] is False
    assert preview["blocker_count"] == 1
    (blocker,) = preview["blockers"]
    assert blocker["order"] == 1
    assert blocker["kind"] == "outbound"
    assert blocker["code"] == "OB-P2"
    assert blocker["state"] == "DRAFT"
    assert blocker["related"]["destination"] == "N4"
    assert blocker["related"]["planned_car_codes"] == ["C-P2-1"]
    assert blocker["next_step"]["action"] == "sequence_outbound"
    assert blocker["next_step"]["endpoint"] == "POST /api/outbound-trains/OB-P2/sequencer"
    assert_unchanged(api, before)
    assert_matches_close(preview, close_blockers(api, "SHIFT-P2"))
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-P2/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})
    api.expect_ok("POST", "/api/outbound-trains/OB-P2/depart", {})
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-P2/close", {})
    assert closed["shift"]["state"] == "CLOSED"


def scenario_multiple_blockers(api: ApiClient) -> None:
    open_shift(api, "SHIFT-P3")
    receive_intake(api, "INT-P3A", [car_payload("C-P3-1"), car_payload("C-P3-2"), car_payload("C-P3-3")])
    api.expect_ok("POST", "/api/intake-trains/INT-P3A/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-P3", "destination": "N4", "car_codes": ["C-P3-3", "C-P3-1"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-P3/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    total_steps = len(sequenced["pull_run"]["steps"])
    receive_intake(api, "INT-P3B", [car_payload("C-P3-8", destination="E7", kind="FLAT", length_m=16)])
    before = api.expect_ok("GET", "/api/yard")["metrics"]
    preview = precheck(api, "SHIFT-P3")
    assert preview["ready"] is False
    assert preview["blocker_count"] == 4
    assert [item["order"] for item in preview["blockers"]] == [1, 2, 3, 4]
    assert kind_code_pairs(preview["blockers"]) == [
        ("intake", "INT-P3B"),
        ("outbound", "OB-P3"),
        ("pull_run", run_code),
        ("unclassified_car", "C-P3-8"),
    ]
    by_kind = {item["kind"]: item for item in preview["blockers"]}
    intake_item = by_kind["intake"]
    assert intake_item["state"] == "OPEN"
    assert intake_item["related"]["consist_cars"] == 1
    assert intake_item["next_step"]["action"] == "classify_intake"
    assert intake_item["next_step"]["endpoint"] == "POST /api/intake-trains/INT-P3B/classify"
    outbound_item = by_kind["outbound"]
    assert outbound_item["state"] == "PLANNED"
    assert outbound_item["related"]["run_codes"] == [run_code]
    assert outbound_item["next_step"]["action"] == "advance_pull_run"
    assert outbound_item["next_step"]["endpoint"] == f"POST /api/pull-runs/{run_code}/advance"
    run_item = by_kind["pull_run"]
    assert run_item["state"] == "QUEUED"
    assert run_item["related"]["outbound_code"] == "OB-P3"
    assert run_item["related"]["remaining_steps"] == total_steps
    assert run_item["next_step"]["action"] == "advance_pull_run"
    car_item = by_kind["unclassified_car"]
    assert car_item["state"] == "RECEIVED"
    assert car_item["related"]["intake_code"] == "INT-P3B"
    assert car_item["next_step"]["action"] == "classify_intake"
    assert car_item["next_step"]["endpoint"] == "POST /api/intake-trains/INT-P3B/classify"
    assert_unchanged(api, before)
    assert_matches_close(preview, close_blockers(api, "SHIFT-P3"))
    api.expect_ok("POST", "/api/intake-trains/INT-P3B/classify", {})
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": total_steps})
    api.expect_ok("POST", "/api/outbound-trains/OB-P3/depart", {})
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-P3/close", {})
    assert closed["shift"]["state"] == "CLOSED"


def scenario_work_then_close(api: ApiClient) -> None:
    open_shift(api, "SHIFT-P4")
    receive_intake(api, "INT-P4", [car_payload("C-P4-1"), car_payload("C-P4-2"), car_payload("C-P4-3")])
    api.expect_ok("POST", "/api/intake-trains/INT-P4/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-P4", "destination": "N4", "car_codes": ["C-P4-3", "C-P4-1"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-P4/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    total_steps = len(sequenced["pull_run"]["steps"])
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    before = api.expect_ok("GET", "/api/yard")["metrics"]
    preview = precheck(api, "SHIFT-P4")
    assert kind_code_pairs(preview["blockers"]) == [("outbound", "OB-P4"), ("pull_run", run_code)]
    run_item = [item for item in preview["blockers"] if item["kind"] == "pull_run"][0]
    assert run_item["state"] == "RUNNING"
    assert run_item["related"]["current_step"] == 1
    assert run_item["related"]["remaining_steps"] == total_steps - 1
    again = precheck(api, "SHIFT-P4")
    assert again["blockers"] == preview["blockers"]
    assert_unchanged(api, before)
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": total_steps})
    assert advanced["completed"] is True
    api.expect_ok("POST", "/api/outbound-trains/OB-P4/depart", {})
    final_preview = precheck(api, "SHIFT-P4")
    assert final_preview["ready"] is True
    assert final_preview["blockers"] == []
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-P4/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    assert closed["snapshot"]["blockers"] == []


def _move_car(data_dir: Path, car_code: str, source_track: str, target_track: str) -> None:
    path = data_dir / STATE_FILE
    raw = json.loads(path.read_text(encoding="utf-8"))
    for track in raw["tracks"]:
        if track["code"] == source_track:
            track["stack"].remove(car_code)
        if track["code"] == target_track:
            track["stack"].append(car_code)
    for car in raw["cars"]:
        if car["code"] == car_code:
            car["location"] = target_track
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def scenario_maintenance_track(api: ApiClient, data_dir: Path) -> None:
    open_shift(api, "SHIFT-P5")
    receive_intake(api, "INT-P5", [car_payload("C-P5-1")])
    classified = api.expect_ok("POST", "/api/intake-trains/INT-P5/classify", {})
    track_code = classified["spots"][0]["track_code"]
    # simulate an out-of-band operational move onto the maintenance track
    _move_car(data_dir, "C-P5-1", track_code, "MAINT-1")
    preview = precheck(api, "SHIFT-P5")
    assert preview["ready"] is False
    assert kind_code_pairs(preview["blockers"]) == [("maintenance_track", "MAINT-1")]
    (blocker,) = preview["blockers"]
    assert blocker["state"] == "MAINTENANCE"
    assert blocker["related"]["cars"] == ["C-P5-1"]
    assert blocker["next_step"]["action"] == "clear_maintenance_track"
    assert_matches_close(preview, close_blockers(api, "SHIFT-P5"))
    # the crew clears the maintenance track out of band; closure then succeeds
    _move_car(data_dir, "C-P5-1", "MAINT-1", track_code)
    assert precheck(api, "SHIFT-P5")["ready"] is True
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-P5/close", {})
    assert closed["shift"]["state"] == "CLOSED"


def run(api: ApiClient, data_dir: Path) -> None:
    scenario_no_blockers(api)
    scenario_single_blocker(api)
    scenario_multiple_blockers(api)
    scenario_work_then_close(api)
    scenario_maintenance_track(api, data_dir)


if __name__ == "__main__":
    server = RunningServer()
    try:
        server.wait_ready()
        run(server.api, Path(server.temp_dir.name) / "data")
        print("OK wf_closure_precheck")
        raise SystemExit(0)
    finally:
        server.stop()
