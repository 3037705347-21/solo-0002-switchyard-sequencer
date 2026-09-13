"""Workflow check: revise a DRAFT outbound train's planned car sequence.

Covers replacement, append, deletion, and the PLANNED-state rejection, plus
failure atomicity (a rejected revision keeps the prior plan and never leaves a
reservation behind). The original create -> sequence -> advance -> depart path
is exercised at the end to confirm it still works.
"""

from __future__ import annotations

from support import ApiClient, run_check

SHIFT = {
    "code": "SHIFT-04",
    "dispatcher": "EARLY",
    "opened_at": "2026-09-13T06:00:00Z",
}

# Five N4 cars are classified in intake order, so N4-A stacks bottom-to-top as
# C-N4-41, C-BLK-41, C-N4-42, C-N4-43, C-N4-44 (C-N4-44 is the stack top).
CARS = [
    {"code": "C-N4-41", "kind": "BOX", "destination": "N4", "loaded": True, "length_m": 18, "danger_class": "NONE"},
    {"code": "C-BLK-41", "kind": "BOX", "destination": "N4", "loaded": False, "length_m": 18, "danger_class": "NONE"},
    {"code": "C-N4-42", "kind": "FLAT", "destination": "N4", "loaded": False, "length_m": 16, "danger_class": "NONE"},
    {"code": "C-N4-43", "kind": "BOX", "destination": "N4", "loaded": True, "length_m": 18, "danger_class": "NONE"},
    {"code": "C-N4-44", "kind": "REEFER", "destination": "N4", "loaded": False, "length_m": 20, "danger_class": "NONE"},
]


def _classify_five_cars(api: ApiClient) -> None:
    api.expect_ok("POST", "/api/shifts", SHIFT)
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-41",
            "route": "RAIL-41",
            "arrival_at": "2026-09-13T06:30:00Z",
            "cars": CARS,
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-41/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"
    yard = api.expect_ok("GET", "/api/yard")
    n4_track = next(item for item in yard["metrics"]["track_metrics"] if item["code"] == "N4-A")
    assert n4_track["cars"] == 5
    assert n4_track["top_car"] == "C-N4-44"


def _assert_planned_sequence(data: dict, expected: list[str]) -> None:
    assert [item["car_code"] for item in data["planned_sequence"]] == expected
    positions = [item["position"] for item in data["planned_sequence"]]
    assert positions == list(range(1, len(expected) + 1))
    assert all(item["track_code"] == "N4-A" and item["destination"] == "N4" for item in data["planned_sequence"])
    assert data["outbound"]["planned_car_codes"] == expected


def run(api: ApiClient) -> None:
    _classify_five_cars(api)
    initial = ["C-N4-44", "C-N4-43"]
    created = api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-41", "destination": "N4", "car_codes": initial},
    )
    assert created["state"] == "DRAFT"
    assert created["planned_car_codes"] == initial
    # A draft plan reserves no cars: reservations happen only at sequencing.
    assert api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]["reserved"] == 0

    # --- Scenario 1: replace a planned car while still DRAFT -----------------
    replaced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-41/plan",
        {"car_codes": ["C-N4-44", "C-N4-42"]},
    )
    assert replaced["outbound"]["state"] == "DRAFT"
    assert replaced["changes"]["added"] == ["C-N4-42"]
    assert replaced["changes"]["removed"] == ["C-N4-43"]
    assert replaced["changes"]["reordered"] is False
    _assert_planned_sequence(replaced, ["C-N4-44", "C-N4-42"])

    # --- Scenario 2: append an additional car -------------------------------
    appended = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-41/plan",
        {"car_codes": ["C-N4-44", "C-N4-43", "C-N4-42"]},
    )
    assert appended["changes"]["added"] == ["C-N4-43"]
    assert appended["changes"]["removed"] == []
    assert appended["changes"]["reordered"] is False
    _assert_planned_sequence(appended, ["C-N4-44", "C-N4-43", "C-N4-42"])

    # A LIFO-infeasible reorder (C-N4-43 is below C-N4-44 but listed first) is
    # rejected and must not alter the draft or reserve any car.
    error = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-41/plan",
        {"car_codes": ["C-N4-43", "C-N4-44", "C-N4-42"]},
    )
    assert "must be pulled before" in error["message"]
    assert api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]["reserved"] == 0

    # Another draft train claims the cars below OB-41's selection.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-42", "destination": "N4", "car_codes": ["C-BLK-41", "C-N4-41"]},
    )
    # Appending a car occupied by that other active draft train is rejected,
    # again without touching OB-41's plan or reserving anything.
    busy = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-41/plan",
        {"car_codes": ["C-N4-44", "C-N4-43", "C-N4-42", "C-BLK-41"]},
    )
    assert busy["code"] == "RESOURCE_BUSY"
    assert "C-BLK-41" in busy["message"]
    assert api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]["reserved"] == 0

    # --- Scenario 3: delete a planned car while still DRAFT ------------------
    removed = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-41/plan",
        {"car_codes": ["C-N4-44", "C-N4-43"]},
    )
    assert removed["changes"]["added"] == []
    assert removed["changes"]["removed"] == ["C-N4-42"]
    assert removed["changes"]["reordered"] is False
    _assert_planned_sequence(removed, ["C-N4-44", "C-N4-43"])

    # Only the three accepted revisions are journaled; failures wrote nothing.
    current = api.expect_ok("GET", "/api/shifts/SHIFT-04")
    revised_events = [event for event in current["events"] if event["kind"] == "PLAN_REVISED"]
    assert len(revised_events) == 3
    assert revised_events[-1]["payload"]["planned_car_codes"] == ["C-N4-44", "C-N4-43"]

    # --- Scenario 4: once PLANNED the consist can no longer be edited --------
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-41/sequencer",
        {"transfer_code": "X1"},
    )
    assert sequenced["outbound"]["state"] == "PLANNED"
    locked = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-41/plan",
        {"car_codes": ["C-N4-44"]},
    )
    assert locked["code"] == "STATE_TRANSITION"

    # Existing sequence / advance / departure path remains usable end to end.
    run_code = sequenced["pull_run"]["code"]
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})
    assert advanced["completed"] is True
    assert advanced["outbound"]["state"] == "READY"
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-41/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    # Editing a departed train is rejected just like a PLANNED one.
    after_departure = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-41/plan",
        {"car_codes": ["C-N4-43"]},
    )
    assert after_departure["code"] == "STATE_TRANSITION"
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["departed"] == 2
    # OB-42 is still only a draft: its cars were never reserved.
    assert yard["metrics"]["car_state_counts"]["reserved"] == 0
    assert yard["metrics"]["car_state_counts"]["standing"] == 3


if __name__ == "__main__":
    raise SystemExit(run_check("wf_outbound_plan_revision", run))
