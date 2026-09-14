"""Workflow check: car location/ownership view across the five yard phases.

Drives the real HTTP API and reconciles ``GET /api/cars/{code}`` against
``GET /api/yard`` for cars that are in yard, buffered, reserved, assembled,
and departed.  It also verifies that:

* the query is read-only (the persisted workspace version never moves),
* a departed car is never reported as still in the yard,
* an unknown car code is a 404.
"""

from __future__ import annotations

from support import ApiClient, run_check


def _car(code: str, destination: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": True,
        "length_m": 18,
        "danger_class": "NONE",
    }
    payload.update(overrides)
    return payload


def _yard_version(api: ApiClient) -> int:
    yard = api.expect_ok("GET", "/api/yard")
    return int(yard["metrics"]["version"])


def _alert_codes(view: dict[str, object]) -> set[str]:
    return {str(item["code"]) for item in view.get("consistency", [])}  # type: ignore[arg-type]


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-05", "dispatcher": "QING", "opened_at": "2026-09-14T08:00:00Z"},
    )
    # N4-A ends bottom-to-top: C-N4-51 (target), C-N4-50, C-BLK-50 (top blocker).
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-50",
            "route": "RAIL-50",
            "arrival_at": "2026-09-14T09:00:00Z",
            "cars": [
                _car("C-N4-51", "N4", kind="FLAT", length_m=16, loaded=False),
                _car("C-N4-50", "N4"),
                _car("C-BLK-50", "N4", loaded=False),
                _car("C-S2-50", "S2", kind="HOPPER", length_m=20),
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-50/classify", {})

    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-50", "destination": "N4", "car_codes": ["C-N4-51"]},
    )
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-50/sequencer",
        {"transfer_code": "X1"},
    )
    run_code = sequenced["pull_run"]["code"]
    verbs = [(s["verb"], s["car_code"]) for s in sequenced["pull_run"]["steps"]]
    assert verbs[0] == ("BUFFER", "C-BLK-50"), verbs

    def view(code: str) -> dict[str, object]:
        return api.expect_ok("GET", f"/api/cars/{code}")

    def assert_clean(code: str, phase: str) -> dict[str, object]:
        doc = view(code)
        assert doc["phase"] == phase, (code, doc["phase"], phase)
        assert doc["claimed_phase"] in {phase, "IN_YARD"} or phase == "BUFFERED"
        assert doc["alert_count"] == 0, (code, doc["consistency"])
        assert doc["ownership"]["shift"]["code"] == "SHIFT-05"
        assert doc["ownership"]["intake_train"] == "INT-50"
        assert doc["last_action"]["kind"] is not None
        return doc

    # Half-execute: blocker buffered, target still reserved on the track.
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})

    in_yard = assert_clean("C-S2-50", "IN_YARD")
    assert in_yard["in_yard"] is True
    assert in_yard["location"]["standing_track"]["code"] == "S2-A"
    assert in_yard["location"]["claimed"] == "S2-A"
    assert in_yard["ownership"]["ticket_outbound"] is None

    in_yard_second = assert_clean("C-N4-50", "IN_YARD")
    assert in_yard_second["location"]["standing_track"]["code"] == "N4-A"

    buffered = assert_clean("C-BLK-50", "BUFFERED")
    assert buffered["in_yard"] is True
    assert buffered["phase_conflict"] is False  # STANDING claim + bay is normal
    assert buffered["location"]["buffer_bay"]["code"] == "X1"
    assert buffered["location"]["standing_track"] is None
    blocker_moves = buffered["plans"]["pull_runs"][0]
    assert blocker_moves["next_car_action"] in {"BUFFER", "RETURN"}
    assert any(m["verb"] == "BUFFER" and m["status"] == "EXECUTED" for m in buffered["recent_changes"]["move_history"])

    reserved = assert_clean("C-N4-51", "RESERVED")
    assert reserved["in_yard"] is True
    assert reserved["location"]["standing_track"]["code"] == "N4-A"
    assert reserved["ownership"]["ticket_outbound"] == "OB-50"
    assert reserved["plans"]["active_pull_run"]["state"] in {"QUEUED", "RUNNING"}
    assert reserved["plans"]["outbound_tickets"][0]["planned_position"] == 1
    assert "RESERVED" in [t["phase"] for t in reserved["recent_changes"]["state_timeline"]]

    # Yard overview must agree with the four phases while the run is mid-flight.
    yard = api.expect_ok("GET", "/api/yard")
    counts = yard["metrics"]["car_state_counts"]
    physical = reserved["yard_overview"]["physical_phase_counts"]
    assert physical["standing"] + physical["buffered"] == counts["standing"], (physical, counts)
    assert physical["reserved"] == counts["reserved"]
    assert counts["standing"] == 3 and counts["reserved"] == 1
    assert physical["standing"] == 2 and physical["buffered"] == 1
    bay_rows = yard["metrics"]["transfer_bays"]
    assert sum(row["cars"] for row in bay_rows) == 1

    # Complete the run: target assembled, blockers returned.
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})

    assembled = assert_clean("C-N4-51", "ASSEMBLED")
    assert assembled["in_yard"] is True
    assert assembled["location"]["outbound_train"]["code"] == "OB-50"
    assert assembled["location"]["standing_track"] is None
    assert assembled["ownership"]["ticket_outbound"] == "OB-50"
    assert assembled["plans"]["pull_runs"][0]["state"] == "COMPLETED"
    timeline = [t["phase"] for t in assembled["recent_changes"]["state_timeline"]]
    assert timeline == ["RECEIVED", "STANDING", "RESERVED", "ASSEMBLED"], timeline

    returned = assert_clean("C-BLK-50", "IN_YARD")
    assert returned["location"]["standing_track"]["code"] == "N4-A"
    assert returned["location"]["buffer_bay"] is None

    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["assembled"] == 1
    assert yard["metrics"]["car_state_counts"]["standing"] == 3

    # Depart: the car must leave the yard in every part of the answer.
    departed_result = api.expect_ok("POST", "/api/outbound-trains/OB-50/depart", {})
    assert departed_result["outbound"]["state"] == "DEPARTED"

    departed = view("C-N4-51")
    assert departed["phase"] == "DEPARTED"
    assert departed["in_yard"] is False
    assert departed["location"]["observed_kind"] == "outbound_train"
    assert departed["location"]["standing_track"] is None
    assert departed["location"]["buffer_bay"] is None
    assert departed["ownership"]["ticket_outbound"] == "OB-50"
    assert departed["last_action"]["kind"] == "TRAIN_DEPARTED"
    timeline = [t["phase"] for t in departed["recent_changes"]["state_timeline"]]
    assert timeline == ["RECEIVED", "STANDING", "RESERVED", "ASSEMBLED", "DEPARTED"], timeline
    assert departed["alert_count"] == 0

    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["departed"] == 1
    assert yard["metrics"]["car_state_counts"]["assembled"] == 0
    assert yard["metrics"]["car_state_counts"]["standing"] == 3
    assert departed["yard_overview"]["state_counts"] == yard["metrics"]["car_state_counts"]
    assert departed["yard_overview"]["physical_phase_counts"]["departed"] == 1
    assert departed["yard_overview"]["car_in_yard"] is False

    # Unknown car -> 404.
    missing = api.expect_error("GET", "/api/cars/C-404-404")
    assert missing["code"] == "NOT_FOUND", missing

    # Read-only guarantee: queries never advance the workspace version.
    version = _yard_version(api)
    for code in ["C-N4-51", "C-BLK-50", "C-N4-50", "C-S2-50"]:
        api.expect_ok("GET", f"/api/cars/{code}")
    assert _yard_version(api) == version


if __name__ == "__main__":
    raise SystemExit(run_check("wf_car_view", run))
