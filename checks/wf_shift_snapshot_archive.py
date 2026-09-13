"""Workflow check: shift snapshot archive list, detail, diffs, and gaps.

The check closes two real shifts, then verifies:

* archive listing (all, per-shift, time window) and snapshot detail;
* same-snapshot comparison (identical diff);
* cross-snapshot field-level differences covering car state counts, track
  occupancy, open/completed outbounds, unfinished runs, and blockers;
* explicit missing-field handling: a damaged legacy document must surface
  ``missing_in_base`` entries with null values instead of defaulting to zero;
* immutability: reads do not modify the live yard and archived snapshots can
  never be rewritten by later shifts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from support import RunningServer


def _intake(code: str, route: str, arrival: str, cars: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": code, "route": route, "arrival_at": arrival, "cars": cars}


def _car(code: str, kind: str, destination: str, length: int, *, loaded: bool = True, danger: str = "NONE") -> dict[str, Any]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": loaded,
        "length_m": length,
        "danger_class": danger,
    }


def _open_classify_close(api, shift: str, dispatcher: str, opened: str, intakes: list[dict[str, Any]]) -> dict[str, Any]:
    api.expect_ok("POST", "/api/shifts", {"code": shift, "dispatcher": dispatcher, "opened_at": opened})
    for train in intakes:
        api.expect_ok("POST", "/api/intake-trains", train)
        api.expect_ok("POST", f"/api/intake-trains/{train['code']}/classify", {})
    closed = api.expect_ok("POST", f"/api/shifts/{shift}/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    return closed


def _changed_field(diff_doc: dict[str, Any], section: str, field: str) -> dict[str, Any]:
    entry = diff_doc["sections"][section]["fields"][field]
    assert entry["status"] == "changed", (section, field, entry)
    return entry


def run(api) -> None:
    shift_a_intakes = [
        _intake(
            "INT-51",
            "RAIL-51",
            "2026-09-10T08:30:00Z",
            [
                _car("C-N4-51", "BOX", "N4", 18),
                _car("C-S2-51", "HOPPER", "S2", 20),
                _car("C-E7-51", "FLAT", "E7", 16),
            ],
        )
    ]
    closed_a = _open_classify_close(
        api,
        "SHIFT-51",
        "KE",
        "2026-09-10T08:00:00Z",
        shift_a_intakes,
    )
    snap_a_code = closed_a["snapshot"]["code"]
    assert snap_a_code == "SNAP-SHIFT-51"

    shift_b_intakes = [
        _intake(
            "INT-61",
            "RAIL-61",
            "2026-09-11T08:30:00Z",
            [
                _car("C-N4-61", "BOX", "N4", 18),
                _car("C-S2-61", "HOPPER", "S2", 20),
            ],
        )
    ]
    # Shift B additionally runs a real outbound cycle: two N4 cars planned,
    # pulled, assembled, and departed, so the second snapshot carries one
    # completed outbound and one unfinished... zero unfinished runs.
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-61", "dispatcher": "MA", "opened_at": "2026-09-11T08:00:00Z"},
    )
    api.expect_ok("POST", "/api/intake-trains", shift_b_intakes[0])
    api.expect_ok("POST", "/api/intake-trains/INT-61/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-61", "destination": "N4", "car_codes": ["C-N4-61"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-61/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    total_steps = len(sequenced["pull_run"]["steps"])
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": total_steps})
    assert advanced["completed"] is True
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-61/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    # Keep the two real closure instants in distinct wall-clock seconds so
    # that the archive time-window queries are meaningful.
    import time

    time.sleep(1.1)
    closed_b = api.expect_ok("POST", "/api/shifts/SHIFT-61/close", {})
    snap_b_code = closed_b["snapshot"]["code"]

    # Snapshot document carries version and source event range.
    snap_a_close = closed_a["snapshot"]
    event_range_a = snap_a_close["source_event_range"]
    assert event_range_a["first_sequence"] == 1
    assert event_range_a["last_sequence"] == event_range_a["first_sequence"] + event_range_a["event_count"] - 1
    assert snap_a_close["version"] >= 1
    assert snap_a_close["immutable"] is True
    assert snap_a_close["closed_at"]
    assert snap_a_close["metrics"]["car_state_counts"]["standing"] == 3

    # --- Listing ----------------------------------------------------------
    listing = api.expect_ok("GET", "/api/shift-snapshots")
    assert listing["count"] == 2
    codes = [item["code"] for item in listing["snapshots"]]
    assert codes == ["SNAP-SHIFT-51", "SNAP-SHIFT-61"]
    header_a = listing["snapshots"][0]
    assert header_a["shift_code"] == "SHIFT-51"
    assert header_a["car_state_counts"]["standing"] == 3
    assert header_a["open_outbound_count"] == 0
    assert header_a["completed_outbound_count"] == 0
    assert header_a["unfinished_run_count"] == 0
    assert header_a["track_count"] >= 7
    header_b = listing["snapshots"][1]
    assert header_b["completed_outbound_count"] == 1

    per_shift = api.expect_ok("GET", "/api/shift-snapshots?shift_code=shift-61")
    assert per_shift["count"] == 1
    assert per_shift["snapshots"][0]["code"] == snap_b_code
    assert per_shift["filters"]["shift_code"] == "SHIFT-61"

    # closed_at is recorded at closure time; build windows around it.
    closed_at_a = header_a["closed_at"]
    closed_at_b = header_b["closed_at"]
    assert isinstance(closed_at_a, str) and isinstance(closed_at_b, str)
    # Boundaries between the two distinct closure instants.
    from switchyard.domain.timeutil import parse_iso

    instant_a = parse_iso(closed_at_a)
    instant_b = parse_iso(closed_at_b)
    assert instant_b > instant_a, "the two real closures must have distinct closure times"
    midpoint = instant_a + (instant_b - instant_a) / 2
    before_b = midpoint.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    window = api.expect_ok("GET", f"/api/shift-snapshots?closed_from={before_b}")
    assert [item["code"] for item in window["snapshots"]] == [snap_b_code]
    early_window = api.expect_ok(
        "GET",
        f"/api/shift-snapshots?closed_from={closed_at_a}&closed_to={before_b}",
    )
    assert [item["code"] for item in early_window["snapshots"]] == [snap_a_code]
    empty_window = api.expect_ok("GET", "/api/shift-snapshots?shift_code=SHIFT-99")
    assert empty_window["count"] == 0

    far_window = api.expect_ok(
        "GET",
        "/api/shift-snapshots?closed_from=2030-01-01T00:00:00Z",
    )
    assert far_window["count"] == 0
    bad_window = api.expect_error(
        "GET",
        f"/api/shift-snapshots?closed_from={closed_at_b}&closed_to={closed_at_a}",
    )
    assert bad_window["code"] == "VALIDATION_ERROR"

    # --- Detail ------------------------------------------------------------
    detail_a = api.expect_ok("GET", f"/api/shift-snapshots/{snap_a_code}")
    snapshot_a = detail_a["snapshot"]
    assert snapshot_a == snap_a_close
    detail_b = api.expect_ok("GET", f"/api/shift-snapshots/{snap_b_code}")
    snapshot_b = detail_b["snapshot"]
    # Cars persist across shifts: A left 3 standing, B added 2 and departed 1,
    # so the second snapshot freezes 4 standing and 1 departed yard-wide.
    assert snapshot_b["metrics"]["car_state_counts"]["departed"] == 1
    assert snapshot_b["metrics"]["car_state_counts"]["standing"] == 4
    assert snapshot_b["metrics"]["completed_outbounds"] == ["OB-61"]
    assert snapshot_b["metrics"]["open_outbounds"] == []
    assert snapshot_b["metrics"]["unfinished_runs"] == []
    assert snapshot_b["metrics"]["outbound_state_counts"]["departed"] == 1
    assert snapshot_b["metrics"]["run_state_counts"]["completed"] == 1
    missing_detail = api.expect_error("GET", "/api/shift-snapshots/SNAP-NOPE")
    assert missing_detail["code"] == "NOT_FOUND"

    # --- Same-snapshot comparison ------------------------------------------
    self_diff = api.expect_ok(
        "POST",
        "/api/shift-snapshots/diff",
        {"base": snap_a_code, "target": snap_a_code},
    )
    assert self_diff["identical"] is True
    assert self_diff["summary"]["fields_changed"] == 0
    assert self_diff["summary"]["items_added"] == 0
    assert self_diff["summary"]["items_removed"] == 0
    assert self_diff["sections"]["car_state_counts"]["fields"]["standing"]["status"] == "equal"
    assert self_diff["sections"]["source_event_range"]["status"] == "compared"

    bad_diff = api.expect_error(
        "POST",
        "/api/shift-snapshots/diff",
        {"base": snap_a_code},
    )
    assert bad_diff["code"] == "VALIDATION_ERROR"
    missing_diff = api.expect_error(
        "POST",
        "/api/shift-snapshots/diff",
        {"base": snap_a_code, "target": "SNAP-MISSING"},
    )
    assert missing_diff["code"] == "NOT_FOUND"

    # --- Cross-snapshot differences ----------------------------------------
    cross = api.expect_ok(
        "POST",
        "/api/shift-snapshots/diff",
        {"base": snap_a_code, "target": snap_b_code},
    )
    assert cross["identical"] is False
    standing = _changed_field(cross, "car_state_counts", "standing")
    assert (standing["base"], standing["target"], standing["delta"]) == (3, 4, 1)
    departed = _changed_field(cross, "car_state_counts", "departed")
    assert (departed["base"], departed["target"], departed["delta"]) == (0, 1, 1)
    total = _changed_field(cross, "totals", "total_cars")
    assert (total["base"], total["target"], total["delta"]) == (3, 5, 2)

    n4 = cross["sections"]["track_occupancy"]["items"]["N4-A"]
    assert n4["status"] == "equal"
    n4_cars = n4["fields"]["cars"]
    assert (n4_cars["base"], n4_cars["target"], n4_cars["delta"]) == (1, 1, 0)
    assert n4_cars["status"] == "equal"
    n4_length = n4["fields"]["length_m"]
    assert (n4_length["base"], n4_length["target"], n4_length["delta"]) == (18, 18, 0)
    s2 = cross["sections"]["track_occupancy"]["items"]["S2-A"]
    s2_cars = s2["fields"]["cars"]
    assert (s2_cars["base"], s2_cars["target"], s2_cars["delta"]) == (1, 2, 1)
    assert s2_cars["status"] == "changed"

    completed_section = cross["sections"]["completed_outbounds"]
    assert completed_section["added"] == ["OB-61"]
    assert completed_section["removed"] == []
    open_section = cross["sections"]["open_outbounds"]
    assert open_section["status"] == "compared"
    assert open_section["added"] == [] and open_section["removed"] == []
    outbound_departed = _changed_field(cross, "outbound_state_counts", "departed")
    assert (outbound_departed["base"], outbound_departed["target"]) == (0, 1)
    run_completed = _changed_field(cross, "run_state_counts", "completed")
    assert (run_completed["base"], run_completed["target"]) == (0, 1)

    blockers = cross["sections"]["blockers"]
    assert blockers["items_added"] == [] and blockers["items_removed"] == []
    event_range_section = cross["sections"]["source_event_range"]
    assert event_range_section["fields"]["event_count"]["status"] == "changed"

    # --- Immutability: reads must not change the live yard -----------------
    yard_after_reads = api.expect_ok("GET", "/api/yard")
    assert yard_after_reads["metrics"]["car_state_counts"]["standing"] == 4
    assert yard_after_reads["metrics"]["car_state_counts"]["departed"] == 1
    # Re-reading archive detail repeatedly returns byte-identical content.
    detail_again = api.expect_ok("GET", f"/api/shift-snapshots/{snap_a_code}")
    assert detail_again["snapshot"] == snapshot_a
    # A closed shift can never be re-closed (which would rewrite its snapshot).
    reopen_error = api.expect_error("POST", "/api/shifts/SHIFT-51/close", {})
    assert reopen_error["code"] == "RESOURCE_BUSY"
    # The old snapshot still reports the shift-A metric after shift B's work.
    assert detail_again["snapshot"]["metrics"]["car_state_counts"]["standing"] == 3
    assert detail_again["snapshot"]["metrics"]["car_state_counts"]["departed"] == 0


def _inject_damaged_legacy_snapshot(data_dir: Path, template: dict[str, Any]) -> dict[str, Any]:
    """Build a hand-edited legacy snapshot missing newer metric sections."""
    state_path = data_dir / "yard-state.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    damaged = json.loads(json.dumps(template))
    damaged["code"] = "SNAP-SHIFT-LEGACY"
    damaged["shift_code"] = "SHIFT-LEGACY"
    # Simulate a document written before blockers/event range/new metrics.
    damaged.pop("blockers", None)
    damaged.pop("source_event_range", None)
    damaged.pop("schema_version", None)
    metrics = damaged.get("metrics", {})
    for key in (
        "track_occupancy",
        "open_outbounds",
        "completed_outbounds",
        "outbound_state_counts",
        "unfinished_runs",
        "run_state_counts",
    ):
        metrics.pop(key, None)
    if isinstance(metrics.get("car_state_counts"), dict):
        metrics["car_state_counts"].pop("removed", None)
    raw["closure_snapshots"].append(damaged)
    state_path.write_text(json.dumps(raw, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return damaged


def run_with_legacy(server: RunningServer, api) -> None:
    run(api)
    data_dir = server.data_dir
    detail_a = api.expect_ok("GET", "/api/shift-snapshots/SNAP-SHIFT-51")
    damaged = _inject_damaged_legacy_snapshot(data_dir, detail_a["snapshot"])

    listing = api.expect_ok("GET", "/api/shift-snapshots")
    codes = [item["code"] for item in listing["snapshots"]]
    assert "SNAP-SHIFT-LEGACY" in codes
    legacy_header = next(item for item in listing["snapshots"] if item["code"] == "SNAP-SHIFT-LEGACY")
    # Missing sections must surface as null summaries, never fabricated zeros.
    assert legacy_header["track_count"] is None
    assert legacy_header["open_outbound_count"] is None
    assert legacy_header["completed_outbound_count"] is None
    assert legacy_header["unfinished_run_count"] is None
    assert legacy_header["blocker_count"] is None
    assert legacy_header["schema_version"] is None
    assert legacy_header["car_state_counts"] == damaged["metrics"]["car_state_counts"]

    legacy_detail = api.expect_ok("GET", "/api/shift-snapshots/SNAP-SHIFT-LEGACY")
    assert "blockers" not in legacy_detail["snapshot"]
    assert "source_event_range" not in legacy_detail["snapshot"]

    gap = api.expect_ok(
        "POST",
        "/api/shift-snapshots/diff",
        {"base": "SNAP-SHIFT-LEGACY", "target": "SNAP-SHIFT-51"},
    )
    blockers_section = gap["sections"]["blockers"]
    assert blockers_section["status"] == "missing_in_base"
    assert blockers_section["items_added"] == []
    event_range_section = gap["sections"]["source_event_range"]
    assert event_range_section["status"] == "missing_in_base"
    for field in ("first_sequence", "last_sequence", "event_count"):
        entry = event_range_section["fields"][field]
        assert entry["status"] == "missing_in_base"
        assert entry["base"] is None and entry["delta"] is None
    track_section = gap["sections"]["track_occupancy"]
    assert track_section["status"] == "missing_in_base"
    assert set(track_section["items_added"]) >= {"N4-A", "S2-A", "E7-A", "MIX-1", "HAZ-1"}
    unfinished_section = gap["sections"]["unfinished_runs"]
    assert unfinished_section["status"] == "missing_in_base"
    completed_section = gap["sections"]["completed_outbounds"]
    assert completed_section["status"] == "missing_in_base"
    assert completed_section["codes"]["base"] is None
    assert "track_occupancy" in gap["summary"]["missing_sections"]
    assert "unfinished_runs" in gap["summary"]["missing_sections"]
    assert "completed_outbounds" in gap["summary"]["missing_sections"]
    assert "blockers" in gap["summary"]["missing_sections"]
    assert "source_event_range" in gap["summary"]["missing_sections"]
    assert gap["identical"] is False
    # A missing count is reported as missing_in_base with null, never zero:
    # even when target is zero the status must not read "equal".
    removed_entry = gap["sections"]["car_state_counts"]["fields"]["removed"]
    assert removed_entry["status"] == "missing_in_base"
    assert removed_entry["base"] is None
    assert removed_entry["target"] == 0

    # Reverse direction: the new snapshot becomes base, gaps flip sides.
    reverse = api.expect_ok(
        "POST",
        "/api/shift-snapshots/diff",
        {"base": "SNAP-SHIFT-51", "target": "SNAP-SHIFT-LEGACY"},
    )
    assert reverse["sections"]["blockers"]["status"] == "missing_in_target"
    assert reverse["sections"]["car_state_counts"]["fields"]["removed"]["status"] == "missing_in_target"
    assert reverse["sections"]["car_state_counts"]["fields"]["standing"]["status"] == "equal"

    # Reading the damaged archive must not repair (mutate) it on disk.
    state_after_reads = json.loads((data_dir / "yard-state.json").read_text(encoding="utf-8"))
    stored_legacy = next(doc for doc in state_after_reads["closure_snapshots"] if doc["code"] == "SNAP-SHIFT-LEGACY")
    assert "blockers" not in stored_legacy
    assert "source_event_range" not in stored_legacy
    # Live yard metrics remain untouched.
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["standing"] == 4


def main() -> int:
    server = RunningServer()
    try:
        server.wait_ready()
        run_with_legacy(server, server.api)
        print("OK wf_shift_snapshot_archive")
        return 0
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
