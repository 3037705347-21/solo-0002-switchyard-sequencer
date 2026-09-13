"""Shift work metrics derived read-only from the event trail and object timestamps.

The module never mutates the workspace and never records events. Every value is
derived from:

* the append-only yard events (ordered by their stable ``sequence``), and
* the persisted object timestamps (``created_at`` / ``started_at`` /
  ``completed_at`` / ``departed_at`` / ``placed_at`` / ``opened_at`` /
  ``closed_at``).

Unknown durations stay ``None``: a missing optional timestamp must never be
counted as zero, and ``None`` values never participate in averages. Running the
same computation over the same event batch always returns the same document,
which keeps results stable across restarts and historical range queries.

Occupancy for an event range is reconstructed by replaying events: every event
before the window is replayed into a baseline, then the window events are
replayed on a copy so per-track/bay/destination deltas are explained by the
events inside the range instead of being misread from zero.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable

from ..domain.enums import EventKind, RunState
from ..domain.errors import NotFoundError
from ..domain.timeutil import duration_seconds

# Every track/bay code seeded into a fresh yard. Replay needs the full track
# universe so tracks with no events still report an explicit zero delta.
_SEED_TRACK_CODES = ("N4-A", "E7-A", "S2-A", "W9-A", "MIX-1", "HAZ-1", "MAINT-1")
_SEED_BAY_CODES = ("X1",)

_INTAKE_RE = re.compile(r"intake\s+(\S+)", re.IGNORECASE)
_RUN_RE = re.compile(r"pull run\s+(\S+)", re.IGNORECASE)
_OUTBOUND_RE = re.compile(r"outbound\s+(\S+)", re.IGNORECASE)

_MOVE_EVENT_KINDS = {
    EventKind.PULL_RUN_ADVANCED.value,
    EventKind.PULL_RUN_COMPLETED.value,
}

CodeExtractor = Callable[[Any], "str | None"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def shift_work_metrics(
    workspace: Any,
    shift_code: str,
    from_sequence: int | None = None,
    to_sequence: int | None = None,
    include_events: bool = True,
) -> dict[str, Any]:
    """Build the shift work metrics document for a shift and an event range.

    ``from_sequence`` / ``to_sequence`` bound the analysis window by stable
    event sequence; both bounds are inclusive. Aggregate durations count work
    whose terminal event (classified / assembled / completed / departed) lands
    inside the window. Occupancy reports replay the pre-window prefix as the
    baseline and the window events as the change set.
    """
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)

    all_events = sorted(workspace.events, key=lambda item: item.sequence)
    shift_events = [event for event in all_events if event.shift_code == shift_code]
    window_start, window_end = _resolve_window(shift_events, from_sequence, to_sequence)

    prefix_events = [event for event in all_events if event.sequence < window_start]
    window_events = [
        event for event in shift_events if window_start <= event.sequence <= window_end
    ]
    # Terminal timestamps may complete a chain started before the window.
    context_events = [event for event in shift_events if event.sequence <= window_end]

    catalog = _EntityCatalog(workspace)
    baseline = _ReplayState.fresh()
    baseline.apply(prefix_events, catalog)
    end_state = baseline.copy()
    end_state.apply(window_events, catalog)

    event_index = {event.sequence: event for event in all_events}

    return {
        "shift_code": shift_code,
        "window": {
            "from_sequence": window_start,
            "to_sequence": window_end,
            "started_at": _event_at(event_index.get(window_start)),
            "ended_at": _event_at(event_index.get(window_end)),
            "event_count": len(window_events),
            "shift_event_count": len(shift_events),
            "bounded": from_sequence is not None or to_sequence is not None,
        },
        "metrics": _build_metrics(
            workspace,
            shift,
            shift_events,
            context_events,
            window_events,
            window_start,
            window_end,
        ),
        "details": {
            "intakes": _intake_details(workspace, shift_events, window_start, window_end),
            "cars": _car_details(
                context_events, window_events, window_start, window_end, baseline, end_state
            ),
            "pull_runs": _run_details(workspace, shift_events, window_start, window_end),
            "outbounds": _outbound_details(workspace, shift_events, window_start, window_end),
        },
        "occupancy": {
            "tracks": _track_reports(workspace, baseline, end_state, window_events),
            "transfer_bays": _bay_reports(workspace, baseline, end_state, window_events),
            "destinations": _destination_reports(baseline, end_state, catalog),
        },
        "events": [event.to_dict() for event in window_events] if include_events else [],
    }


# ---------------------------------------------------------------------------
# Window handling
# ---------------------------------------------------------------------------


def _resolve_window(
    shift_events: list[Any], from_sequence: int | None, to_sequence: int | None
) -> tuple[int, int]:
    if shift_events:
        first = shift_events[0].sequence
        last = shift_events[-1].sequence
    else:
        first, last = 1, 0
    return (first if from_sequence is None else int(from_sequence)), (
        last if to_sequence is None else int(to_sequence)
    )


def _in_window(sequence: int, start: int, end: int) -> bool:
    return start <= sequence <= end


# ---------------------------------------------------------------------------
# Entity catalog: persisted objects used to enrich and back-fill event data
# ---------------------------------------------------------------------------


class _EntityCatalog:
    def __init__(self, workspace: Any):
        self.workspace = workspace
        self.car_destination = {
            code: str(car.destination) for code, car in workspace.cars.items()
        }

    def destination_of(self, car_code: str) -> str:
        car = self.workspace.cars.get(car_code)
        if car is not None:
            return str(car.destination)
        return self.car_destination.get(car_code, "UNKNOWN")


# ---------------------------------------------------------------------------
# Event identity extraction
#
# New events carry explicit entity codes. Events recorded by older builds only
# embedded them in the human message, which has a fixed shape, so the message
# remains a deterministic fallback.
# ---------------------------------------------------------------------------


def _payload_code(event: Any, key: str, pattern: re.Pattern[str]) -> str | None:
    value = event.payload.get(key)
    if isinstance(value, str) and value:
        return value
    match = pattern.search(event.message)
    return match.group(1) if match else None


def _intake_code(event: Any) -> str | None:
    return _payload_code(event, "intake_code", _INTAKE_RE)


def _run_code(event: Any) -> str | None:
    return _payload_code(event, "run_code", _RUN_RE)


def _outbound_code(event: Any) -> str | None:
    return _payload_code(event, "outbound_code", _OUTBOUND_RE)


def _event_at(event: Any | None) -> str | None:
    return None if event is None else event.at


def _event_str_list(event: Any, key: str) -> list[str]:
    value = event.payload.get(key)
    return [str(item) for item in value] if isinstance(value, list) else []


def _event_steps(workspace: Any, event: Any) -> list[dict[str, Any]]:
    """Executed move steps for an advance/completion event.

    Prefer the explicit event payload. Legacy journal events recorded before
    steps were embedded fall back to the persisted run step plan; when even
    that is unavailable the result is empty rather than guessed.
    """
    steps = event.payload.get("executed_steps")
    if isinstance(steps, list) and steps:
        return [dict(raw) for raw in steps if isinstance(raw, dict)]
    code = _run_code(event)
    run = workspace.runs.get(code) if code is not None else None
    if run is None:
        return []
    planned = [step.to_dict() for step in run.steps]
    if str(event.kind) == EventKind.PULL_RUN_COMPLETED.value:
        return planned
    current = event.payload.get("current_step")
    if isinstance(current, int):
        return planned[: max(0, min(current, len(planned)))]
    return []


# ---------------------------------------------------------------------------
# Deterministic yard replay
# ---------------------------------------------------------------------------


class _ReplayState:
    """Materialized yard occupancy at a point on the event timeline."""

    def __init__(self, track_codes: set[str], bay_codes: set[str]):
        self.tracks: dict[str, list[str]] = {code: [] for code in sorted(track_codes)}
        self.bays: dict[str, list[str]] = {code: [] for code in sorted(bay_codes)}
        self.car_state: dict[str, str] = {}
        self.car_location: dict[str, str | None] = {}
        self.track_peak: dict[str, int] = {code: 0 for code in self.tracks}
        self.bay_peak: dict[str, int] = {code: 0 for code in self.bays}

    @classmethod
    def fresh(cls) -> "_ReplayState":
        return cls(set(_SEED_TRACK_CODES), set(_SEED_BAY_CODES))

    def copy(self) -> "_ReplayState":
        return copy.deepcopy(self)

    def apply(self, events: list[Any], catalog: _EntityCatalog) -> None:
        for event in events:
            kind = str(event.kind)
            if kind == EventKind.TRAIN_RECEIVED.value:
                for code in _event_str_list(event, "car_codes"):
                    self.car_state[code] = "RECEIVED"
                    self.car_location[code] = "INTAKE"
            elif kind == EventKind.TRAIN_CLASSIFIED.value:
                self._apply_classified(event)
            elif kind == EventKind.PULL_PLANNED.value:
                for code in self._planned_codes(event, catalog):
                    self.car_state[code] = "RESERVED"
            elif kind in _MOVE_EVENT_KINDS:
                for raw in _event_steps(catalog.workspace, event):
                    self._apply_move(raw)
                if kind == EventKind.PULL_RUN_COMPLETED.value:
                    outbound_code = _outbound_code(event)
                    for code in _event_str_list(event, "assembled_car_codes"):
                        self.car_state[code] = "ASSEMBLED"
                        if outbound_code:
                            self.car_location[code] = outbound_code
            elif kind == EventKind.TRAIN_DEPARTED.value:
                for code in _event_str_list(event, "car_codes"):
                    self.car_state[code] = "DEPARTED"
                    self.car_location[code] = None

    def _planned_codes(self, event: Any, catalog: _EntityCatalog) -> list[str]:
        codes = _event_str_list(event, "planned_car_codes")
        if codes:
            return codes
        run = catalog.workspace.runs.get(_run_code(event))
        return list(run.planned_car_codes) if run is not None else []

    def _apply_classified(self, event: Any) -> None:
        spots = event.payload.get("spots")
        if not isinstance(spots, list):
            return
        for raw in spots:
            if not isinstance(raw, dict):
                continue
            car_code = raw.get("car_code")
            track_code = raw.get("track_code")
            if not isinstance(car_code, str) or not isinstance(track_code, str):
                continue
            if self.car_state.get(car_code) in {"STANDING", "RESERVED", "ASSEMBLED", "DEPARTED"}:
                continue  # repeated classification only spots newly placed cars
            stack = self.tracks.setdefault(track_code, [])
            self.track_peak.setdefault(track_code, 0)
            if car_code not in stack:
                stack.append(car_code)
                self.track_peak[track_code] = max(self.track_peak[track_code], len(stack))
            self.car_state[car_code] = "STANDING"
            self.car_location[car_code] = track_code

    def _apply_move(self, raw: dict[str, Any]) -> None:
        verb = str(raw.get("verb", "")).upper()
        car_code = str(raw.get("car_code", ""))
        source_code = str(raw.get("source_code", ""))
        target_code = str(raw.get("target_code", ""))
        if verb == "BUFFER":
            stack = self.tracks.get(source_code)
            if stack is not None and car_code in stack:
                stack.remove(car_code)
            bay = self.bays.setdefault(target_code, [])
            self.bay_peak.setdefault(target_code, 0)
            if car_code not in bay:
                bay.append(car_code)
                self.bay_peak[target_code] = max(self.bay_peak[target_code], len(bay))
            self.car_location[car_code] = target_code
        elif verb == "RETURN":
            bay = self.bays.get(source_code)
            if bay is not None and car_code in bay:
                bay.remove(car_code)
            track = self.tracks.setdefault(target_code, [])
            self.track_peak.setdefault(target_code, 0)
            if car_code not in track:
                track.append(car_code)
                self.track_peak[target_code] = max(self.track_peak[target_code], len(track))
            self.car_location[car_code] = target_code
        elif verb == "PULL":
            stack = self.tracks.get(source_code)
            if stack is not None and car_code in stack:
                stack.remove(car_code)
            self.car_state[car_code] = "ASSEMBLED"
            self.car_location[car_code] = target_code


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


def _build_metrics(
    workspace: Any,
    shift: Any,
    shift_events: list[Any],
    context_events: list[Any],
    window_events: list[Any],
    start: int,
    end: int,
) -> dict[str, Any]:
    return {
        "shift": {
            "opened_at": shift.opened_at,
            "closed_at": shift.closed_at,
            "state": str(shift.state),
            "duration_seconds": _safe_duration(shift.opened_at, shift.closed_at),
        },
        "durations_seconds": _duration_summary(workspace, shift_events, window_events, start, end),
        "pull_completion": _pull_summary(workspace, window_events),
        "rework": _retry_summary(window_events),
    }


def _safe_duration(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    value = duration_seconds(start, end)
    # Negative spans come from independent timestamp sources (e.g. a supplied
    # shift open time against the wall clock) and are unknown, not negative.
    return value if value >= 0 else None


def _avg(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _stat_block(values: list[int]) -> dict[str, Any]:
    return {
        "sample_size": len(values),
        "average_seconds": _avg(values),
        "max_seconds": max(values) if values else None,
    }


def _events_by_entity(
    events: list[Any], kind: str, key_fn: CodeExtractor
) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for event in events:
        if str(event.kind) != kind:
            continue
        key = key_fn(event)
        if key is not None:
            result.setdefault(key, []).append(event)
    return result


def _duration_summary(
    workspace: Any,
    shift_events: list[Any],
    window_events: list[Any],
    start: int,
    end: int,
) -> dict[str, Any]:
    receive_to_classify: list[int] = []
    classify_to_assemble: list[int] = []
    plan_to_complete: list[int] = []
    plan_to_depart: list[int] = []

    received = _events_by_entity(shift_events, EventKind.TRAIN_RECEIVED.value, _intake_code)
    classified = _events_by_entity(shift_events, EventKind.TRAIN_CLASSIFIED.value, _intake_code)
    planned = _events_by_entity(shift_events, EventKind.PULL_PLANNED.value, _run_code)
    run_completed = _events_by_entity(
        shift_events, EventKind.PULL_RUN_COMPLETED.value, _run_code
    )
    created = _events_by_entity(shift_events, EventKind.TRAIN_CREATED.value, _outbound_code)
    departed = _events_by_entity(shift_events, EventKind.TRAIN_DEPARTED.value, _outbound_code)

    # Intakes fully classified inside the window. Partial attempts stay out of
    # the average (their duration is unknown, not zero) and show in details.
    for event in window_events:
        if str(event.kind) != EventKind.TRAIN_CLASSIFIED.value:
            continue
        unplaced = event.payload.get("unplaced")
        if not isinstance(unplaced, list) or unplaced:
            continue
        code = _intake_code(event)
        intake = workspace.intakes.get(code) if code else None
        first_received = received.get(code or "", [None])[0]
        start_at = _event_at(first_received)
        end_at = (intake.placed_at if intake is not None and intake.placed_at else None) or event.at
        value = _safe_duration(start_at, end_at)
        if value is not None and value >= 0:
            receive_to_classify.append(value)

    # Cars pulled (assembled) inside the window.
    car_classified_at = _car_classified_times(shift_events)
    for event in window_events:
        if str(event.kind) not in _MOVE_EVENT_KINDS:
            continue
        for raw in _event_steps(workspace, event):
            if str(raw.get("verb", "")).upper() != "PULL":
                continue
            code = str(raw.get("car_code", ""))
            value = _safe_duration(car_classified_at.get(code), event.at)
            if value is not None and value >= 0:
                classify_to_assemble.append(value)

    # Runs completed inside the window.
    for event in window_events:
        if str(event.kind) != EventKind.PULL_RUN_COMPLETED.value:
            continue
        code = _run_code(event)
        run = workspace.runs.get(code) if code else None
        first_planned = planned.get(code or "", [None])[0]
        planned_at = _event_at(first_planned) or (
            run.created_at if run is not None and run.created_at else None
        )
        value = _safe_duration(planned_at, event.at)
        if value is not None and value >= 0:
            plan_to_complete.append(value)
        if run is not None:
            depart_events = departed.get(run.outbound_code, [])
            depart_in_window = [
                item for item in depart_events if _in_window(item.sequence, start, end)
            ]
            depart_at: str | None = None
            if depart_in_window:
                raw_at = depart_in_window[-1].payload.get("departed_at")
                depart_at = (
                    str(raw_at) if isinstance(raw_at, str) else depart_in_window[-1].at
                )
            dep_value = _safe_duration(planned_at, depart_at)
            if dep_value is not None and dep_value >= 0:
                plan_to_depart.append(dep_value)

    return {
        "receive_to_classify": _stat_block(receive_to_classify),
        "classify_to_assemble": _stat_block(classify_to_assemble),
        "plan_to_complete": _stat_block(plan_to_complete),
        "plan_to_depart": _stat_block(plan_to_depart),
    }


def _car_classified_times(events: list[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for event in events:
        if str(event.kind) != EventKind.TRAIN_CLASSIFIED.value:
            continue
        spots = event.payload.get("spots")
        if not isinstance(spots, list):
            continue
        for raw in spots:
            if isinstance(raw, dict) and isinstance(raw.get("car_code"), str):
                # First classification wins; a repeated attempt must not move
                # the car's original classified-at time.
                result.setdefault(raw["car_code"], event.at)
    return result


def _pull_summary(workspace: Any, window_events: list[Any]) -> dict[str, Any]:
    planned_codes: set[str] = set()
    started_codes: set[str] = set()
    completed_codes: set[str] = set()
    for event in window_events:
        kind = str(event.kind)
        if kind == EventKind.PULL_PLANNED.value:
            code = _run_code(event)
            if code:
                planned_codes.add(code)
        elif kind == EventKind.PULL_RUN_STARTED.value:
            code = _run_code(event)
            if code:
                started_codes.add(code)
        elif kind == EventKind.PULL_RUN_COMPLETED.value:
            code = _run_code(event)
            if code:
                completed_codes.add(code)

    failed_in_window = {
        code for code in planned_codes if _run_state(workspace, code) == RunState.FAILED.value
    }
    completed_of_planned = completed_codes & planned_codes
    completion_rate = (
        round(len(completed_of_planned) / len(planned_codes) * 100, 2) if planned_codes else None
    )

    buffer_moves = return_moves = pull_moves = 0
    run_buffer_counts = {code: 0 for code in completed_codes}
    for event in window_events:
        if str(event.kind) not in _MOVE_EVENT_KINDS:
            continue
        steps = _event_steps(workspace, event)
        code = _run_code(event)
        for raw in steps:
            verb = str(raw.get("verb", "")).upper()
            if verb == "BUFFER":
                buffer_moves += 1
                if code in run_buffer_counts:
                    run_buffer_counts[code] += 1
            elif verb == "RETURN":
                return_moves += 1
            elif verb == "PULL":
                pull_moves += 1

    completed_run_buffers = [
        run_buffer_counts[code] for code in completed_codes if code in planned_codes
    ]

    return {
        "planned_runs": len(planned_codes),
        "started_runs": len(started_codes | completed_codes),
        "completed_runs": len(completed_of_planned),
        "failed_runs": len(failed_in_window),
        "incomplete_runs": max(
            0, len(planned_codes) - len(completed_of_planned) - len(failed_in_window)
        ),
        "completion_rate_percent": completion_rate,
        "executed_moves": {
            "buffer": buffer_moves,
            "pull": pull_moves,
            "return": return_moves,
            "total": buffer_moves + pull_moves + return_moves,
        },
        "pulled_cars": pull_moves,
        "average_buffers_per_completed_run": _avg(completed_run_buffers),
        "average_buffers_per_pull": round(buffer_moves / pull_moves, 2) if pull_moves else None,
    }


def _run_state(workspace: Any, code: str) -> str | None:
    run = workspace.runs.get(code)
    return None if run is None else str(run.state)


def _retry_summary(window_events: list[Any]) -> dict[str, Any]:
    closure_blocks = sum(
        1 for event in window_events if str(event.kind) == EventKind.CLOSURE_BLOCKED.value
    )
    classify_counts = _attempt_counts(window_events, EventKind.TRAIN_CLASSIFIED.value, _intake_code)
    advance_counts = _attempt_counts(window_events, EventKind.PULL_RUN_ADVANCED.value, _run_code)
    completion_counts = _attempt_counts(
        window_events, EventKind.PULL_RUN_COMPLETED.value, _run_code
    )
    received_counts = _attempt_counts(window_events, EventKind.TRAIN_RECEIVED.value, _intake_code)
    departed_counts = _attempt_counts(window_events, EventKind.TRAIN_DEPARTED.value, _outbound_code)

    classification_reattempts = sum(max(0, count - 1) for count in classify_counts.values())

    # An execution batch is one crew action: either an ADVANCED event or the
    # final COMPLETED event for a run. Beyond the first batch every extra
    # advance/completion is a repeated crew attempt.
    batch_counts: dict[str, int] = {}
    for code, count in advance_counts.items():
        batch_counts[code] = batch_counts.get(code, 0) + count
    for code, count in completion_counts.items():
        batch_counts[code] = batch_counts.get(code, 0) + count
    multi_batch_runs = sum(1 for count in batch_counts.values() if count > 1)
    extra_batch_events = sum(max(0, count - 1) for count in batch_counts.values())

    duplicate_lifecycle = sum(max(0, count - 1) for count in received_counts.values()) + sum(
        max(0, count - 1) for count in departed_counts.values()
    )
    total = closure_blocks + classification_reattempts + extra_batch_events + duplicate_lifecycle

    return {
        "closure_blocked_attempts": closure_blocks,
        "classification_reattempts": classification_reattempts,
        "pull_run_multi_batch_count": multi_batch_runs,
        "pull_run_extra_batch_events": extra_batch_events,
        "duplicate_lifecycle_events": duplicate_lifecycle,
        "total_repeated_attempts": total,
        "classification_attempts": [
            {"intake_code": code, "attempts": classify_counts[code]}
            for code in sorted(classify_counts)
        ],
        "execution_batches": [
            {
                "run_code": code,
                "advance_events": advance_counts.get(code, 0),
                "completed_events": completion_counts.get(code, 0),
                "batches": batch_counts[code],
            }
            for code in sorted(batch_counts)
        ],
    }


def _attempt_counts(events: list[Any], kind: str, key_fn: CodeExtractor) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if str(event.kind) != kind:
            continue
        key = key_fn(event)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Detail rows
# ---------------------------------------------------------------------------


def _intake_details(workspace: Any, shift_events: list[Any], start: int, end: int) -> list[dict[str, Any]]:
    received = _events_by_entity(shift_events, EventKind.TRAIN_RECEIVED.value, _intake_code)
    classified = _events_by_entity(shift_events, EventKind.TRAIN_CLASSIFIED.value, _intake_code)
    rows: list[dict[str, Any]] = []
    codes = sorted(set(received) | set(classified))
    for code in codes:
        recv_in = [event for event in received.get(code, []) if _in_window(event.sequence, start, end)]
        class_in = [event for event in classified.get(code, []) if _in_window(event.sequence, start, end)]
        if not recv_in and not class_in:
            continue
        first_recv = received.get(code, [None])[0]
        last_class = class_in[-1] if class_in else None
        unplaced: list[str] = []
        spotted_in_window = 0
        if last_class is not None:
            raw_unplaced = last_class.payload.get("unplaced")
            if isinstance(raw_unplaced, list):
                unplaced = [str(item) for item in raw_unplaced]
        for event in class_in:
            spotted = event.payload.get("spotted")
            if isinstance(spotted, int):
                spotted_in_window += spotted
        intake = workspace.intakes.get(code)
        classified_at: str | None = None
        if intake is not None and intake.placed_at and not unplaced:
            classified_at = intake.placed_at
        elif last_class is not None and not unplaced:
            classified_at = last_class.at
        state = (
            str(last_class.payload.get("state"))
            if last_class is not None and last_class.payload.get("state")
            else (str(intake.state) if intake is not None else None)
        )
        rows.append(
            {
                "intake_code": code,
                "received_at": _event_at(first_recv),
                "arrival_at": intake.arrival_at if intake is not None else None,
                "classified_at": classified_at,
                "classification_attempts": len(class_in),
                "spotted_in_window": spotted_in_window,
                "unplaced_after_last_attempt": unplaced,
                "state": state,
                "receive_to_classify_seconds": _safe_duration(
                    _event_at(first_recv), classified_at
                ),
                "event_sequences": sorted(
                    {event.sequence for event in recv_in + class_in}
                ),
            }
        )
    return rows


def _car_event_codes(window_events: list[Any]) -> set[str]:
    codes: set[str] = set()
    for event in window_events:
        kind = str(event.kind)
        if kind == EventKind.TRAIN_RECEIVED.value:
            codes.update(_event_str_list(event, "car_codes"))
        elif kind == EventKind.TRAIN_CLASSIFIED.value:
            spots = event.payload.get("spots")
            if isinstance(spots, list):
                for raw in spots:
                    if isinstance(raw, dict) and isinstance(raw.get("car_code"), str):
                        codes.add(raw["car_code"])
        elif kind in _MOVE_EVENT_KINDS:
            move_steps = event.payload.get("executed_steps")
            if not isinstance(move_steps, list):
                continue
            for raw in move_steps:
                if isinstance(raw, dict) and isinstance(raw.get("car_code"), str):
                    codes.add(raw["car_code"])
        elif kind == EventKind.TRAIN_DEPARTED.value:
            codes.update(_event_str_list(event, "car_codes"))
    return codes


def _car_details(
    context_events: list[Any],
    window_events: list[Any],
    start: int,
    end: int,
    baseline: _ReplayState,
    end_state: _ReplayState,
) -> list[dict[str, Any]]:
    timelines = _car_timelines(context_events)
    touched = _car_event_codes(window_events)
    rows: list[dict[str, Any]] = []
    for code in sorted(touched):
        times = timelines.get(code, {})
        classified_at = times.get("classified_at")
        assembled_at = times.get("assembled_at")
        departed_at = times.get("departed_at")
        rows.append(
            {
                "car_code": code,
                "received_at": times.get("received_at"),
                "classified_at": classified_at,
                "assembled_at": assembled_at,
                "departed_at": departed_at,
                "classify_to_assemble_seconds": _safe_duration(classified_at, assembled_at),
                "assemble_to_depart_seconds": _safe_duration(assembled_at, departed_at),
                "state_before_window": baseline.car_state.get(code),
                "state_after_window": end_state.car_state.get(code),
            }
        )
    return rows


def _car_timelines(events: list[Any]) -> dict[str, dict[str, str]]:
    timelines: dict[str, dict[str, str]] = {}

    def ensure(code: str) -> dict[str, str]:
        return timelines.setdefault(code, {})

    for event in events:
        kind = str(event.kind)
        if kind == EventKind.TRAIN_RECEIVED.value:
            for code in _event_str_list(event, "car_codes"):
                ensure(code).setdefault("received_at", event.at)
        elif kind == EventKind.TRAIN_CLASSIFIED.value:
            spots = event.payload.get("spots")
            if isinstance(spots, list):
                for raw in spots:
                    if isinstance(raw, dict) and isinstance(raw.get("car_code"), str):
                        ensure(raw["car_code"]).setdefault("classified_at", event.at)
        elif kind in _MOVE_EVENT_KINDS:
            steps = event.payload.get("executed_steps")
            if isinstance(steps, list):
                for raw in steps:
                    if isinstance(raw, dict) and str(raw.get("verb", "")).upper() == "PULL":
                        code = raw.get("car_code")
                        if isinstance(code, str):
                            ensure(code)["assembled_at"] = event.at
        elif kind == EventKind.TRAIN_DEPARTED.value:
            for code in _event_str_list(event, "car_codes"):
                ensure(code)["departed_at"] = event.at
    return timelines


def _run_details(workspace: Any, shift_events: list[Any], start: int, end: int) -> list[dict[str, Any]]:
    planned = _events_by_entity(shift_events, EventKind.PULL_PLANNED.value, _run_code)
    started = _events_by_entity(shift_events, EventKind.PULL_RUN_STARTED.value, _run_code)
    advanced = _events_by_entity(shift_events, EventKind.PULL_RUN_ADVANCED.value, _run_code)
    completed = _events_by_entity(shift_events, EventKind.PULL_RUN_COMPLETED.value, _run_code)
    rows: list[dict[str, Any]] = []
    for code in sorted(set(planned) | set(started) | set(advanced) | set(completed)):
        plan_in = [event for event in planned.get(code, []) if _in_window(event.sequence, start, end)]
        start_in = [event for event in started.get(code, []) if _in_window(event.sequence, start, end)]
        advance_in = [
            event for event in advanced.get(code, []) if _in_window(event.sequence, start, end)
        ]
        complete_in = [
            event for event in completed.get(code, []) if _in_window(event.sequence, start, end)
        ]
        if not (plan_in or start_in or advance_in or complete_in):
            continue
        run = workspace.runs.get(code)
        first_plan = planned.get(code, [None])[0]
        first_start = started.get(code, [None])[0]
        last_complete = completed.get(code, [None])[-1]
        planned_at = _event_at(first_plan) or (
            run.created_at if run is not None and run.created_at else None
        )
        started_at = _event_at(first_start) or (run.started_at if run is not None else None)
        completed_at = _event_at(last_complete) or (run.completed_at if run is not None else None)

        executed_steps = buffer_count = pull_count = return_count = 0
        for event in advance_in + complete_in:
            for raw in _event_steps(workspace, event):
                executed_steps += 1
                verb = str(raw.get("verb", "")).upper()
                if verb == "BUFFER":
                    buffer_count += 1
                elif verb == "PULL":
                    pull_count += 1
                elif verb == "RETURN":
                    return_count += 1

        planned_steps: int | None = None
        if first_plan is not None and isinstance(first_plan.payload.get("steps"), int):
            planned_steps = int(first_plan.payload["steps"])
        if planned_steps is None and run is not None:
            planned_steps = len(run.steps)

        state = str(run.state) if run is not None else None
        if complete_in or state == RunState.COMPLETED.value:
            outcome = "COMPLETED"
        elif state == RunState.FAILED.value:
            outcome = "FAILED"
        elif start_in or advance_in:
            outcome = "IN_PROGRESS"
        else:
            outcome = "QUEUED"

        execution_batches = len(advance_in) + len(complete_in)

        rows.append(
            {
                "run_code": code,
                "outbound_code": run.outbound_code if run is not None else None,
                "planned_at": planned_at,
                "started_at": started_at,
                "completed_at": completed_at,
                "state": state,
                "outcome": outcome,
                "planned_steps": planned_steps,
                "executed_steps_in_window": executed_steps,
                "execution_batches": execution_batches,
                "buffer_moves": buffer_count,
                "pull_moves": pull_count,
                "return_moves": return_count,
                "advance_events_in_window": len(advance_in),
                "multi_batch": execution_batches > 1,
                "plan_to_start_seconds": _safe_duration(planned_at, started_at),
                "start_to_complete_seconds": _safe_duration(started_at, completed_at),
                "plan_to_complete_seconds": _safe_duration(planned_at, completed_at),
                "event_sequences": sorted(
                    {
                        event.sequence
                        for event in plan_in + start_in + advance_in + complete_in
                    }
                ),
            }
        )
    return rows


def _outbound_details(
    workspace: Any, shift_events: list[Any], start: int, end: int
) -> list[dict[str, Any]]:
    created = _events_by_entity(shift_events, EventKind.TRAIN_CREATED.value, _outbound_code)
    departed = _events_by_entity(shift_events, EventKind.TRAIN_DEPARTED.value, _outbound_code)
    rows: list[dict[str, Any]] = []
    for code in sorted(set(created) | set(departed)):
        create_in = [event for event in created.get(code, []) if _in_window(event.sequence, start, end)]
        depart_in = [
            event for event in departed.get(code, []) if _in_window(event.sequence, start, end)
        ]
        if not create_in and not depart_in:
            continue
        outbound = workspace.outbounds.get(code)
        first_created = created.get(code, [None])[0]
        last_departed = departed.get(code, [None])[-1]
        created_at = _event_at(first_created) or (
            outbound.created_at if outbound is not None and outbound.created_at else None
        )
        departed_at: str | None = None
        if last_departed is not None:
            raw = last_departed.payload.get("departed_at")
            departed_at = str(raw) if isinstance(raw, str) else last_departed.at
        elif outbound is not None and outbound.departed_at and depart_in:
            departed_at = outbound.departed_at
        planned_codes: list[str] = []
        if first_created is not None:
            planned_codes = _event_str_list(first_created, "car_codes")
        if not planned_codes and outbound is not None:
            planned_codes = list(outbound.planned_car_codes)
        rows.append(
            {
                "outbound_code": code,
                "destination": outbound.destination if outbound is not None else None,
                "created_at": created_at,
                "departed_at": departed_at,
                "state": str(outbound.state) if outbound is not None else None,
                "planned_cars": len(planned_codes),
                "assembled_cars": len(outbound.assembled_car_codes) if outbound is not None else None,
                "run_codes": list(outbound.run_codes) if outbound is not None else [],
                "plan_to_depart_seconds": _safe_duration(created_at, departed_at),
                "event_sequences": sorted(
                    {event.sequence for event in create_in + depart_in}
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Occupancy reports
# ---------------------------------------------------------------------------


def _track_reports(
    workspace: Any, baseline: _ReplayState, end_state: _ReplayState, window_events: list[Any]
) -> list[dict[str, Any]]:
    moved_in: dict[str, int] = {}
    moved_out: dict[str, int] = {}
    for event in window_events:
        if str(event.kind) not in _MOVE_EVENT_KINDS:
            continue
        for raw in _event_steps(workspace, event):
            verb = str(raw.get("verb", "")).upper()
            if verb == "RETURN":
                key = raw.get("target_code")
                if isinstance(key, str):
                    moved_in[key] = moved_in.get(key, 0) + 1
            elif verb in {"BUFFER", "PULL"}:
                key = raw.get("source_code")
                if isinstance(key, str):
                    moved_out[key] = moved_out.get(key, 0) + 1

    rows: list[dict[str, Any]] = []
    for code in sorted(set(baseline.tracks) | set(end_state.tracks)):
        before = len(baseline.tracks.get(code, []))
        after = len(end_state.tracks.get(code, []))
        peak = max(end_state.track_peak.get(code, 0), after)
        rows.append(
            {
                "track_code": code,
                "cars_before_window": before,
                "cars_after_window": after,
                "net_change": after - before,
                "cars_moved_in": moved_in.get(code, 0),
                "cars_moved_out": moved_out.get(code, 0),
                "peak_cars_in_window": peak,
            }
        )
    return rows


def _bay_reports(
    workspace: Any, baseline: _ReplayState, end_state: _ReplayState, window_events: list[Any]
) -> list[dict[str, Any]]:
    buffered = returned = 0
    per_bay_in: dict[str, int] = {}
    per_bay_out: dict[str, int] = {}
    for event in window_events:
        if str(event.kind) not in _MOVE_EVENT_KINDS:
            continue
        for raw in _event_steps(workspace, event):
            verb = str(raw.get("verb", "")).upper()
            if verb == "BUFFER":
                buffered += 1
                key = raw.get("target_code")
                if isinstance(key, str):
                    per_bay_in[key] = per_bay_in.get(key, 0) + 1
            elif verb == "RETURN":
                returned += 1
                key = raw.get("source_code")
                if isinstance(key, str):
                    per_bay_out[key] = per_bay_out.get(key, 0) + 1

    rows: list[dict[str, Any]] = []
    for code in sorted(set(baseline.bays) | set(end_state.bays)):
        before = len(baseline.bays.get(code, []))
        after = len(end_state.bays.get(code, []))
        peak = max(end_state.bay_peak.get(code, 0), after)
        rows.append(
            {
                "bay_code": code,
                "cars_before_window": before,
                "cars_after_window": after,
                "net_change": after - before,
                "buffered_in_window": per_bay_in.get(code, 0),
                "returned_in_window": per_bay_out.get(code, 0),
                "peak_cars_in_window": peak,
            }
        )
    return rows


def _destination_reports(
    baseline: _ReplayState, end_state: _ReplayState, catalog: _EntityCatalog
) -> list[dict[str, Any]]:
    def bucket(state: _ReplayState) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for code, car_state in state.car_state.items():
            dest = catalog.destination_of(code)
            row = result.setdefault(
                dest,
                {
                    "received": 0,
                    "standing": 0,
                    "reserved": 0,
                    "assembled": 0,
                    "departed": 0,
                    "on_hand": 0,
                },
            )
            key = car_state.lower()
            if key in row:
                row[key] += 1
            # Physical occupancy excludes cars that have already left the yard.
            if key != "departed":
                row["on_hand"] += 1
        return result

    before = bucket(baseline)
    after = bucket(end_state)
    rows: list[dict[str, Any]] = []
    state_keys = ("received", "standing", "reserved", "assembled", "departed")
    for dest in sorted(set(before) | set(after)):
        b = before.get(dest, {})
        a = after.get(dest, {})
        departed_delta = a.get("departed", 0) - b.get("departed", 0)
        rows.append(
            {
                "destination": dest,
                "on_hand_before_window": b.get("on_hand", 0),
                "on_hand_after_window": a.get("on_hand", 0),
                "net_change": a.get("on_hand", 0) - b.get("on_hand", 0),
                "departed_in_window": departed_delta,
                "state_before": {key: b.get(key, 0) for key in state_keys},
                "state_after": {key: a.get(key, 0) for key in state_keys},
            }
        )
    return rows


__all__ = ["shift_work_metrics"]
