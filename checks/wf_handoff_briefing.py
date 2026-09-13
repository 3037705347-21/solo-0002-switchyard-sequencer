"""Workflow check: shift handover briefing across three shift states.

The briefing is a read-only projection. The check drives real work through
the public API and verifies that totals match per-object details, that every
listed object carries a code and last-event timestamp, that unfinished work
is never reported as finished, and that generating a briefing does not write
business events.
"""

from __future__ import annotations

from support import ApiClient, run_check


def _assert_every_item_has_code_and_time(section: object, key: str = "items") -> None:
    assert isinstance(section, dict)
    items = section[key]
    assert isinstance(items, list)
    for item in items:
        assert item.get("code"), item
        assert item.get("last_event_at"), item


def _assert_briefing_header(briefing: dict[str, object], shift_code: str, state: str) -> None:
    assert briefing["read_only"] is True
    assert briefing["shift_code"] == shift_code
    assert briefing["shift"]["state"] == state
    assert briefing["briefing_version"]
    assert briefing["source_event_sequence"] >= 0
    assert briefing["generated_at"]
    assert briefing["briefing_code"] == f"BRIEF-{shift_code}-{briefing['source_event_sequence']}"


def _intake(code: str, route: str, arrival: str, cars: list[dict[str, object]]) -> dict[str, object]:
    return {"code": code, "route": route, "arrival_at": arrival, "cars": cars}


def _car(code: str, destination: str, kind: str = "BOX", length: int = 18) -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": "NONE",
    }


def _map_codes(items: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["code"]): item for item in items}


def _check_maintenance_track_blockers() -> None:
    """Maintenance tracks never appear in event messages, so verify their
    blocker timestamps through payload attribution and the car/shift fallback.
    """
    from switchyard.domain.car import FreightCar
    from switchyard.domain.enums import CarKind, CarState, EventKind, TrackPurpose, TrackState
    from switchyard.domain.pull import YardEvent
    from switchyard.domain.shift import YardShift
    from switchyard.domain.track import StandingTrack
    from switchyard.report.handoff import build_handoff_briefing
    from switchyard.storage.workspace import YardWorkspace

    opened_at = "2026-09-13T08:00:00Z"
    spot_at = "2026-09-13T09:30:00Z"

    def workspace_with(payload: dict[str, object] | None) -> tuple[YardWorkspace, YardShift]:
        workspace = YardWorkspace()
        workspace.tracks["MAINT-1"] = StandingTrack(
            "MAINT-1", TrackPurpose.GENERAL, 6, 180, state=TrackState.MAINTENANCE
        )
        workspace.cars["C-MT-01"] = FreightCar(
            code="C-MT-01",
            kind=CarKind.BOX,
            destination="N4",
            loaded=True,
            length_m=18,
            danger_class="NONE",
            state=CarState.STANDING,
            location="MAINT-1",
        )
        workspace.tracks["MAINT-1"].stack.append("C-MT-01")
        shift = YardShift(code="SHIFT-MT", dispatcher="KE", opened_at=opened_at)
        workspace.shifts["SHIFT-MT"] = shift
        if payload is not None:
            workspace.events.append(
                YardEvent(
                    sequence=1,
                    at=spot_at,
                    shift_code="SHIFT-MT",
                    kind=EventKind.TRAIN_CLASSIFIED,
                    message="intake INT-MT fully classified",
                    payload=payload,
                )
            )
        return workspace, shift

    cases = [
        # Spot record names the track directly.
        ({"intake_code": "INT-MT", "spots": [{"track_code": "MAINT-1", "car_code": "C-MT-01"}]}, spot_at),
        # Legacy payload names only the car: fall back to the car's last event.
        ({"intake_code": "INT-MT", "spots": [{"car_code": "C-MT-01"}]}, spot_at),
        # Nothing attributable at all: fall back to the shift opening time.
        (None, opened_at),
    ]
    for payload, expected_at in cases:
        workspace, shift = workspace_with(payload)
        briefing = build_handoff_briefing(workspace, shift)
        assert briefing["totals"]["blockers"] == 1, briefing["blockers"]
        blocker = briefing["blockers"][0]
        assert blocker["kind"] == "maintenance_track"
        assert blocker["code"] == "MAINT-1"
        assert blocker["last_event_at"] == expected_at, blocker


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-H1", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )

    # --- State 1: empty open shift -------------------------------------
    empty = api.expect_ok("GET", "/api/handoff-briefing")
    _assert_briefing_header(empty, "SHIFT-H1", "OPEN")
    assert empty["source_event_sequence"] == 1  # only SHIFT_OPENED
    totals = empty["totals"]
    assert totals["received_cars"] == 0
    assert totals["classified_cars"] == 0
    assert totals["pending_items"] == 0
    assert totals["ready_outbound_trains"] == 0
    assert totals["active_pull_runs"] == 0
    assert totals["departed_trains"] == 0
    assert totals["blockers"] == 0
    assert empty["received"]["items"] == []
    assert empty["classified"]["items"] == []
    assert empty["pending"] == []
    assert empty["assembled_outbound"]["items"] == []
    assert empty["active_pull_runs"]["items"] == []
    assert empty["departed"]["items"] == []
    assert empty["blockers"] == []
    shift_view = api.expect_ok("GET", "/api/shifts/SHIFT-H1")
    assert [event["kind"] for event in shift_view["events"]] == ["SHIFT_OPENED"]

    # Regenerating the same state must yield the same version/source pointer.
    empty_again = api.expect_ok("GET", "/api/handoff-briefing")
    assert empty_again["briefing_version"] == empty["briefing_version"]
    assert empty_again["source_event_sequence"] == empty["source_event_sequence"]
    shift_view = api.expect_ok("GET", "/api/shifts/SHIFT-H1")
    assert [event["kind"] for event in shift_view["events"]] == ["SHIFT_OPENED"]

    # --- State 2: partial work ------------------------------------------
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        _intake(
            "INT-H1",
            "RAIL-H1",
            "2026-09-13T08:30:00Z",
            # First car lands at the bottom of S2-A, the second on top, so the
            # planned pull needs a BUFFER/PULL/RETURN ticket.
            [_car("C-S2-H1", "S2"), _car("C-S2-H2", "S2")],
        ),
    )
    api.expect_ok("POST", "/api/intake-trains/INT-H1/classify", {})
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        _intake("INT-H2", "RAIL-H2", "2026-09-13T09:00:00Z", [_car("C-E7-H2", "E7")]),
    )
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-H1", "destination": "S2", "car_codes": ["C-S2-H1"]},
    )
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-H1/sequencer",
        {"transfer_code": "X1"},
    )
    run_code = sequenced["pull_run"]["code"]
    assert len(sequenced["pull_run"]["steps"]) == 3
    # Advance only the BUFFER step so the pull ticket is still running.
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})

    partial = api.expect_ok("GET", "/api/shifts/SHIFT-H1/handoff-briefing")
    _assert_briefing_header(partial, "SHIFT-H1", "OPEN")
    assert partial["briefing_version"] > empty["briefing_version"]
    assert partial["source_event_sequence"] > empty["source_event_sequence"]
    totals = partial["totals"]

    # Received: 2 + 1 cars across two trains; details must match the total.
    assert totals["received_cars"] == 3
    received = _map_codes(partial["received"]["items"])
    assert partial["received"]["car_count"] == 3
    assert set(received) == {"INT-H1", "INT-H2"}
    assert received["INT-H1"]["car_count"] == 2
    assert received["INT-H1"]["car_codes"] == ["C-S2-H1", "C-S2-H2"]
    assert received["INT-H2"]["car_count"] == 1
    _assert_every_item_has_code_and_time(partial["received"])

    # Classified: only the fully placed INT-H1 contributes cars.
    assert totals["classified_cars"] == 2
    classified = _map_codes(partial["classified"]["items"])
    assert partial["classified"]["car_count"] == 2
    assert set(classified) == {"INT-H1"}
    assert classified["INT-H1"]["classified_car_count"] == 2
    assert classified["INT-H1"]["classified_car_codes"] == ["C-S2-H1", "C-S2-H2"]
    assert classified["INT-H1"]["unplaced_car_codes"] == []
    _assert_every_item_has_code_and_time(partial["classified"])

    # Pending: INT-H2 still OPEN and one unclassified car.
    pending = _map_codes(partial["pending"])
    assert totals["pending_items"] == 1
    assert set(pending) == {"INT-H2"}
    assert pending["INT-H2"]["kind"] == "intake"
    assert pending["INT-H2"]["state"] == "OPEN"
    assert pending["INT-H2"]["unplaced_car_codes"] == ["C-E7-H2"]
    for item in partial["pending"]:
        assert item.get("code") and item.get("last_event_at")

    # The half-executed ticket is an active pull run and must not appear ready
    # or departed; its outbound stays PLANNED and is not listed as assembled.
    assert totals["active_pull_runs"] == 1
    runs = _map_codes(partial["active_pull_runs"]["items"])
    assert set(runs) == {run_code}
    assert runs[run_code]["state"] == "RUNNING"
    assert runs[run_code]["outbound_code"] == "OB-H1"
    assert runs[run_code]["current_step"] == 1
    assert runs[run_code]["total_steps"] == 3
    assert runs[run_code]["remaining_steps"] == 2
    _assert_every_item_has_code_and_time(partial["active_pull_runs"])
    assert totals["ready_outbound_trains"] == 0
    assert partial["assembled_outbound"]["items"] == []
    assert totals["departed_trains"] == 0
    assert partial["departed"]["items"] == []

    # Remaining blockers mirror the unfinished work, and every blocker must
    # carry a valid last-event time (including the unclassified car).
    blocker_codes = {item["code"] for item in partial["blockers"]}
    assert totals["blockers"] == len(partial["blockers"])
    assert "INT-H2" in blocker_codes
    assert "OB-H1" in blocker_codes
    assert run_code in blocker_codes
    assert "C-E7-H2" in blocker_codes
    for item in partial["blockers"]:
        assert item.get("code") and item.get("kind") and item.get("message")
        assert item.get("last_event_at"), item
    shift_events = api.expect_ok("GET", "/api/shifts/SHIFT-H1")["events"]
    receive_h2_at = next(
        event["at"]
        for event in shift_events
        if event["kind"] == "TRAIN_RECEIVED" and event["payload"].get("intake_code") == "INT-H2"
    )
    blocker_map = {item["code"]: item for item in partial["blockers"]}
    assert blocker_map["C-E7-H2"]["last_event_at"] == receive_h2_at
    assert blocker_map["INT-H2"]["last_event_at"] == receive_h2_at

    # Briefing generation is read-only: no extra event was journaled.
    events_before = len(api.expect_ok("GET", "/api/shifts/SHIFT-H1")["events"])
    api.expect_ok("GET", "/api/shifts/SHIFT-H1/handoff-briefing")
    api.expect_ok("GET", "/api/handoff-briefing")
    events_after = len(api.expect_ok("GET", "/api/shifts/SHIFT-H1")["events"])
    assert events_after == events_before

    # --- State 3: departed but the shift is still open ------------------
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 2})
    api.expect_ok("POST", "/api/outbound-trains/OB-H1/depart", {})
    api.expect_ok("POST", "/api/intake-trains/INT-H2/classify", {})

    done = api.expect_ok("GET", "/api/handoff-briefing")
    _assert_briefing_header(done, "SHIFT-H1", "OPEN")
    totals = done["totals"]
    assert totals["received_cars"] == 3
    assert totals["classified_cars"] == 3
    assert totals["pending_items"] == 0
    assert done["pending"] == []
    assert totals["active_pull_runs"] == 0
    assert done["active_pull_runs"]["items"] == []
    assert totals["ready_outbound_trains"] == 0
    assert done["assembled_outbound"]["items"] == []

    assert totals["departed_trains"] == 1
    assert totals["departed_cars"] == 1
    departed = _map_codes(done["departed"]["items"])
    assert done["departed"]["train_count"] == 1
    assert done["departed"]["car_count"] == 1
    assert departed["OB-H1"]["state"] == "DEPARTED"
    assert departed["OB-H1"]["departed_car_count"] == 1
    assert departed["OB-H1"]["departed_car_codes"] == ["C-S2-H1"]
    _assert_every_item_has_code_and_time(done["departed"])

    # The train departed but the shift was never closed: no blockers remain
    # and the shift is still reported OPEN.
    assert totals["blockers"] == 0
    assert done["blockers"] == []
    assert done["shift"]["closed_at"] is None

    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["departed"] == 1
    assert yard["metrics"]["car_state_counts"]["standing"] == 2
    assert yard["active_shift"] == "SHIFT-H1"

    # The explicit-shift endpoint still serves the briefing after closure.
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-H1/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    after_close = api.expect_ok("GET", "/api/shifts/SHIFT-H1/handoff-briefing")
    _assert_briefing_header(after_close, "SHIFT-H1", "CLOSED")
    assert after_close["totals"]["departed_trains"] == 1
    missing = api.expect_error("GET", "/api/handoff-briefing")
    assert missing["code"] == "RESOURCE_BUSY"
    api.expect_error("GET", "/api/shifts/NOPE-99/handoff-briefing")

    # Maintenance-track blocker attribution cannot be driven through the API
    # (nothing classifies onto MAINT-1), so verify it against persisted state.
    _check_maintenance_track_blockers()


if __name__ == "__main__":
    raise SystemExit(run_check("wf_handoff_briefing", run))
