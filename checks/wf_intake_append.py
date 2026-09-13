"""Workflow check: append late-reported cars to an OPEN intake train."""

from __future__ import annotations

from support import ApiClient, run_check


def car(code: str, destination: str = "N4", *, kind: str = "BOX", danger: str = "NONE", length: int = 18) -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": danger,
    }


def intake_payload(code: str, cars: list[dict[str, object]]) -> dict[str, object]:
    return {
        "code": code,
        "route": "RAIL-11",
        "arrival_at": "2026-09-13T09:10:00Z",
        "cars": cars,
    }


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-01", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )

    # Happy path: create OPEN train with two cars, append two more, then classify.
    created = api.expect_ok(
        "POST",
        "/api/intake-trains",
        intake_payload("INT-01", [car("C-N4-21"), car("C-N4-22")]),
    )
    assert created["intake"]["state"] == "OPEN"
    assert created["intake"]["consist"] == ["C-N4-21", "C-N4-22"]
    assert len(created["cars"]) == 2

    appended = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-01/cars",
        {"cars": [car("C-E7-21", "E7", kind="FLAT", length=16), car("C-W9-21", "W9", kind="TANK", danger="D1", length=22)]},
    )
    assert appended["intake"]["state"] == "OPEN"
    assert appended["intake"]["consist"] == ["C-N4-21", "C-N4-22", "C-E7-21", "C-W9-21"]
    assert len(appended["intake"]["consist"]) == 4
    assert [item["code"] for item in appended["cars"]] == ["C-E7-21", "C-W9-21"]
    assert all(item["state"] == "RECEIVED" and item["location"] == "INTAKE" for item in appended["cars"])

    # The append is journalised as its own event.
    shift = api.expect_ok("GET", "/api/shifts/SHIFT-01")
    append_events = [event for event in shift["events"] if event["kind"] == "TRAIN_APPENDED"]
    assert len(append_events) == 1
    assert append_events[0]["payload"]["appended_count"] == 2
    assert append_events[0]["payload"]["car_count"] == 4

    classified = api.expect_ok("POST", "/api/intake-trains/INT-01/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"
    assert len(classified["spots"]) == 4
    spots = {item["car_code"]: item["track_code"] for item in classified["spots"]}
    assert spots["C-W9-21"] == "HAZ-1"

    # Failure path: append after CLASSIFIED is rejected with the current state.
    blocked = api.expect_error("POST", "/api/intake-trains/INT-01/cars", {"cars": [car("C-S2-21", "S2")]})
    assert blocked["code"] == "CONFLICT"
    assert "CLASSIFIED" in blocked["message"]
    assert blocked["details"]["state"] == "CLASSIFIED"

    # Failure path: duplicate code within the append batch.
    duplicate_batch = api.expect_error(
        "POST",
        "/api/intake-trains/INT-02/cars",
        {"cars": [car("C-N4-31"), car("C-N4-31")]},
    )
    assert duplicate_batch["code"] == "VALIDATION_ERROR"

    # Failure path: code already present in the yard (from INT-01) must not overwrite it.
    api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-02", [car("C-N4-41")]))
    duplicate_existing = api.expect_error(
        "POST",
        "/api/intake-trains/INT-02/cars",
        {"cars": [car("C-N4-21", destination="S2", length=30)]},
    )
    assert duplicate_existing["code"] == "CONFLICT"
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["total_cars"] == 5

    # Failure path: invalid car fields reuse the single-train validation.
    invalid = api.expect_error(
        "POST",
        "/api/intake-trains/INT-02/cars",
        {"cars": [car("C-N4-42", destination="ZZ")]},
    )
    assert invalid["code"] == "VALIDATION_ERROR"
    assert "destination" in invalid["message"]

    invalid_hazard = api.expect_error(
        "POST",
        "/api/intake-trains/INT-02/cars",
        {"cars": [car("C-N4-43", danger="D9")]},
    )
    assert invalid_hazard["code"] == "VALIDATION_ERROR"
    assert "hazard" in invalid_hazard["message"]

    # Failure path: PARTIAL intake also refuses structural consist changes.
    # HAZ-1 already holds C-W9-21 from INT-01, so ten D1 cars leave three unplaced.
    haz_cars = [car(f"C-HAZ-{index:02d}", "N4", kind="TANK", danger="D1", length=22) for index in range(1, 11)]
    partial = api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-03", haz_cars))
    assert partial["intake"]["state"] == "OPEN"
    result = api.expect_ok("POST", "/api/intake-trains/INT-03/classify", {})
    assert result["intake"]["state"] == "PARTIAL"
    assert len(result["unplaced"]) == 3
    blocked_partial = api.expect_error(
        "POST",
        "/api/intake-trains/INT-03/cars",
        {"cars": [car("C-HAZ-99", "N4", kind="TANK", danger="D1", length=22)]},
    )
    assert blocked_partial["code"] == "CONFLICT"
    assert "PARTIAL" in blocked_partial["message"]
    assert blocked_partial["details"]["state"] == "PARTIAL"

    # Failed appends never mutate the yard.
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["total_cars"] == 15
    assert "INT-03" in yard["metrics"]["active_intakes"]


if __name__ == "__main__":
    raise SystemExit(run_check("wf_intake_append", run))
