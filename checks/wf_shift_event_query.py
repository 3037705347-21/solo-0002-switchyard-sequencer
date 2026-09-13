"""Workflow check: filtered, append-stable paging of the shift event trail.

Drives a full shift (continuous intake, classification, buffered pull
advances, departure, a blocked closure and the final closure), then verifies:

* filtering by event kind, time range, and object number (shift/intake/
  outbound/run/car codes),
* keyset pagination whose cursor stays stable while new events keep being
  appended between pages: every matching event appears exactly once, none is
  skipped, and events appended during the walk never leak into its pages,
* totals, ordering (oldest first), and the relation between each event and the
  current state of its subject, so a rejected (blocked closure) or superseded
  (an earlier run advance) action is not mistaken for the final state.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.error
import urllib.parse

from support import ApiClient, run_check

SHIFT = "SHIFT-50"


def robust_request(api: ApiClient, method: str, path: str, payload: object | None = None) -> tuple[int, dict[str, object]]:
    """Request with a small retry budget for transient local-connection resets."""

    last_error: Exception | None = None
    for attempt in range(6):
        try:
            return api.request(method, path, payload)
        except (ConnectionResetError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.02 * (attempt + 1))
    raise AssertionError(f"request kept failing: {last_error}")


def robust_ok(api: ApiClient, method: str, path: str, payload: object | None = None) -> dict[str, object]:
    status, body = robust_request(api, method, path, payload)
    if status not in {200, 201} or not body.get("ok"):
        raise AssertionError(f"{method} {path} failed: {status} {body}")
    return dict(body.get("data") or {})


def robust_error(api: ApiClient, method: str, path: str, payload: object | None = None) -> dict[str, object]:
    status, body = robust_request(api, method, path, payload)
    if status < 400 or body.get("ok"):
        raise AssertionError(f"{method} {path} should have failed: {status} {body}")
    return dict(body.get("error") or {})


def q(path: str, **params: object) -> str:
    query = urllib.parse.urlencode(
        [(key, value) for key, value in params.items() if value is not None],
        doseq=True,
    )
    return f"{path}?{query}" if query else path


def event_page(api: ApiClient, **params: object) -> dict[str, object]:
    return robust_ok(api, "GET", q(f"/api/shifts/{SHIFT}", **params))


def sequences(page: dict[str, object]) -> list[int]:
    return [event["sequence"] for event in page["events"]]


def car(code: str) -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": "N4",
        "loaded": True,
        "length_m": 18,
        "danger_class": "NONE",
    }


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": SHIFT, "dispatcher": "KE", "opened_at": "2026-09-13T08:00:00Z"},
    )

    # Continuous intake: two inbound trains, all cars land on the N4-A stack
    # in arrival order (bottom -> top: A1,A2,A3 then B1,B2,B3).
    for intake in ("INT-A", "INT-B"):
        suffix = intake[-1]
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {
                "code": intake,
                "route": f"RAIL-{suffix}",
                "arrival_at": f"2026-09-13T09:{10 if suffix == 'A' else 20}:00Z",
                "cars": [car(f"C-N4-{suffix}1"), car(f"C-N4-{suffix}2"), car(f"C-N4-{suffix}3")],
            },
        )
        api.expect_ok("POST", f"/api/intake-trains/{intake}/classify", {})

    # Each outbound pulls the two deeper cars (middle then bottom) out of the
    # shared stack, so every run needs BUFFER/PULL/RETURN moves. Advances are
    # issued one step at a time on purpose: they grow many PULL_RUN_ADVANCED
    # events to paginate over. OB-A takes 18 steps (17 advances), OB-B takes
    # 6 steps (5 advances).
    def pull_and_depart(outbound: str, cars_in_plan: list[str]) -> int:
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": outbound, "destination": "N4", "car_codes": cars_in_plan},
        )
        sequenced = api.expect_ok(
            "POST",
            f"/api/outbound-trains/{outbound}/sequencer",
            {"transfer_code": "X1"},
        )
        run_code = sequenced["pull_run"]["code"]
        total_steps = len(sequenced["pull_run"]["steps"])
        verbs = [step["verb"] for step in sequenced["pull_run"]["steps"]]
        assert "BUFFER" in verbs and "RETURN" in verbs and "PULL" in verbs
        advanced = {}
        for _ in range(total_steps):
            advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
        assert advanced["completed"] is True
        departed = api.expect_ok("POST", f"/api/outbound-trains/{outbound}/depart", {})
        assert departed["outbound"]["state"] == "DEPARTED"
        return total_steps

    steps_a = pull_and_depart("OB-A", ["C-N4-A2", "C-N4-A1"])
    steps_b = pull_and_depart("OB-B", ["C-N4-B2", "C-N4-B1"])
    advances_a = steps_a - 1
    advances_b = steps_b - 1

    # A late inbound train keeps the shift open: closure must be blocked, and
    # the blocked attempt is an audited event that does not change state.
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-C",
            "route": "RAIL-C",
            "arrival_at": "2026-09-13T10:30:00Z",
            "cars": [car("C-N4-C1")],
        },
    )
    blocked = api.expect_error("POST", f"/api/shifts/{SHIFT}/close", {})
    assert blocked["code"] == "RESOURCE_BUSY"

    _check_legacy_shape(api)
    _check_kind_filters(api, advances_a + advances_b)
    _check_object_filters(api, advances_a, advances_b)
    _check_time_filters(api)
    _check_relation(api, advances_a, advances_b)
    _check_validation(api)
    blockers_written = _check_paging_with_concurrent_appends(api)
    blockers_written += _check_parallel_writers(api)

    # Finish the shift only after all paging assertions: classify the late
    # intake, then the second closure succeeds.
    api.expect_ok("POST", "/api/intake-trains/INT-C/classify", {})
    closed = api.expect_ok("POST", f"/api/shifts/{SHIFT}/close", {})
    assert closed["shift"]["state"] == "CLOSED"

    # Final blocked-closure accounting: one from the main flow plus every
    # blocker interleaved into the paging walks. None of them closed anything.
    final = event_page(api, kind="CLOSURE_BLOCKED", limit=200)
    assert final["total"] == 1 + blockers_written
    for event in final["events"]:
        assert event["relation"]["status"] == "REJECTED"
        assert event["relation"]["state_matches"] is False


def _check_legacy_shape(api: ApiClient) -> None:
    # No parameters -> original response with at most 40 recent events.
    data = robust_ok(api, "GET", f"/api/shifts/{SHIFT}")
    assert set(data.keys()) == {"shift", "events"}
    assert len(data["events"]) <= 40
    assert "relation" not in data["events"][0]


def _check_kind_filters(api: ApiClient, total_advances: int) -> None:
    blocked_page = event_page(api, kind="CLOSURE_BLOCKED")
    assert blocked_page["total"] == 1
    event = blocked_page["events"][0]
    assert event["relation"]["status"] == "REJECTED"
    assert event["relation"]["state_matches"] is False
    assert blocked_page["filter"]["kinds"] == ["CLOSURE_BLOCKED"]

    page = event_page(api, kind="PULL_RUN_ADVANCED", limit=5)
    assert page["total"] == total_advances
    assert page["has_more"] is True
    assert len(page["events"]) == 5

    # Comma list and repeated parameter are equivalent.
    comma = event_page(api, kind="PULL_RUN_ADVANCED,PULL_RUN_COMPLETED", limit=200)
    repeated = event_page(api, kind=["PULL_RUN_ADVANCED", "PULL_RUN_COMPLETED"], limit=200)
    assert comma["total"] == total_advances + 2
    assert sequences(repeated) == sequences(comma)

    received = event_page(api, kind="TRAIN_RECEIVED", limit=50)
    assert received["total"] == 3


def _check_object_filters(api: ApiClient, advances_a: int, advances_b: int) -> None:
    # Run code: plan + started + advances + completed.
    run_page = event_page(api, object_code="RUN-OB-A", limit=200)
    kinds = [event["kind"] for event in run_page["events"]]
    assert kinds.count("PULL_PLANNED") == 1
    assert kinds.count("PULL_RUN_STARTED") == 1
    assert kinds.count("PULL_RUN_ADVANCED") == advances_a
    assert kinds.count("PULL_RUN_COMPLETED") == 1
    assert run_page["total"] == advances_a + 3

    # Outbound code additionally surfaces its draft/departure events.
    outbound_page = event_page(api, object_code="OB-A", limit=200)
    outbound_kinds = [event["kind"] for event in outbound_page["events"]]
    assert outbound_kinds[0] == "TRAIN_CREATED"
    assert outbound_kinds[-1] == "TRAIN_DEPARTED"
    assert set(sequences(run_page)).issubset(set(sequences(outbound_page)))

    # Intake code: received + classified.
    intake_page = event_page(api, object_code="INT-A", limit=50)
    assert [event["kind"] for event in intake_page["events"]] == [
        "TRAIN_RECEIVED",
        "TRAIN_CLASSIFIED",
    ]

    # Car code: planned, the pull step, and the departure of its outbound.
    car_page = event_page(api, object_code="C-N4-A2", limit=50)
    car_kinds = [event["kind"] for event in car_page["events"]]
    assert "PULL_PLANNED" in car_kinds
    assert "PULL_RUN_ADVANCED" in car_kinds
    assert "TRAIN_DEPARTED" in car_kinds

    # Shift code returns the whole trail.
    shift_page = event_page(api, object_code=SHIFT, limit=200)
    assert shift_page["total"] == len(shift_page["events"])

    # Filters combine: run + advance kind.
    combo = event_page(api, object_code="RUN-OB-B", kind="PULL_RUN_ADVANCED", limit=50)
    assert combo["total"] == advances_b


def _check_time_filters(api: ApiClient) -> None:
    all_events = event_page(api, limit=200)["events"]
    all_sequences = [event["sequence"] for event in all_events]
    timestamps = sorted(event["at"] for event in all_events)
    day_start = timestamps[0][:10] + "T00:00:00Z"
    day_end = timestamps[0][:10] + "T23:59:59Z"

    # A wide window returns everything, oldest first.
    window = event_page(api, start_at=day_start, end_at=day_end, limit=200)
    assert sequences(window) == all_sequences

    second_ts = timestamps[1]
    last_ts = timestamps[-1]

    # start_at is inclusive.
    tail = event_page(api, start_at=second_ts, limit=200)
    expected_tail = [seq for seq, event in zip(all_sequences, all_events) if event["at"] >= second_ts]
    assert sequences(tail) == expected_tail

    # end_at is inclusive.
    head = event_page(api, end_at=second_ts, limit=200)
    assert sequences(head) == [event["sequence"] for event in all_events if event["at"] <= second_ts]

    # A window after the trail is empty.
    empty = event_page(api, start_at="2099-01-01T00:00:00Z", limit=200)
    assert empty["events"] == []
    assert empty["total"] == 0
    assert empty["has_more"] is False

    # Combined with kind, using the real recorded timestamp of the blocker.
    blocked_page = event_page(api, kind="CLOSURE_BLOCKED")
    blocked_at = blocked_page["events"][0]["at"]
    blocked = event_page(
        api,
        kind="CLOSURE_BLOCKED",
        start_at=blocked_at,
        end_at=blocked_at,
        limit=50,
    )
    assert blocked["total"] == 1
    assert last_ts >= blocked_at


def _check_relation(api: ApiClient, advances_a: int, advances_b: int) -> None:
    page = event_page(api, object_code="RUN-OB-A", limit=200)
    by_kind: dict[str, list[dict[str, object]]] = {}
    for event in page["events"]:
        by_kind.setdefault(event["kind"], []).append(event)

    # Every earlier advance is superseded by later progress (including the last
    # advance, superseded by the completion event).
    advances = by_kind["PULL_RUN_ADVANCED"]
    assert len(advances) == advances_a
    assert all(event["relation"]["status"] == "SUPERSEDED" for event in advances)
    completed = by_kind["PULL_RUN_COMPLETED"][0]
    assert completed["relation"]["status"] == "CURRENT"
    assert completed["relation"]["current_state"] == "COMPLETED"
    assert completed["relation"]["state_matches"] is True

    # Cars reported on the completion event reflect the final departed state,
    # not the intermediate assembled state.
    car_states = {item["code"]: item for item in completed["car_states"]}
    assert car_states["C-N4-A1"]["state"] == "DEPARTED"
    assert car_states["C-N4-A2"]["state"] == "DEPARTED"

    # Each advance event carries the current state of the car it touched; the
    # first move buffers C-B3, which eventually comes back standing.
    first_advance = advances[0]
    assert first_advance["car_states"], "advance event must expose touched cars"
    for item in first_advance["car_states"]:
        assert item["exists"] is True
        assert item["state"] in {"DEPARTED", "STANDING"}
    buffered_car = first_advance["car_states"][0]["code"]
    assert buffered_car.startswith("C-N4-B")

    # Blocked closure is REJECTED and never became the shift's current state.
    blocked_page = event_page(api, kind="CLOSURE_BLOCKED")
    blocked_event = blocked_page["events"][0]
    assert blocked_event["relation"]["current_state"] == "OPEN"
    assert blocked_event["relation"]["declared_state"] is None

    # Intake received is superseded by classified, which is current.
    intake_page = event_page(api, object_code="INT-A", limit=50)
    intake_events = intake_page["events"]
    assert intake_events[0]["relation"]["status"] == "SUPERSEDED"
    assert intake_events[0]["relation"]["current_state"] == "CLASSIFIED"
    assert intake_events[1]["relation"]["status"] == "CURRENT"

    # Train draft is superseded; departure is current.
    outbound_page = event_page(api, object_code="OB-A", limit=200)
    draft_event = outbound_page["events"][0]
    depart_event = outbound_page["events"][-1]
    assert draft_event["relation"]["status"] == "SUPERSEDED"
    assert depart_event["relation"]["status"] == "CURRENT"
    assert depart_event["relation"]["current_state"] == "DEPARTED"


def _check_validation(api: ApiClient) -> None:
    bad_kind = api.expect_error("GET", f"/api/shifts/{SHIFT}?kind=NOT_A_KIND")
    assert bad_kind["code"] == "VALIDATION_ERROR"
    bad_window = api.expect_error(
        "GET",
        f"/api/shifts/{SHIFT}?start_at=2026-09-13T12:00:00Z&end_at=2026-09-13T11:00:00Z",
    )
    assert bad_window["code"] == "VALIDATION_ERROR"
    bad_limit = api.expect_error("GET", f"/api/shifts/{SHIFT}?limit=0")
    assert bad_limit["code"] == "VALIDATION_ERROR"
    bad_cursor = api.expect_error("GET", f"/api/shifts/{SHIFT}?limit=10&cursor=not-a-cursor")
    assert bad_cursor["code"] == "VALIDATION_ERROR"
    missing = api.expect_error("GET", "/api/shifts/SHIFT-NOPE?limit=10")
    assert missing["code"] == "NOT_FOUND"


def _write_blocker(api: ApiClient) -> None:
    # INT-C stays open until the end of the check, so the closure attempt keeps
    # failing and appends exactly one audited CLOSURE_BLOCKED event per call.
    error = robust_error(api, "POST", f"/api/shifts/{SHIFT}/close", {})
    assert error["code"] == "RESOURCE_BUSY"


def _walk(api: ApiClient, interleave, expect_growth: bool = True, **params: object) -> tuple[list[int], int]:
    """Walk all pages, calling interleave() after every page fetch."""

    cursor: str | None = None
    seen: list[int] = []
    pages = 0
    anchor: int | None = None
    total: int | None = None
    previous_live = 0
    while True:
        page_params = dict(params)
        if cursor is not None:
            page_params["cursor"] = cursor
        page = robust_ok(api, "GET", q(f"/api/shifts/{SHIFT}", **page_params))
        if pages == 0:
            anchor = int(page["anchor"])
            total = int(page["total"])
            previous_live = int(page["live_total"])
        else:
            # The anchored snapshot is immutable: total/anchor are constant and
            # new events are reported separately, never folded into the walk.
            assert page["anchor"] == anchor
            assert page["total"] == total
            assert int(page["new_event_count"]) == int(page["live_total"]) - anchor
            if expect_growth:
                assert int(page["live_total"]) >= previous_live
            previous_live = int(page["live_total"])
        page_sequences = sequences(page)
        if seen:
            assert page_sequences[0] > seen[-1], "pages overlapped or moved backwards"
        seen.extend(page_sequences)
        cursor = page.get("next_cursor")
        pages += 1
        interleave()
        if not cursor:
            break
        if pages > 200:
            raise AssertionError("pagination did not terminate")
    return seen, anchor


def _check_paging_with_concurrent_appends(api: ApiClient) -> int:
    blockers_written = 0

    def write_blocker() -> None:
        nonlocal blockers_written
        _write_blocker(api)
        blockers_written += 1

    # 1) Filtered walk with one new event of another kind after every page.
    filtered, filtered_anchor = _walk(
        api, write_blocker, kind="PULL_RUN_ADVANCED", limit=3
    )
    advance_total = event_page(api, kind="PULL_RUN_ADVANCED", limit=1)["total"]
    expected_pages = math.ceil(int(advance_total) / 3)
    assert len(filtered) == advance_total
    assert len(set(filtered)) == len(filtered), "an event appeared more than once"
    assert filtered == sorted(filtered), "events were not returned oldest first"
    assert all(seq <= filtered_anchor for seq in filtered)
    # Re-walking the same cursor without new writes yields the identical set.
    repeat = _drain(api, kind="PULL_RUN_ADVANCED", limit=3)
    assert repeat == filtered

    # 2) Unfiltered walk under the same pressure: an offset-based window would
    #    skip events as new ones land; keyset-on-anchor must return exactly the
    #    anchored snapshot, once each.
    all_seen, anchor = _walk(api, write_blocker, limit=5)
    assert all_seen == list(range(1, anchor + 1)), "paging skipped or duplicated an event"
    assert all(seq <= anchor for seq in all_seen)

    # 3) Object-code walk while appending: every run-related sequence once.
    run_seen, _ = _walk(api, write_blocker, object_code="RUN-OB-B", limit=2)
    assert len(run_seen) == len(set(run_seen)) == 8

    # 4) Restarting without a cursor adopts the new high water mark and surfaces
    #    every event appended during the walks.
    fresh = event_page(api, limit=200)
    assert fresh["anchor"] > anchor
    assert fresh["new_event_count"] == 0
    fresh_blockers = [event for event in fresh["events"] if event["kind"] == "CLOSURE_BLOCKED"]
    # One pre-walk blocker plus one blocker after each page of every walk.
    expected_pages_unfiltered = math.ceil(anchor / 5)
    expected_blockers = 1 + expected_pages + expected_pages_unfiltered + math.ceil(8 / 2)
    assert len(fresh_blockers) == expected_blockers
    assert blockers_written == expected_blockers - 1

    # 5) Boundary: the last matching event is followed by non-matching events.
    #    Page exactly to the last TRAIN_RECEIVED event; has_more must be false
    #    even though higher (non-matching) sequences exist in the snapshot.
    received_total = int(event_page(api, kind="TRAIN_RECEIVED", limit=1)["total"])
    boundary = _drain(api, kind="TRAIN_RECEIVED", limit=1)
    assert len(boundary) == received_total
    assert boundary[-1] < int(event_page(api, limit=1)["live_total"])
    # Walking from a stale cursor after new appends still covers exactly the
    # original snapshot.
    first = event_page(api, kind="TRAIN_RECEIVED", limit=2)
    stale_cursor = first["next_cursor"]
    _write_blocker(api)
    blockers_written += 1
    rest = robust_ok(
        api,
        "GET",
        q(f"/api/shifts/{SHIFT}", kind="TRAIN_RECEIVED", limit=2, cursor=stale_cursor),
    )
    assert sequences(rest) == boundary[2:]
    assert rest["total"] == received_total

    # 6) A cursor minted for one filter must not be reused for another.
    other = event_page(api, kind="TRAIN_RECEIVED", limit=1)
    stolen = other["next_cursor"]
    mismatch = robust_error(
        api,
        "GET",
        q(f"/api/shifts/{SHIFT}", kind="TRAIN_DEPARTED", limit=1, cursor=stolen),
    )
    assert mismatch["code"] == "VALIDATION_ERROR"

    return blockers_written


def _drain(api: ApiClient, **params: object) -> list[int]:
    cursor: str | None = None
    seen: list[int] = []
    while True:
        page_params = dict(params)
        if cursor is not None:
            page_params["cursor"] = cursor
        page = event_page(api, **page_params)
        seen.extend(sequences(page))
        cursor = page.get("next_cursor")
        if not cursor:
            return seen


def _check_parallel_writers(api: ApiClient) -> int:
    """Several threads append blocked-closure events while paging proceeds."""

    baseline = event_page(api, limit=1)
    baseline_total = int(baseline["live_total"])

    writer_rounds = 6
    writer_count = 4
    written: list[int] = []
    written_lock = threading.Lock()

    def writer() -> None:
        for _ in range(writer_rounds):
            error = robust_error(api, "POST", f"/api/shifts/{SHIFT}/close", {})
            assert error["code"] == "RESOURCE_BUSY"
            with written_lock:
                written.append(1)
            time.sleep(0.01)

    threads = [threading.Thread(target=writer) for _ in range(writer_count)]
    for thread in threads:
        thread.start()

    # Page while writers append concurrently; the anchored walk must neither
    # skip nor duplicate even though the live journal grows at the same time.
    seen, anchor = _walk(api, lambda: None, expect_growth=False, limit=4)
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    expected_writes = writer_rounds * writer_count
    assert len(written) == expected_writes
    assert seen == list(range(1, anchor + 1))

    # Every concurrent append survived (write lock prevents lost updates) and
    # they all landed above the walk's anchor.
    settled = event_page(api, limit=1)
    live_growth = int(settled["live_total"]) - baseline_total
    assert live_growth == expected_writes, f"lost {expected_writes - live_growth} concurrent events"
    final = event_page(api, kind="CLOSURE_BLOCKED", limit=200)
    assert int(final["total"]) >= expected_writes
    return expected_writes


if __name__ == "__main__":
    raise SystemExit(run_check("wf_shift_event_query", run))
