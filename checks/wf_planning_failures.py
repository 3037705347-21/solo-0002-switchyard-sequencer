"""Workflow check: rich planning failures and side-effect-free rejection.

Phase A drives the live API into two simultaneous planning failures (a
blocked sequence and a reserved blocker) and confirms the error set matches
the yard state, that nothing is persisted on failure, and that following the
returned hints clears the related failure. Phase B seeds a crafted workspace
with seven distinct problems and confirms they are all returned at once and
stay consistent with the persisted yard. Phase C confirms a clean plan still
behaves exactly as before.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from support import ApiClient, RunningServer

from switchyard.domain.car import FreightCar
from switchyard.domain.enums import CarKind, CarState, OutboundState
from switchyard.domain.outbound import OutboundTrain
from switchyard.domain.shift import YardShift
from switchyard.domain.track import BufferBay
from switchyard.storage.repository import STATE_FILE, YardRepository
from switchyard.storage.seed import build_seed_workspace


def _car_payload(code: str) -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": "N4",
        "loaded": False,
        "length_m": 18,
        "danger_class": "NONE",
    }


def _failure_pairs(error: dict[str, object]) -> list[tuple[str, str]]:
    details = error["details"]
    return [(item["car_code"], item["reason"]) for item in details["failures"]]


def phase_a(api: ApiClient, data_dir: Path) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-90", "dispatcher": "QA", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-90",
            "route": "RAIL-90",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": [
                _car_payload(code)
                for code in ["C-A1-90", "C-B1-90", "C-B2-90", "C-A2-90", "C-TOP-90"]
            ],
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-90/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"

    # Reserve C-B2-90 on another outbound so it cannot be buffered later.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-HOLD-90", "destination": "N4", "car_codes": ["C-B2-90"]},
    )
    held = api.expect_ok("POST", "/api/outbound-trains/OB-HOLD-90/sequencer", {"transfer_code": "X1"})
    assert held["outbound"]["state"] == "PLANNED"
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-90", "destination": "N4", "car_codes": ["C-A1-90", "C-A2-90"]},
    )

    state_path = data_dir / STATE_FILE
    state_before = state_path.read_bytes()
    yard_before = api.expect_ok("GET", "/api/yard")
    shift_before = api.expect_ok("GET", "/api/shifts/SHIFT-90")

    # C-A1-90 is buried under a later-planned car and a reserved car: both
    # problems must come back in one response.
    status, body = api.request("POST", "/api/outbound-trains/OB-90/sequencer", {"transfer_code": "X1"})
    assert status == 422, body
    assert body["ok"] is False
    error = body["error"]
    assert error["code"] == "PLAN_VALIDATION_FAILED", error
    assert error["details"]["failure_count"] == 2
    assert error["details"]["outbound_code"] == "OB-90"
    assert error["details"]["transfer_code"] == "X1"
    assert error["fields"] == {"C-A1-90": ["blocked-sequence", "blocker-reserved"]}
    failures = error["details"]["failures"]
    assert _failure_pairs(error) == [("C-A1-90", "blocked-sequence"), ("C-A1-90", "blocker-reserved")]
    by_reason = {item["reason"]: item for item in failures}
    sequence = by_reason["blocked-sequence"]
    assert sequence["category"] == "act-first"
    assert sequence["location"] == "N4-A"
    assert sequence["conflict_with"] == "C-A2-90"
    assert sequence["blocked_by"] == ["C-TOP-90", "C-A2-90", "C-B2-90", "C-B1-90"]
    assert sequence["suggestion"] == {
        "action": "act-first",
        "target_code": "C-A2-90",
        "note": "pull C-A2-90 before C-A1-90; move it earlier in the plan",
    }
    reserved = by_reason["blocker-reserved"]
    assert reserved["category"] == "act-first"
    assert reserved["conflict_with"] == "C-B2-90"
    assert reserved["blocked_by"] == ["C-TOP-90", "C-A2-90", "C-B2-90", "C-B1-90"]
    assert reserved["suggestion"]["action"] == "act-first"
    assert reserved["suggestion"]["target_code"] == "OB-HOLD-90"
    assert "OB-HOLD-90" in reserved["message"]

    # The failed request must not leave reservations, runs, or events behind.
    assert state_path.read_bytes() == state_before
    yard_after = api.expect_ok("GET", "/api/yard")
    assert yard_after["metrics"] == yard_before["metrics"]
    shift_after = api.expect_ok("GET", "/api/shifts/SHIFT-90")
    assert shift_after["events"] == shift_before["events"]
    retry_status, retry_body = api.request(
        "POST", "/api/outbound-trains/OB-90/sequencer", {"transfer_code": "X1"}
    )
    assert retry_status == 422
    assert retry_body["error"]["code"] == "PLAN_VALIDATION_FAILED"

    # Follow the hint: finish and depart OB-HOLD-90, then re-plan. The
    # blocker-reserved failure must be gone from the next response.
    run_code = held["pull_run"]["code"]
    total_steps = len(held["pull_run"]["steps"])
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": total_steps})
    assert advanced["completed"] is True
    api.expect_ok("POST", "/api/outbound-trains/OB-HOLD-90/depart", {})
    status, body = api.request("POST", "/api/outbound-trains/OB-90/sequencer", {"transfer_code": "X1"})
    assert status == 422
    assert _failure_pairs(body["error"]) == [("C-A1-90", "blocked-sequence")]

    # A clean outbound still plans, reserves, and departs exactly as before.
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-91",
            "route": "RAIL-91",
            "arrival_at": "2026-09-13T10:00:00Z",
            "cars": [_car_payload("C-W1-91"), _car_payload("C-W2-91")],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-91/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-91", "destination": "N4", "car_codes": ["C-W2-91", "C-W1-91"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-91/sequencer", {"transfer_code": "X1"})
    steps = sequenced["pull_run"]["steps"]
    assert [(step["verb"], step["car_code"]) for step in steps] == [
        ("PULL", "C-W2-91"),
        ("PULL", "C-W1-91"),
    ]
    assert sequenced["pull_run"]["state"] == "QUEUED"
    assert sequenced["outbound"]["state"] == "PLANNED"
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 2
    run_code = sequenced["pull_run"]["code"]
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": len(steps)})
    assert advanced["completed"] is True
    assert advanced["outbound"]["assembled_car_codes"] == ["C-W2-91", "C-W1-91"]
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-91/depart", {})
    assert departed["departed_car_count"] == 2


def _crafted_car(code: str, destination: str, state: CarState, location: str | None) -> FreightCar:
    return FreightCar(
        code=code,
        kind=CarKind.BOX,
        destination=destination,
        loaded=False,
        length_m=18,
        state=state,
        location=location,
    )


def craft_workspace(data_dir: Path) -> None:
    workspace = build_seed_workspace()
    workspace.shifts["SHIFT-91"] = YardShift(
        code="SHIFT-91", dispatcher="QA", opened_at="2026-09-13T08:00:00Z"
    )
    workspace.buffer_bays["X1"] = BufferBay("X1", 1)
    for car in [
        _crafted_car("C-OK-91", "N4", CarState.STANDING, "N4-A"),
        _crafted_car("C-RSV-91", "N4", CarState.RESERVED, "N4-A"),
        _crafted_car("C-WIN-91", "N4", CarState.STANDING, "N4-A"),
        _crafted_car("C-DEST-91", "E7", CarState.STANDING, "E7-A"),
        _crafted_car("C-MNT-91", "N4", CarState.STANDING, "MAINT-1"),
        _crafted_car("C-LIMBO-91", "N4", CarState.STANDING, "N4-A"),
        _crafted_car("C-ASM-91", "N4", CarState.ASSEMBLED, "OB-KEEP"),
        _crafted_car("C-BUR-91", "N4", CarState.STANDING, "MIX-1"),
        _crafted_car("C-AB-91", "N4", CarState.STANDING, "MIX-1"),
        _crafted_car("C-AB-92", "N4", CarState.STANDING, "MIX-1"),
    ]:
        workspace.cars[car.code] = car
    workspace.tracks["N4-A"].stack = ["C-RSV-91", "C-OK-91", "C-WIN-91"]
    workspace.tracks["E7-A"].stack = ["C-DEST-91"]
    workspace.tracks["MAINT-1"].stack = ["C-MNT-91"]
    workspace.tracks["MIX-1"].stack = ["C-BUR-91", "C-AB-91", "C-AB-92"]
    workspace.outbounds["OB-KEEP"] = OutboundTrain(
        code="OB-KEEP",
        destination="N4",
        planned_car_codes=["C-RSV-91"],
        assembled_car_codes=["C-ASM-91"],
        state=OutboundState.PLANNED,
        run_codes=["RUN-OB-KEEP"],
        created_at="2026-09-13T08:30:00Z",
    )
    workspace.outbounds["OB-FAIL"] = OutboundTrain(
        code="OB-FAIL",
        destination="N4",
        planned_car_codes=[
            "C-GONE-91",
            "C-RSV-91",
            "C-DEST-91",
            "C-MNT-91",
            "C-LIMBO-91",
            "C-ASM-91",
            "C-BUR-91",
            "C-OK-91",
        ],
        state=OutboundState.DRAFT,
        created_at="2026-09-13T08:40:00Z",
    )
    workspace.outbounds["OB-WIN"] = OutboundTrain(
        code="OB-WIN",
        destination="N4",
        planned_car_codes=["C-WIN-91"],
        state=OutboundState.DRAFT,
        created_at="2026-09-13T08:50:00Z",
    )
    YardRepository(data_dir).save(workspace)


def phase_b(api: ApiClient, data_dir: Path) -> None:
    state_path = data_dir / STATE_FILE
    state_before = state_path.read_bytes()
    yard_before = api.expect_ok("GET", "/api/yard")
    counts = yard_before["metrics"]["car_state_counts"]
    assert (counts["standing"], counts["reserved"], counts["assembled"]) == (8, 1, 1)

    status, body = api.request("POST", "/api/outbound-trains/OB-FAIL/sequencer", {"transfer_code": "X1"})
    assert status == 422, body
    error = body["error"]
    assert error["code"] == "PLAN_VALIDATION_FAILED"
    assert error["details"]["failure_count"] == 7
    assert _failure_pairs(error) == [
        ("C-GONE-91", "car-missing"),
        ("C-RSV-91", "car-not-standing"),
        ("C-DEST-91", "destination-mismatch"),
        ("C-MNT-91", "track-not-operational"),
        ("C-LIMBO-91", "car-not-in-stack"),
        ("C-ASM-91", "car-not-standing"),
        ("C-BUR-91", "buffer-overflow"),
    ]
    failures = error["details"]["failures"]
    categories = {item["category"] for item in failures}
    assert categories == {"swap-car", "act-first", "fix-resource"}
    by_car = {}
    for item in failures:
        by_car.setdefault(item["car_code"], {})[item["reason"]] = item

    # Every failure must agree with the persisted yard state.
    persisted = json.loads(state_before.decode("utf-8"))
    persisted_cars = {car["code"]: car for car in persisted["cars"]}
    persisted_tracks = {track["code"]: track for track in persisted["tracks"]}
    persisted_outbounds = {train["code"]: train for train in persisted["outbounds"]}

    missing = by_car["C-GONE-91"]["car-missing"]
    assert "C-GONE-91" not in persisted_cars
    candidate = missing["suggestion"]["target_code"]
    assert candidate == "C-AB-91"
    assert persisted_cars[candidate]["state"] == "STANDING"
    assert persisted_cars[candidate]["destination"] == "N4"

    reserved = by_car["C-RSV-91"]["car-not-standing"]
    assert reserved["category"] == "act-first"
    assert reserved["car_state"] == persisted_cars["C-RSV-91"]["state"] == "RESERVED"
    assert reserved["held_by"] == "OB-KEEP"
    assert reserved["suggestion"]["target_code"] == "OB-KEEP"
    assert "C-RSV-91" in persisted_outbounds["OB-KEEP"]["planned_car_codes"]

    mismatch = by_car["C-DEST-91"]["destination-mismatch"]
    assert mismatch["category"] == "swap-car"
    assert mismatch["location"] == "E7-A"
    assert persisted_cars["C-DEST-91"]["destination"] == "E7"
    assert "E7" in mismatch["message"]
    assert mismatch["suggestion"]["target_code"] == candidate

    maintenance = by_car["C-MNT-91"]["track-not-operational"]
    assert maintenance["category"] == "fix-resource"
    assert maintenance["location"] == "MAINT-1"
    assert maintenance["suggestion"]["target_code"] == "MAINT-1"
    assert persisted_tracks["MAINT-1"]["state"] == "MAINTENANCE"

    limbo = by_car["C-LIMBO-91"]["car-not-in-stack"]
    assert limbo["category"] == "fix-resource"
    assert limbo["location"] == "N4-A"
    assert limbo["suggestion"]["target_code"] == "N4-A"
    assert "C-LIMBO-91" not in persisted_tracks["N4-A"]["stack"]

    assembled = by_car["C-ASM-91"]["car-not-standing"]
    assert assembled["category"] == "act-first"
    assert assembled["car_state"] == persisted_cars["C-ASM-91"]["state"] == "ASSEMBLED"
    assert assembled["held_by"] == "OB-KEEP"
    assert "C-ASM-91" in persisted_outbounds["OB-KEEP"]["assembled_car_codes"]

    overflow = by_car["C-BUR-91"]["buffer-overflow"]
    assert overflow["category"] == "fix-resource"
    assert overflow["location"] == "MIX-1"
    assert overflow["blocked_by"] == ["C-AB-92", "C-AB-91"]
    assert overflow["suggestion"]["target_code"] == "X1"
    assert persisted["buffer_bays"][0]["capacity_cars"] == 1

    # The clean car in the same plan produced no failure, and the failed
    # request left the workspace untouched.
    assert "C-OK-91" not in by_car
    assert state_path.read_bytes() == state_before
    yard_after = api.expect_ok("GET", "/api/yard")
    assert yard_after["metrics"] == yard_before["metrics"]
    shift = api.expect_ok("GET", "/api/shifts/SHIFT-91")
    assert shift["events"] == []


def phase_c(api: ApiClient) -> None:
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-WIN/sequencer", {"transfer_code": "X1"})
    run = sequenced["pull_run"]
    assert run["state"] == "QUEUED"
    assert [(step["verb"], step["car_code"], step["source_code"], step["target_code"]) for step in run["steps"]] == [
        ("PULL", "C-WIN-91", "N4-A", "OB-WIN")
    ]
    assert sequenced["outbound"]["state"] == "PLANNED"
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 2
    assert yard["metrics"]["active_runs"] == ["RUN-OB-WIN"]
    shift = api.expect_ok("GET", "/api/shifts/SHIFT-91")
    assert [event["kind"] for event in shift["events"]] == ["PULL_PLANNED"]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="switchyard-plan-failures-"))
    try:
        server_a = RunningServer(data_dir=root / "live")
        server_a.wait_ready()
        try:
            phase_a(server_a.api, root / "live")
        finally:
            server_a.stop()
        craft_workspace(root / "crafted")
        server_b = RunningServer(data_dir=root / "crafted")
        server_b.wait_ready()
        try:
            phase_b(server_b.api, root / "crafted")
            phase_c(server_b.api)
        finally:
            server_b.stop()
        print("OK wf_planning_failures")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
