"""Workflow check: versioned intake manifest corrections."""

from __future__ import annotations

from support import ApiClient, run_check


def car(code: str, kind: str, destination: str, length_m: int, danger: str = "NONE") -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length_m,
        "danger_class": danger,
    }


def intake_payload(code: str, cars: list[dict[str, object]]) -> dict[str, object]:
    return {
        "code": code,
        "route": "RAIL-21",
        "arrival_at": "2026-09-08T09:40:00Z",
        "cars": cars,
    }


def correction_payload(
    cars: list[dict[str, object]],
    operator: str = "LIN",
    reason: str = "manifest fix",
) -> dict[str, object]:
    return {"operator": operator, "reason": reason, "cars": cars}


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-01", "dispatcher": "LIN", "opened_at": "2026-09-08T08:00:00Z"},
    )

    # --- unclassified correction: fix a destination typo and swap a car ---
    initial_cars = [
        car("C-N4-11", "BOX", "N4", 18),
        car("C-N4-12", "HOPPER", "N4", 20),
        car("C-E7-11", "FLAT", "E7", 16),
        car("C-HAZ-11", "TANK", "W9", 22, "D1"),
    ]
    created = api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-01", initial_cars))
    assert created["intake"]["manifest_version"] == 1

    corrected_cars = [
        car("C-N4-11", "BOX", "N4", 18),
        car("C-N4-12", "HOPPER", "E7", 20),
        car("C-E7-21", "FLAT", "E7", 16),
        car("C-HAZ-11", "TANK", "W9", 22, "D1"),
    ]
    corrected = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-01/correct",
        correction_payload(corrected_cars, reason="fix destination typo and swap flat car"),
    )
    assert corrected["intake"]["manifest_version"] == 2
    assert corrected["intake"]["consist"] == ["C-N4-11", "C-N4-12", "C-E7-21", "C-HAZ-11"]
    record = corrected["manifest_version"]
    assert record["version"] == 2
    assert record["operator"] == "LIN"
    assert record["reason"] == "fix destination typo and swap flat car"
    changes = {(item["kind"], item["car_code"]): item for item in record["changes"]}
    assert ("updated", "C-N4-12") in changes
    assert changes[("updated", "C-N4-12")]["before"]["destination"] == "N4"
    assert changes[("updated", "C-N4-12")]["after"]["destination"] == "E7"
    assert ("removed", "C-E7-11") in changes
    assert ("added", "C-E7-21") in changes

    # --- duplicate car codes inside the correction are rejected ---
    duplicated = corrected_cars + [car("C-N4-11", "BOX", "N4", 18)]
    bad = api.expect_error("POST", "/api/intake-trains/INT-01/correct", correction_payload(duplicated))
    assert bad["code"] == "VALIDATION_ERROR"

    # --- a correction without any change is rejected ---
    noop = api.expect_error("POST", "/api/intake-trains/INT-01/correct", correction_payload(corrected_cars))
    assert noop["code"] == "VALIDATION_ERROR"

    # --- classification reads the latest manifest version ---
    classified = api.expect_ok("POST", "/api/intake-trains/INT-01/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"
    tracks = {item["car_code"]: item["track_code"] for item in classified["spots"]}
    assert tracks["C-N4-12"] == "E7-A"
    assert tracks["C-E7-21"] == "E7-A"
    assert "C-E7-11" not in tracks

    # --- classified cars cannot be modified; the conflict names the car ---
    removed_standing = [item for item in corrected_cars if item["code"] != "C-N4-11"]
    conflict = api.expect_error(
        "POST",
        "/api/intake-trains/INT-01/correct",
        correction_payload(removed_standing),
    )
    assert conflict["code"] == "CONFLICT"
    conflict_cars = {item["car_code"]: item for item in conflict["details"]["conflicts"]}
    assert conflict_cars["C-N4-11"]["state"] == "STANDING"
    assert conflict_cars["C-N4-11"]["location"] == "N4-A"

    # --- cars referenced by an outbound plan cannot be modified either ---
    drafted = api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-01", "destination": "N4", "car_codes": ["C-N4-11"]},
    )
    assert drafted["state"] == "DRAFT"
    updated_planned = [
        car("C-N4-11", "BOX", "N4", 19),
        *[item for item in corrected_cars if item["code"] != "C-N4-11"],
    ]
    planned_conflict = api.expect_error(
        "POST",
        "/api/intake-trains/INT-01/correct",
        correction_payload(updated_planned),
    )
    assert planned_conflict["code"] == "CONFLICT"
    planned_cars = {item["car_code"]: item for item in planned_conflict["details"]["conflicts"]}
    assert planned_cars["C-N4-11"]["outbound_codes"] == ["OB-01"]

    # --- partial classification: placed cars are locked, unplaced can be swapped ---
    # HAZ-1 holds 8 cars and C-HAZ-11 already occupies one slot, so the 8th
    # tank car of INT-02 overflows and stays unplaced.
    partial_cars = [car("C-PART-1", "BOX", "N4", 18)]
    partial_cars += [car(f"C-PART-H{index}", "TANK", "W9", 20, "D1") for index in range(1, 9)]
    api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-02", partial_cars))
    partial = api.expect_ok("POST", "/api/intake-trains/INT-02/classify", {})
    assert partial["intake"]["state"] == "PARTIAL"
    assert partial["unplaced"] == ["C-PART-H8"]

    locked = [car("C-PART-1", "BOX", "N4", 21)] + partial_cars[1:]
    locked_conflict = api.expect_error(
        "POST",
        "/api/intake-trains/INT-02/correct",
        correction_payload(locked),
    )
    assert locked_conflict["code"] == "CONFLICT"
    locked_cars = {item["car_code"] for item in locked_conflict["details"]["conflicts"]}
    assert locked_cars == {"C-PART-1"}

    swapped = [item for item in partial_cars if item["code"] != "C-PART-H8"]
    swapped.append(car("C-PART-X8", "BOX", "W9", 18))
    replaced = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-02/correct",
        correction_payload(swapped, reason="swap overflow tank for box car"),
    )
    assert replaced["intake"]["manifest_version"] == 2
    assert replaced["intake"]["unplaced"] == ["C-PART-X8"]

    # --- a swapped-in car code must be unique across the yard ---
    clashing = [item for item in swapped if item["code"] != "C-PART-X8"]
    clashing.append(car("C-N4-11", "BOX", "W9", 18))
    clash = api.expect_error("POST", "/api/intake-trains/INT-02/correct", correction_payload(clashing))
    assert clash["code"] == "CONFLICT"

    finished = api.expect_ok("POST", "/api/intake-trains/INT-02/classify", {})
    assert finished["intake"]["state"] == "CLASSIFIED"

    # --- history stays traceable; current views read the latest version ---
    history = api.expect_ok("GET", "/api/intake-trains/INT-01/manifest-versions")
    assert history["current_version"] == 2
    assert [item["version"] for item in history["versions"]] == [1, 2]
    first, second = history["versions"]
    assert first["reason"] == "initial manifest"
    assert "C-E7-11" in first["car_codes"]
    first_specs = {item["code"]: item for item in first["cars"]}
    assert first_specs["C-N4-12"]["destination"] == "N4"
    assert second["operator"] == "LIN"
    assert "C-E7-21" in second["car_codes"]

    current = api.expect_ok("GET", "/api/intake-trains/INT-01/manifest")
    assert current["manifest_version"]["version"] == 2
    current_codes = [item["code"] for item in current["cars"]]
    assert current_codes == ["C-N4-11", "C-N4-12", "C-E7-21", "C-HAZ-11"]

    partial_history = api.expect_ok("GET", "/api/intake-trains/INT-02/manifest-versions")
    assert partial_history["current_version"] == 2
    swap_changes = {
        (item["kind"], item["car_code"]) for item in partial_history["versions"][1]["changes"]
    }
    assert ("removed", "C-PART-H8") in swap_changes
    assert ("added", "C-PART-X8") in swap_changes

    # --- removed cars leave no trace in the yard balance ---
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["total_cars"] == 13
    assert yard["metrics"]["car_state_counts"]["received"] == 0
    blocker_text = " ".join(item["message"] for item in yard["blockers"])
    assert "C-E7-11" not in blocker_text
    assert "C-PART-H8" not in blocker_text


if __name__ == "__main__":
    raise SystemExit(run_check("wf_manifest_correction", run))
