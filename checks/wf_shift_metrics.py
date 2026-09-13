"""Workflow check: shift work metrics for clean and rework-heavy shifts.

Exercises the read-only ``GET /api/shifts/{code}/work-metrics`` view and proves
that metrics, per-entity details, and the raw event trail explain each other.
The check also restarts the service against the same data directory to verify
cross-restart stability and runs an in-process failed-run scenario that the
HTTP API cannot otherwise create.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from support import ApiClient, RunningServer, run_check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def car(code: str, destination: str = "N4", kind: str = "BOX", danger: str = "NONE", length: int = 18) -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": danger,
    }


def metrics(api: ApiClient, shift: str, query: str = "") -> dict[str, object]:
    suffix = f"?{query}" if query else ""
    return api.expect_ok("GET", f"/api/shifts/{shift}/work-metrics{suffix}")


def by_key(rows: list[dict[str, object]], key: str) -> dict[str, dict[str, object]]:
    return {str(row[key]): row for row in rows}


# ---------------------------------------------------------------------------
# Shift A: one complete, successful shift
# ---------------------------------------------------------------------------


def drive_clean_shift(api: ApiClient) -> dict[str, object]:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-A1", "dispatcher": "LIN", "opened_at": utc_now()},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-A1",
            "route": "RAIL-A1",
            "arrival_at": "2026-09-13T08:10:00Z",
            "cars": [
                car("C-N4-A1"),
                car("C-BLK-A1"),
                car("C-N4-A2", kind="REEFER", length=20),
            ],
        },
    )
    classify = api.expect_ok("POST", "/api/intake-trains/INT-A1/classify", {})
    assert classify["intake"]["placed_at"] is not None

    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-A1", "destination": "N4", "car_codes": ["C-N4-A2", "C-N4-A1"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-A1/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    verbs = [step["verb"] for step in sequenced["pull_run"]["steps"]]
    assert verbs == ["PULL", "BUFFER", "PULL", "RETURN"]

    # Execute the run in two crew batches to exercise repeated execution.
    first = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    assert first["completed"] is False
    second = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})
    assert second["completed"] is True
    api.expect_ok("POST", "/api/outbound-trains/OB-A1/depart", {})

    doc = metrics(api, "SHIFT-A1")
    m = doc["metrics"]
    assert m["shift"]["closed_at"] is None
    assert m["shift"]["duration_seconds"] is None  # open shift: unknown, never zero

    d = m["durations_seconds"]
    assert d["receive_to_classify"]["sample_size"] == 1
    assert d["receive_to_classify"]["average_seconds"] is not None
    assert d["classify_to_assemble"]["sample_size"] == 2
    assert d["plan_to_complete"]["sample_size"] == 1
    assert d["plan_to_depart"]["sample_size"] == 1
    assert d["plan_to_depart"]["average_seconds"] >= d["plan_to_complete"]["average_seconds"]

    pull = m["pull_completion"]
    assert pull["planned_runs"] == 1
    assert pull["completed_runs"] == 1
    assert pull["failed_runs"] == 0
    assert pull["incomplete_runs"] == 0
    assert pull["completion_rate_percent"] == 100.0
    assert pull["executed_moves"] == {"buffer": 1, "pull": 2, "return": 1, "total": 4}
    assert pull["pulled_cars"] == 2
    assert pull["average_buffers_per_completed_run"] == 1.0
    assert pull["average_buffers_per_pull"] == 0.5

    rework = m["rework"]
    assert rework["closure_blocked_attempts"] == 0
    assert rework["classification_reattempts"] == 0
    # Two execution batches (one advanced, one completed) => one extra batch.
    assert rework["pull_run_multi_batch_count"] == 1
    assert rework["pull_run_extra_batch_events"] == 1
    assert rework["total_repeated_attempts"] == 1
    batches = by_key(rework["execution_batches"], "run_code")
    assert batches[run_code]["batches"] == 2
    assert batches[run_code]["advance_events"] == 1
    assert batches[run_code]["completed_events"] == 1

    # Details explain the aggregates.
    intakes = by_key(doc["details"]["intakes"], "intake_code")
    intake_row = intakes["INT-A1"]
    assert intake_row["classification_attempts"] == 1
    assert intake_row["state"] == "CLASSIFIED"
    assert intake_row["unplaced_after_last_attempt"] == []
    assert intake_row["receive_to_classify_seconds"] is not None

    runs = by_key(doc["details"]["pull_runs"], "run_code")
    run_row = runs[run_code]
    assert run_row["outcome"] == "COMPLETED"
    assert run_row["planned_steps"] == 4
    assert run_row["executed_steps_in_window"] == 4
    assert run_row["execution_batches"] == 2
    assert run_row["multi_batch"] is True
    assert run_row["buffer_moves"] == 1
    assert run_row["pull_moves"] == 2
    assert run_row["return_moves"] == 1
    assert run_row["plan_to_complete_seconds"] is not None
    assert run_row["start_to_complete_seconds"] is not None

    cars = by_key(doc["details"]["cars"], "car_code")
    assert cars["C-N4-A2"]["classify_to_assemble_seconds"] is not None
    assert cars["C-N4-A2"]["state_after_window"] == "DEPARTED"
    # Buffered blocker was never assembled: unknown duration stays null.
    assert cars["C-BLK-A1"]["assembled_at"] is None
    assert cars["C-BLK-A1"]["classify_to_assemble_seconds"] is None
    assert cars["C-BLK-A1"]["state_after_window"] == "STANDING"

    outbounds = by_key(doc["details"]["outbounds"], "outbound_code")
    assert outbounds["OB-A1"]["state"] == "DEPARTED"
    assert outbounds["OB-A1"]["planned_cars"] == 2
    assert outbounds["OB-A1"]["assembled_cars"] == 2
    assert outbounds["OB-A1"]["plan_to_depart_seconds"] is not None

    # Occupancy replay: N4-A went 0 -> 3 -> (buffer/pull/return/pull) -> 1.
    tracks = by_key(doc["occupancy"]["tracks"], "track_code")
    n4 = tracks["N4-A"]
    assert n4["cars_before_window"] == 0
    assert n4["cars_after_window"] == 1
    assert n4["net_change"] == 1
    assert n4["peak_cars_in_window"] == 3
    assert n4["cars_moved_out"] == 3  # 1 buffer + 2 pulls
    assert n4["cars_moved_in"] == 1  # blocker return
    bays = by_key(doc["occupancy"]["transfer_bays"], "bay_code")
    assert bays["X1"]["peak_cars_in_window"] == 1
    assert bays["X1"]["buffered_in_window"] == 1
    assert bays["X1"]["returned_in_window"] == 1
    assert bays["X1"]["cars_after_window"] == 0

    dests = by_key(doc["occupancy"]["destinations"], "destination")
    n4d = dests["N4"]
    assert n4d["on_hand_before_window"] == 0
    assert n4d["on_hand_after_window"] == 1  # blocker remains
    assert n4d["departed_in_window"] == 2

    # Raw events travel with the document and reference every detail sequence.
    raw_kinds = [event["kind"] for event in doc["events"]]
    assert raw_kinds == [
        "SHIFT_OPENED",
        "TRAIN_RECEIVED",
        "TRAIN_CLASSIFIED",
        "TRAIN_CREATED",
        "PULL_PLANNED",
        "PULL_RUN_STARTED",
        "PULL_RUN_ADVANCED",
        "PULL_RUN_COMPLETED",
        "TRAIN_DEPARTED",
    ]
    all_sequences = {event["sequence"] for event in doc["events"]}
    for row in (intake_row, run_row, outbounds["OB-A1"]):
        assert set(row["event_sequences"]) <= all_sequences

    # Idempotence: same batch of events computes the exact same document.
    again = metrics(api, "SHIFT-A1")
    assert json.dumps(doc, sort_keys=True) == json.dumps(again, sort_keys=True)

    # Read-only: querying metrics must not append events.
    before = len(doc["events"])
    third = metrics(api, "SHIFT-A1")
    assert len(third["events"]) == before

    return doc


def close_and_freeze(api: ApiClient, open_doc: dict[str, object]) -> dict[str, object]:
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-A1/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    snapshot_work = closed["snapshot"]["work_metrics"]
    doc = metrics(api, "SHIFT-A1")
    # Snapshot at closure and live read of a closed shift are identical.
    assert snapshot_work == doc["metrics"]
    assert doc["metrics"]["shift"]["duration_seconds"] is not None
    assert doc["metrics"]["shift"]["closed_at"] == closed["shift"]["closed_at"]
    # Frozen: a second read after no writes stays byte-identical.
    again = metrics(api, "SHIFT-A1")
    assert json.dumps(doc, sort_keys=True) == json.dumps(again, sort_keys=True)
    return doc


# ---------------------------------------------------------------------------
# Shift B: partial classification, repeated classify attempts, blocked closure
# ---------------------------------------------------------------------------


def drive_rework_shift(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-B1", "dispatcher": "MA", "opened_at": utc_now()},
    )
    # Nine hazardous cars but HAZ-1 holds only eight: classification is
    # partial on the first attempt and still partial on the repeated attempt.
    cars = [car(f"C-HAZ-B{i}", destination="W9", kind="TANK", danger="D1") for i in range(1, 10)]
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {"code": "INT-B1", "route": "RAIL-B1", "arrival_at": "2026-09-13T14:05:00Z", "cars": cars},
    )
    first = api.expect_ok("POST", "/api/intake-trains/INT-B1/classify", {})
    assert first["intake"]["state"] == "PARTIAL"
    assert len(first["unplaced"]) == 1
    assert first["intake"]["placed_at"] is None  # optional timestamp stays unknown
    second = api.expect_ok("POST", "/api/intake-trains/INT-B1/classify", {})
    assert second["intake"]["state"] == "PARTIAL"
    assert len(second["unplaced"]) >= 1

    # Closure must be blocked twice (one unclassified car), and each block is
    # itself a journaled, replayed event.
    blocked_one = api.expect_error("POST", "/api/shifts/SHIFT-B1/close", {})
    blocked_two = api.expect_error("POST", "/api/shifts/SHIFT-B1/close", {})
    assert blocked_one["code"] == "RESOURCE_BUSY"
    assert blocked_two["code"] == "RESOURCE_BUSY"

    doc = metrics(api, "SHIFT-B1")
    m = doc["metrics"]
    rework = m["rework"]
    assert rework["closure_blocked_attempts"] == 2
    assert rework["classification_reattempts"] == 1
    assert rework["total_repeated_attempts"] == 3
    attempts = by_key(rework["classification_attempts"], "intake_code")
    assert attempts["INT-B1"]["attempts"] == 2

    # Partial work contributes zero completed samples; unknown is not zero.
    d = m["durations_seconds"]
    assert d["receive_to_classify"]["sample_size"] == 0
    assert d["receive_to_classify"]["average_seconds"] is None
    assert d["receive_to_classify"]["max_seconds"] is None
    pull = m["pull_completion"]
    assert pull["planned_runs"] == 0
    assert pull["completed_runs"] == 0
    assert pull["completion_rate_percent"] is None  # no runs: unknown, not 0%

    intake = by_key(doc["details"]["intakes"], "intake_code")["INT-B1"]
    assert intake["state"] == "PARTIAL"
    assert len(intake["unplaced_after_last_attempt"]) == 1
    assert intake["classified_at"] is None
    assert intake["receive_to_classify_seconds"] is None
    assert intake["classification_attempts"] == 2
    assert len(intake["event_sequences"]) == 3  # receipt + two attempts

    haz = by_key(doc["occupancy"]["tracks"], "track_code")["HAZ-1"]
    assert haz["cars_after_window"] == 8
    assert haz["peak_cars_in_window"] == 8

    # Every retry count is traceable to raw events in the document.
    raw = doc["events"]
    assert sum(1 for e in raw if e["kind"] == "CLOSURE_BLOCKED") == 2
    assert sum(1 for e in raw if e["kind"] == "TRAIN_CLASSIFIED") == 2
    blocked_seqs = [e["sequence"] for e in raw if e["kind"] == "CLOSURE_BLOCKED"]
    assert blocked_seqs[0] < blocked_seqs[1]


# ---------------------------------------------------------------------------
# Range queries: every number must be explained by the events in the window
# ---------------------------------------------------------------------------


def check_ranges(api: ApiClient, clean_doc: dict[str, object]) -> None:
    sequences = [event["sequence"] for event in clean_doc["events"]]
    first_seq, last_seq = sequences[0], sequences[-1]
    classify_seq = next(
        event["sequence"] for event in clean_doc["events"] if event["kind"] == "TRAIN_CLASSIFIED"
    )

    # Window ending exactly at classification: intake done, no pull work.
    early = metrics(api, "SHIFT-A1", f"from_sequence={first_seq}&to_sequence={classify_seq}")
    assert early["window"]["bounded"] is True
    assert early["window"]["event_count"] == 3
    assert early["metrics"]["pull_completion"]["planned_runs"] == 0
    assert early["metrics"]["pull_completion"]["completion_rate_percent"] is None
    tracks = by_key(early["occupancy"]["tracks"], "track_code")
    assert tracks["N4-A"]["cars_before_window"] == 0
    assert tracks["N4-A"]["cars_after_window"] == 3
    intakes = by_key(early["details"]["intakes"], "intake_code")
    assert intakes["INT-A1"]["receive_to_classify_seconds"] is not None

    # Window starting after classification: baseline already holds three cars.
    late = metrics(api, "SHIFT-A1", f"from_sequence={classify_seq + 1}")
    tracks = by_key(late["occupancy"]["tracks"], "track_code")
    assert tracks["N4-A"]["cars_before_window"] == 3
    assert tracks["N4-A"]["cars_after_window"] == 1
    assert tracks["N4-A"]["cars_moved_out"] == 3
    pull = late["metrics"]["pull_completion"]
    assert pull["completed_runs"] == 1
    assert pull["completion_rate_percent"] == 100.0
    # Car duration chains started before this window: timestamps still resolve
    # through the pre-window context, so values are not misread as unknown.
    cars = by_key(late["details"]["cars"], "car_code")
    assert cars["C-N4-A2"]["classify_to_assemble_seconds"] is not None
    assert cars["C-N4-A2"]["state_before_window"] == "STANDING"
    assert cars["C-N4-A2"]["state_after_window"] == "DEPARTED"

    # Range validation and unknown shifts.
    status, body = api.request(
        "GET", "/api/shifts/SHIFT-A1/work-metrics?from_sequence=abc"
    )
    assert status == 422 and body["error"]["code"] == "VALIDATION_ERROR"
    status, body = api.request(
        "GET", "/api/shifts/SHIFT-A1/work-metrics?from_sequence=99&to_sequence=2"
    )
    assert status == 422
    status, body = api.request("GET", "/api/shifts/NOPE/work-metrics")
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"

    # include_events=false suppresses raw events but keeps metrics identical.
    no_events = metrics(api, "SHIFT-A1", "include_events=false")
    assert no_events["events"] == []
    assert no_events["metrics"] == clean_doc["metrics"]


# ---------------------------------------------------------------------------
# Cross-restart stability over the same data directory
# ---------------------------------------------------------------------------


def check_restart(data_dir: Path, clean_doc: dict[str, object]) -> None:
    server = RunningServer(data_dir=data_dir, cleanup=False)
    try:
        server.wait_ready()
        api = server.api
        reopened = metrics(api, "SHIFT-A1")
        # Exact JSON equality over the whole document (event at-times included).
        assert json.dumps(reopened, sort_keys=True) == json.dumps(clean_doc, sort_keys=True)
        rework = metrics(api, "SHIFT-B1")
        assert rework["metrics"]["rework"]["closure_blocked_attempts"] == 2
        assert rework["metrics"]["rework"]["classification_reattempts"] == 1
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# In-process scenario: a failed pull run cannot be closed, must not be zeroed
# ---------------------------------------------------------------------------


def check_failed_run_in_process() -> None:
    """A planned run that fails mid-shift: failed counts, unknown durations.

    The HTTP workflow never persists a failed run (an aborted advance commits
    nothing), so this scenario is assembled in-process against the real domain
    objects and transition table.
    """
    from switchyard.domain.car import FreightCar
    from switchyard.domain.enums import CarKind, CarState, EventKind, RunState, ShiftState
    from switchyard.domain.outbound import OutboundTrain
    from switchyard.domain.pull import PullRun
    from switchyard.domain.sequencer import plan_pull_run
    from switchyard.domain.shift import YardShift
    from switchyard.domain.transitions import transition_run
    from switchyard.report.shift_metrics import shift_work_metrics
    from switchyard.service.context import YardApplication

    with tempfile.TemporaryDirectory(prefix="switchyard-failed-") as tmp:
        app = YardApplication(tmp)
        ws = app.load()
        ws.shifts["SHIFT-F1"] = YardShift(
            code="SHIFT-F1", dispatcher="X", opened_at=utc_now()
        )
        ws.record_event("SHIFT-F1", EventKind.SHIFT_OPENED, "shift SHIFT-F1 opened")

        car_one = FreightCar(
            code="C-N4-F1",
            kind=CarKind.BOX,
            destination="N4",
            loaded=True,
            length_m=18,
            state=CarState.STANDING,
            location="N4-A",
        )
        ws.cars[car_one.code] = car_one
        ws.tracks["N4-A"].stack.append(car_one.code)

        outbound = OutboundTrain(
            code="OB-F1",
            destination="N4",
            planned_car_codes=[car_one.code],
            created_at="2026-09-13T20:10:00Z",
        )
        ws.outbounds["OB-F1"] = outbound
        run = plan_pull_run("RUN-OB-F1", outbound, ws.cars, ws.tracks, ws.buffer_bays, "X1")
        ws.runs[run.code] = run
        ws.record_event(
            "SHIFT-F1",
            EventKind.PULL_PLANNED,
            "pull run RUN-OB-F1 planned for OB-F1",
            {
                "run_code": "RUN-OB-F1",
                "outbound_code": "OB-F1",
                "planned_car_codes": [car_one.code],
                "steps": 1,
                "move_steps": [step.to_dict() for step in run.steps],
            },
        )
        transition_run(run, RunState.RUNNING)
        run.started_at = "2026-09-13T20:11:00Z"
        transition_run(run, RunState.FAILED)  # failed mid-shift, never completed

        doc = shift_work_metrics(ws, "SHIFT-F1")
        pull = doc["metrics"]["pull_completion"]
        assert pull["planned_runs"] == 1
        assert pull["completed_runs"] == 0
        assert pull["failed_runs"] == 1
        assert pull["incomplete_runs"] == 0
        assert pull["completion_rate_percent"] == 0.0  # 0 of 1 completed, known zero

        run_row = by_key(doc["details"]["pull_runs"], "run_code")["RUN-OB-F1"]
        assert run_row["outcome"] == "FAILED"
        assert run_row["state"] == "FAILED"
        assert run_row["planned_at"] is not None
        assert run_row["started_at"] == "2026-09-13T20:11:00Z"
        # No completion timestamp: completion duration is unknown, never zero.
        assert run_row["completed_at"] is None
        assert run_row["plan_to_complete_seconds"] is None
        assert run_row["start_to_complete_seconds"] is None

        d = doc["metrics"]["durations_seconds"]
        assert d["plan_to_complete"]["sample_size"] == 0
        assert d["plan_to_complete"]["average_seconds"] is None

        # Open shift duration stays unknown too.
        assert doc["metrics"]["shift"]["state"] == "OPEN"
        assert doc["metrics"]["shift"]["duration_seconds"] is None


def run_with_persistence() -> None:
    tmp = tempfile.TemporaryDirectory(prefix="switchyard-metrics-")
    data_dir = Path(tmp.name) / "data"
    server = RunningServer(data_dir=data_dir, cleanup=False)
    try:
        server.wait_ready()
        api = server.api
        clean_doc = drive_clean_shift(api)
        check_ranges(api, clean_doc)
        final_doc = close_and_freeze(api, clean_doc)
        drive_rework_shift(api)
    finally:
        server.stop()
    check_restart(data_dir, final_doc)
    tmp.cleanup()
    check_failed_run_in_process()


if __name__ == "__main__":
    # Persistence + restart needs its own harness rather than the default
    # ephemeral-server wrapper.
    run_with_persistence()
    print("OK wf_shift_metrics")
    raise SystemExit(0)
