"""Workflow check: intake classification with optional manual spot requests."""

from __future__ import annotations

from support import ApiClient, run_check


def car(code: str, kind: str, destination: str, length: int, danger: str = "NONE", *, loaded: bool = True) -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": loaded,
        "length_m": length,
        "danger_class": danger,
    }


def intake(code: str, route: str, cars: list[dict[str, object]]) -> dict[str, object]:
    return {"code": code, "route": route, "arrival_at": "2026-09-08T09:10:00Z", "cars": cars}


def index_outcomes(data: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["car_code"]: item for item in data["manual_spots"]}


def spot_map(data: dict[str, object]) -> dict[str, str]:
    return {item["car_code"]: item["track_code"] for item in data["spots"]}


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-M1", "dispatcher": "LIN", "opened_at": "2026-09-08T08:00:00Z"},
    )

    # 1. Legal designation: a general-purpose car pinned to MIX-1 and a D1 tank
    #    pinned to HAZ-1 both pass every track check and honor the request.
    cars = [
        car("C-N4-21", "BOX", "N4", 18),
        car("C-HAZ-21", "TANK", "W9", 22, "D1"),
    ]
    api.expect_ok("POST", "/api/intake-trains", intake("INT-M1", "RAIL-11", cars))
    classified = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-M1/classify",
        {
            "manual_spots": [
                {"car_code": "C-N4-21", "track_code": "MIX-1"},
                {"car_code": "C-HAZ-21", "track_code": "HAZ-1"},
            ]
        },
    )
    assert classified["intake"]["state"] == "CLASSIFIED"
    assert classified["unplaced"] == []
    tracks = spot_map(classified)
    assert tracks["C-N4-21"] == "MIX-1"
    assert tracks["C-HAZ-21"] == "HAZ-1"
    outcomes = index_outcomes(classified)
    assert outcomes["C-N4-21"]["applied"] is True
    assert outcomes["C-N4-21"]["automatic_track"] == "N4-A"
    assert outcomes["C-HAZ-21"]["applied"] is True

    # 2. Capacity exactly exhausted: ten N4 cars fill N4-A, so the eleventh
    #    manual request for N4-A is rejected with a concrete capacity reason and
    #    the car keeps its automatic selection on MIX-1.
    full = [car(f"C-N4-3{n}", "BOX", "N4", 18) for n in range(10)]
    full.append(car("C-N4-40", "BOX", "N4", 18))
    api.expect_ok("POST", "/api/intake-trains", intake("INT-M2", "RAIL-12", full))
    exhausted = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-M2/classify",
        {"manual_spots": [{"car_code": "C-N4-40", "track_code": "N4-A"}]},
    )
    assert exhausted["intake"]["state"] == "CLASSIFIED"
    assert exhausted["unplaced"] == []
    outcomes = index_outcomes(exhausted)
    denied = outcomes["C-N4-40"]
    assert denied["applied"] is False
    assert "at car capacity" in denied["reason"]
    assert denied["automatic_track"] == "MIX-1"
    assert spot_map(exhausted)["C-N4-40"] == "MIX-1"
    yard = api.expect_ok("GET", "/api/yard")
    track_usage = {item["code"]: item for item in yard["metrics"]["track_metrics"]}
    assert track_usage["N4-A"]["cars"] == 10

    # 3. Hazard rating mismatch: a D1 tank forced onto the non-rated MIX-1 is
    #    rejected with the hazard reason and falls back to HAZ-1 automatically.
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        intake("INT-M3", "RAIL-13", [car("C-HAZ-31", "TANK", "W9", 22, "D1")]),
    )
    mismatched = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-M3/classify",
        {"manual_spots": [{"car_code": "C-HAZ-31", "track_code": "MIX-1"}]},
    )
    outcomes = index_outcomes(mismatched)
    assert outcomes["C-HAZ-31"]["applied"] is False
    assert "not hazard rated" in outcomes["C-HAZ-31"]["reason"]
    assert outcomes["C-HAZ-31"]["automatic_track"] == "HAZ-1"
    assert spot_map(mismatched)["C-HAZ-31"] == "HAZ-1"

    # 4. Whole train completion: unspecified cars keep the original ranking and
    #    a mix of accepted and rejected requests still classifies every car;
    #    the event journal keeps the same classified semantics with outcomes.
    last = [
        car("C-E7-41", "HOPPER", "E7", 20),
        car("C-S2-41", "BOX", "S2", 18),
        car("C-W9-41", "FLAT", "W9", 16),
        car("C-HAZ-41", "TANK", "W9", 22, "D1"),
    ]
    api.expect_ok("POST", "/api/intake-trains", intake("INT-M4", "RAIL-14", last))

    # Malformed manual requests fail the boundary validation without mutating
    # the still-open intake or recording a classified event.
    bad_shape = api.expect_error(
        "POST",
        "/api/intake-trains/INT-M4/classify",
        {"manual_spots": [{"car_code": "C-E7-41"}]},
    )
    assert bad_shape["code"] == "VALIDATION_ERROR"
    bad_track = api.expect_error(
        "POST",
        "/api/intake-trains/INT-M4/classify",
        {"manual_spots": [{"car_code": "C-E7-41", "track_code": "NOPE-9"}]},
    )
    assert bad_track["code"] == "VALIDATION_ERROR"

    done = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-M4/classify",
        {
            "manual_spots": [
                {"car_code": "C-S2-41", "track_code": "MIX-1"},
                {"car_code": "C-HAZ-41", "track_code": "MIX-1"},
            ]
        },
    )
    assert done["intake"]["state"] == "CLASSIFIED"
    assert done["unplaced"] == []
    assert len(done["spots"]) == 4
    tracks = spot_map(done)
    assert tracks["C-E7-41"] == "E7-A"
    assert tracks["C-W9-41"] == "W9-A"
    assert tracks["C-S2-41"] == "MIX-1"
    assert tracks["C-HAZ-41"] == "HAZ-1"

    shift = api.expect_ok("GET", "/api/shifts/SHIFT-M1")
    events = shift["events"]
    classified_events = [event for event in events if event["kind"] == "TRAIN_CLASSIFIED"]
    assert len(classified_events) == 4
    payloads = {event["payload"]["spotted"]: event["payload"] for event in classified_events}
    assert payloads[4]["unplaced"] == []
    assert len(payloads[4]["manual_spots"]) == 2


if __name__ == "__main__":
    raise SystemExit(run_check("wf_intake_manual_spot", run))
