"""Workflow check: reservation ledger across planning, conflict, and replan flows.

Covers four flows:
1. planning success freezes traceable reservation records;
2. planning failure leaves no reservation residue;
3. duplicate planning attempts report both conflicting parties;
4. replan and cancel release reservations while history stays traceable.
"""

from __future__ import annotations

from support import ApiClient, run_check


def classify_four_cars(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-RSV", "dispatcher": "RUI", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-31",
            "route": "RAIL-31",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": [
                {
                    "code": "C-RSV-01",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-RSV-02",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-RSV-03",
                    "kind": "FLAT",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 16,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-RSV-04",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-31/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"


def flow_planning_success(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-31", "destination": "N4", "car_codes": ["C-RSV-04", "C-RSV-02"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-31/sequencer", {"transfer_code": "X1"})
    reservations = sequenced["reservations"]
    assert len(reservations) == 2
    assert all(item["status"] == "ACTIVE" for item in reservations)
    assert all(item["outbound_code"] == "OB-31" for item in reservations)

    ledger = api.expect_ok("GET", "/api/reservations?status=ACTIVE")
    assert ledger["total"] == 2
    by_code = {item["car_code"]: item for item in ledger["reservations"]}
    top_car = by_code["C-RSV-04"]
    dug_car = by_code["C-RSV-02"]
    for entry in (top_car, dug_car):
        assert entry["outbound_code"] == "OB-31"
        assert entry["destination"] == "N4"
        assert entry["track_code"] == "N4-A"
        assert entry["frozen_at"]
        assert entry["held_for_seconds"] >= 0
        assert "RUN-OB-31" in entry["release_condition"]
        assert entry["car_state"] == "RESERVED"
        assert entry["outbound_state"] == "PLANNED"
    assert [step["verb"] for step in top_car["actions"]] == ["PULL"]
    assert [step["verb"] for step in dug_car["actions"]] == ["BUFFER", "PULL", "RETURN"]
    assert dug_car["actions"][0]["car_code"] == "C-RSV-03"

    assert ledger["by_outbound"]["OB-31"] == [item["code"] for item in ledger["reservations"]]
    assert set(ledger["by_car"]) == {"C-RSV-04", "C-RSV-02"}

    assert api.expect_ok("GET", "/api/reservations?track=N4-A")["total"] == 2
    assert api.expect_ok("GET", "/api/reservations?track=E7-A")["total"] == 0
    assert api.expect_ok("GET", "/api/reservations?destination=N4")["total"] == 2
    assert api.expect_ok("GET", "/api/reservations?destination=E7")["total"] == 0
    assert api.expect_ok("GET", "/api/reservations?status=RELEASED")["total"] == 0
    assert api.expect_ok("GET", "/api/reservations?car=C-RSV-02")["total"] == 1
    api.expect_error("GET", "/api/reservations?status=BOGUS")
    api.expect_error("GET", "/api/reservations?shift=SHIFT-RSV")

    single = api.expect_ok("GET", f"/api/reservations/{dug_car['code']}")
    assert single["reservation"]["code"] == dug_car["code"]
    assert single["reservation"]["car_code"] == "C-RSV-02"


def flow_planning_failure(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-FAIL", "destination": "N4", "car_codes": ["C-RSV-01", "C-RSV-03"]},
    )
    error = api.expect_error("POST", "/api/outbound-trains/OB-FAIL/sequencer", {"transfer_code": "X1"})
    assert error["code"] in {"VALIDATION_ERROR", "RESOURCE_BUSY"}

    ledger = api.expect_ok("GET", "/api/reservations")
    assert ledger["total"] == 2
    assert all(item["outbound_code"] != "OB-FAIL" for item in ledger["reservations"])
    assert api.expect_ok("GET", "/api/reservations?outbound=OB-FAIL")["total"] == 0

    api.expect_error("POST", "/api/outbound-trains/OB-FAIL/replan", {})
    cancelled = api.expect_ok("POST", "/api/outbound-trains/OB-FAIL/cancel", {})
    assert cancelled["outbound"]["state"] == "ABANDONED"
    assert cancelled["released_reservations"] == []
    reserved = api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]["reserved"]
    assert reserved == 2


def flow_duplicate_attempt(api: ApiClient) -> None:
    error = api.expect_error(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-32", "destination": "N4", "car_codes": ["C-RSV-04"]},
    )
    assert error["code"] == "RESOURCE_BUSY"
    conflicts = error["details"]["conflicts"]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["car_code"] == "C-RSV-04"
    assert conflict["held_by"]["outbound_code"] == "OB-31"
    assert conflict["held_by"]["reservation_code"].startswith("RSV-")
    assert conflict["held_by"]["run_code"] == "RUN-OB-31"
    assert conflict["held_by"]["track_code"] == "N4-A"
    assert conflict["held_by"]["frozen_at"]
    assert conflict["requested_by"]["outbound_code"] == "OB-32"
    assert conflict["requested_by"]["destination"] == "N4"

    mixed = api.expect_error(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-33", "destination": "N4", "car_codes": ["C-RSV-03", "C-RSV-04"]},
    )
    mixed_conflicts = mixed["details"]["conflicts"]
    assert [item["car_code"] for item in mixed_conflicts] == ["C-RSV-04"]

    api.expect_error("POST", "/api/outbound-trains/OB-31/sequencer", {"transfer_code": "X1"})
    assert api.expect_ok("GET", "/api/reservations?status=ACTIVE")["total"] == 2


def flow_replan_release(api: ApiClient) -> None:
    released_codes = [
        item["code"] for item in api.expect_ok("GET", "/api/reservations?status=ACTIVE")["reservations"]
    ]
    replanned = api.expect_ok("POST", "/api/outbound-trains/OB-31/replan", {})
    assert replanned["outbound"]["state"] == "DRAFT"
    released = replanned["released_reservations"]
    assert len(released) == 2
    assert all(item["status"] == "RELEASED" for item in released)
    assert all(item["release_reason"] == "replanned" for item in released)
    assert all(item["released_at"] for item in released)

    assert api.expect_ok("GET", "/api/reservations?status=ACTIVE")["total"] == 0
    history = api.expect_ok("GET", "/api/reservations?status=RELEASED")
    assert history["total"] == 2
    assert {item["code"] for item in history["reservations"]} == set(released_codes)
    reserved = api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]["reserved"]
    assert reserved == 0

    resequenced = api.expect_ok("POST", "/api/outbound-trains/OB-31/sequencer", {"transfer_code": "X1"})
    new_codes = [item["code"] for item in resequenced["reservations"]]
    assert len(new_codes) == 2
    assert not set(new_codes) & set(released_codes)
    ledger = api.expect_ok("GET", "/api/reservations")
    assert ledger["total"] == 4
    assert api.expect_ok("GET", "/api/reservations?status=ACTIVE")["total"] == 2
    old_record = api.expect_ok("GET", f"/api/reservations/{released_codes[0]}")["reservation"]
    assert old_record["status"] == "RELEASED"
    assert old_record["release_reason"] == "replanned"

    advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-31/advance", {"steps": 10})
    assert advanced["completed"] is True
    fulfilled = api.expect_ok("GET", "/api/reservations?status=FULFILLED")
    assert fulfilled["total"] == 2
    assert all(item["release_reason"] == "assembled" for item in fulfilled["reservations"])
    assert api.expect_ok("GET", "/api/reservations?status=ACTIVE")["total"] == 0
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-31/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"

    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-34", "destination": "N4", "car_codes": ["C-RSV-01"]},
    )
    api.expect_ok("POST", "/api/outbound-trains/OB-34/sequencer", {"transfer_code": "X1"})
    assert api.expect_ok("GET", "/api/reservations?status=ACTIVE")["total"] == 1
    cancelled = api.expect_ok("POST", "/api/outbound-trains/OB-34/cancel", {})
    assert cancelled["outbound"]["state"] == "ABANDONED"
    assert len(cancelled["released_reservations"]) == 1
    assert cancelled["released_reservations"][0]["release_reason"] == "cancelled"

    ledger = api.expect_ok("GET", "/api/reservations")
    assert ledger["total"] == 5
    assert api.expect_ok("GET", "/api/reservations?status=ACTIVE")["total"] == 0
    assert api.expect_ok("GET", "/api/reservations?status=RELEASED")["total"] == 3
    assert api.expect_ok("GET", "/api/reservations?status=FULFILLED")["total"] == 2
    car_history = api.expect_ok("GET", "/api/reservations?car=C-RSV-01")
    assert car_history["total"] == 1
    assert car_history["reservations"][0]["status"] == "RELEASED"
    counts = api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]
    assert counts["reserved"] == 0
    assert counts["departed"] == 2


def run(api: ApiClient) -> None:
    classify_four_cars(api)
    flow_planning_success(api)
    flow_planning_failure(api)
    flow_duplicate_attempt(api)
    flow_replan_release(api)


if __name__ == "__main__":
    raise SystemExit(run_check("wf_reservation_ledger", run))
