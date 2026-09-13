"""Event audit and reconciliation between the event journal and the yard snapshot.

This module is strictly read-only. It correlates the append-only event journal
(``events.jsonl``) with the events embedded in the current state snapshot
(``yard-state.json``) along four axes: event sequence, shift, timestamp, and
object. It surfaces duplicate events, sequence gaps, order inversions, changes
that only exist on one side, and the trace from each event to the current
state of the object it touched.

The auditor never writes, never appends "fix-up" events, and never raises on a
damaged input: malformed journal lines, unknown event kinds, and events whose
object cannot be resolved are retained as manual review items instead of
blocking callers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..domain.timeutil import parse_iso

# Issue severities. "review" items need a human but do not prove divergence;
# "error" items prove the journal and snapshot do not describe the same work.
SEVERITY_ERROR = "error"
SEVERITY_REVIEW = "review"
SEVERITY_INFO = "info"

# Trace verdicts for an event's object.
TRACE_CONSISTENT = "CONSISTENT"
TRACE_INCONSISTENT = "INCONSISTENT"
TRACE_OBJECT_MISSING = "OBJECT_MISSING"
TRACE_NO_OBJECT = "NO_OBJECT"
TRACE_REVIEW = "REVIEW"

KNOWN_KINDS = {
    "SHIFT_OPENED",
    "TRAIN_RECEIVED",
    "TRAIN_CLASSIFIED",
    "TRAIN_CREATED",
    "PULL_PLANNED",
    "PULL_RUN_STARTED",
    "PULL_RUN_ADVANCED",
    "PULL_RUN_COMPLETED",
    "TRAIN_DEPARTED",
    "SHIFT_CLOSED",
    "CLOSURE_BLOCKED",
    "YARD_VIEWED",
}

_KIND_OBJECT_TYPES = {
    "SHIFT_OPENED": "SHIFT",
    "SHIFT_CLOSED": "SHIFT",
    "CLOSURE_BLOCKED": "SHIFT",
    "TRAIN_RECEIVED": "INTAKE",
    "TRAIN_CLASSIFIED": "INTAKE",
    "TRAIN_CREATED": "OUTBOUND",
    "TRAIN_DEPARTED": "OUTBOUND",
    "PULL_PLANNED": "PULL_RUN",
    "PULL_RUN_STARTED": "PULL_RUN",
    "PULL_RUN_ADVANCED": "PULL_RUN",
    "PULL_RUN_COMPLETED": "PULL_RUN",
    "YARD_VIEWED": None,
}

_CODE_TOKEN = r"[A-Za-z0-9_.-]+"
_INTAKE_RE = re.compile(rf"intake\s+({_CODE_TOKEN})", re.IGNORECASE)
_OUTBOUND_RE = re.compile(rf"outbound\s+({_CODE_TOKEN})", re.IGNORECASE)
_RUN_RE = re.compile(rf"pull[ _]run\s+({_CODE_TOKEN})", re.IGNORECASE)


@dataclass(slots=True)
class EventRecord:
    """One normalized event as found on one side."""

    side: str  # "journal" or "state"
    ordinal: int  # physical order on that side (line number / list index)
    sequence: int | None
    at: str
    shift_code: str
    kind: str
    message: str
    payload: dict[str, Any]
    raw: str = ""
    malformed: bool = False
    malformed_reason: str | None = None

    def identity_key(self) -> str:
        return json.dumps(
            {
                "sequence": self.sequence,
                "at": self.at,
                "shift_code": self.shift_code,
                "kind": self.kind,
                "message": self.message,
                "payload": self.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(slots=True)
class ObjectRef:
    object_type: str | None
    object_code: str | None
    car_codes: list[str] = field(default_factory=list)
    attribution: str = "exact"  # "exact" | "message" | "none"


@dataclass(slots=True)
class _SnapshotIndex:
    present: bool = False
    next_event_sequence: int | None = None
    shifts: dict[str, dict[str, Any]] = field(default_factory=dict)
    intakes: dict[str, dict[str, Any]] = field(default_factory=dict)
    outbounds: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    cars: dict[str, dict[str, Any]] = field(default_factory=dict)
    tracks: dict[str, list[str]] = field(default_factory=dict)
    bays: dict[str, list[str]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


def index_snapshot(state_raw: dict[str, Any] | None) -> _SnapshotIndex:
    """Build a tolerant lookup over the state snapshot.

    Uses raw serialized shapes so an unexpected enum value or a missing field
    degrades to a review item instead of crashing the audit.
    """

    index = _SnapshotIndex()
    if not isinstance(state_raw, dict):
        return index
    index.present = True
    try:
        index.next_event_sequence = int(state_raw.get("next_event_sequence"))
    except (TypeError, ValueError):
        index.next_event_sequence = None
    index.shifts = {str(item.get("code")): dict(item) for item in state_raw.get("shifts", []) if isinstance(item, dict)}
    index.intakes = {str(item.get("code")): dict(item) for item in state_raw.get("intakes", []) if isinstance(item, dict)}
    index.outbounds = {
        str(item.get("code")): dict(item) for item in state_raw.get("outbounds", []) if isinstance(item, dict)
    }
    index.runs = {
        str(item.get("code")): dict(item) for item in state_raw.get("pull_runs", []) if isinstance(item, dict)
    }
    index.cars = {str(item.get("code")): dict(item) for item in state_raw.get("cars", []) if isinstance(item, dict)}
    index.tracks = {
        str(item.get("code")): [str(code) for code in item.get("stack", [])]
        for item in state_raw.get("tracks", [])
        if isinstance(item, dict)
    }
    index.bays = {
        str(item.get("code")): [str(code) for code in item.get("stack", [])]
        for item in state_raw.get("buffer_bays", [])
        if isinstance(item, dict)
    }
    index.events = [dict(item) for item in state_raw.get("events", []) if isinstance(item, dict)]
    return index


def _as_event_record(item: dict[str, Any], side: str, ordinal: int) -> EventRecord:
    payload = item.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    sequence = item.get("sequence")
    try:
        sequence_value = int(sequence)
    except (TypeError, ValueError):
        sequence_value = None
    return EventRecord(
        side=side,
        ordinal=ordinal,
        sequence=sequence_value,
        at=str(item.get("at", "")),
        shift_code=str(item.get("shift_code", "")),
        kind=str(item.get("kind", "")).upper(),
        message=str(item.get("message", "")),
        payload=dict(payload),
        raw=json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def normalize_journal(lines: Iterable[Any]) -> list[EventRecord]:
    records: list[EventRecord] = []
    for line in lines:
        if line.ok:
            records.append(_as_event_record(line.data, "journal", line.line_no))
        else:
            records.append(
                EventRecord(
                    side="journal",
                    ordinal=line.line_no,
                    sequence=None,
                    at="",
                    shift_code="",
                    kind="",
                    message="",
                    payload={},
                    raw=line.raw,
                    malformed=True,
                    malformed_reason=line.error,
                )
            )
    return records


def normalize_state_events(events: Iterable[dict[str, Any]]) -> list[EventRecord]:
    return [_as_event_record(item, "state", index) for index, item in enumerate(events, start=1)]


def _message_code(pattern: re.Pattern[str], message: str) -> str | None:
    match = pattern.search(message)
    if match is None:
        return None
    return match.group(1)


def resolve_object(record: EventRecord, snapshot: _SnapshotIndex) -> ObjectRef:
    """Resolve the yard object a single event refers to.

    Preference order: the structured event field (shift code), then structured
    payload links, then the human-readable message. Anything that cannot be
    resolved comes back unattributed so it can be held for manual review.
    """

    kind = record.kind
    payload = record.payload
    object_type = _KIND_OBJECT_TYPES.get(kind)
    if kind not in KNOWN_KINDS:
        return ObjectRef(object_type=None, object_code=None, attribution="none")
    if object_type is None:
        return ObjectRef(object_type=None, object_code=None)

    object_code: str | None = None
    attribution = "exact"
    if object_type == "SHIFT":
        object_code = record.shift_code or None
    elif object_type == "INTAKE":
        object_code = _message_code(_INTAKE_RE, record.message)
        attribution = "message" if object_code else "none"
    elif object_type == "OUTBOUND":
        object_code = _message_code(_OUTBOUND_RE, record.message)
        attribution = "message" if object_code else "none"
    elif object_type == "PULL_RUN":
        direct = _message_code(_RUN_RE, record.message)
        outbound_code = _message_code(_OUTBOUND_RE, record.message)
        if direct is not None:
            object_code = direct
            attribution = "message"
        elif outbound_code is not None:
            # Completion messages name the run; started/advanced messages name
            # only the run context, so resolve the run through its outbound
            # link in the snapshot.
            linked = next(
                (
                    code
                    for code, run in snapshot.runs.items()
                    if str(run.get("outbound_code", "")) == outbound_code
                ),
                None,
            )
            object_code = linked
            attribution = "linked" if linked else "none"

    cars = _payload_car_codes(kind, payload)
    if not cars:
        cars = _snapshot_car_codes(kind, object_type, object_code, snapshot)
    return ObjectRef(object_type=object_type, object_code=object_code, car_codes=cars, attribution=attribution)


def _payload_car_codes(kind: str, payload: dict[str, Any]) -> list[str]:
    cars: list[str] = []
    if kind == "TRAIN_CLASSIFIED":
        cars.extend(str(code) for code in payload.get("unplaced", []) if code is not None)
        for spot in payload.get("spots", []):
            if isinstance(spot, dict) and spot.get("car_code") is not None:
                cars.append(str(spot["car_code"]))
    elif kind in {"PULL_RUN_COMPLETED"}:
        cars.extend(str(code) for code in payload.get("assembled_car_codes", []) if code is not None)
    return cars


def _snapshot_car_codes(kind: str, object_type: str | None, object_code: str | None, snapshot: _SnapshotIndex) -> list[str]:
    """Enrich "view by car" using the snapshot side when payloads omit cars."""

    if object_code is None:
        return []
    if object_type == "INTAKE" and kind in {"TRAIN_RECEIVED", "TRAIN_CLASSIFIED"}:
        intake = snapshot.intakes.get(object_code)
        if intake is not None:
            return [str(code) for code in intake.get("consist", [])]
    if object_type == "OUTBOUND":
        outbound = snapshot.outbounds.get(object_code)
        if outbound is not None:
            codes = list(outbound.get("assembled_car_codes", [])) or list(outbound.get("planned_car_codes", []))
            return [str(code) for code in codes]
    if object_type == "PULL_RUN":
        run = snapshot.runs.get(object_code)
        if run is not None:
            outbound = snapshot.outbounds.get(str(run.get("outbound_code", "")))
            if outbound is not None:
                return [str(code) for code in outbound.get("planned_car_codes", [])]
    return []


def _parse_time(value: str) -> Any:
    if not value:
        return None
    try:
        return parse_iso(value)
    except (ValueError, TypeError):
        return None


def _ranges(sequences: list[int]) -> list[list[int]]:
    if not sequences:
        return []
    ordered = sorted(set(sequences))
    ranges: list[list[int]] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append([start, prev])
        start = prev = value
    ranges.append([start, prev])
    return ranges


@dataclass(slots=True)
class _IssueBuilder:
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        code: str,
        message: str,
        *,
        severity: str = SEVERITY_ERROR,
        review: bool = False,
        sequence: int | None = None,
        side: str | None = None,
        **details: Any,
    ) -> None:
        issue = {
            "code": code,
            "severity": severity,
            "review": bool(review),
            "message": message,
            "sequence": sequence,
            "side": side,
            "details": details,
        }
        self.issues.append(issue)


def _trace_object(kind: str, ref: ObjectRef, snapshot: _SnapshotIndex) -> dict[str, Any]:
    """Trace one event's object to its current state in the snapshot."""

    if ref.object_type is None:
        return {
            "object_type": None,
            "object_code": None,
            "verdict": TRACE_REVIEW if kind not in KNOWN_KINDS else TRACE_NO_OBJECT,
            "current_state": None,
            "detail": "event kind is not recognized" if kind not in KNOWN_KINDS else "event has no yard object",
        }
    if ref.object_code is None:
        return {
            "object_type": ref.object_type,
            "object_code": None,
            "verdict": TRACE_REVIEW,
            "current_state": None,
            "detail": "object code could not be derived from the event",
        }

    entity: dict[str, Any] | None = None
    if ref.object_type == "SHIFT":
        entity = snapshot.shifts.get(ref.object_code)
    elif ref.object_type == "INTAKE":
        entity = snapshot.intakes.get(ref.object_code)
    elif ref.object_type == "OUTBOUND":
        entity = snapshot.outbounds.get(ref.object_code)
    elif ref.object_type == "PULL_RUN":
        entity = snapshot.runs.get(ref.object_code)

    if entity is None:
        if not snapshot.present:
            detail = "state snapshot unavailable"
        else:
            detail = f"{ref.object_type} {ref.object_code} is absent from the current snapshot"
        return {
            "object_type": ref.object_type,
            "object_code": ref.object_code,
            "verdict": TRACE_OBJECT_MISSING,
            "current_state": None,
            "detail": detail,
        }

    current_state = str(entity.get("state", ""))
    verdict, detail = _expected_state(kind, ref.object_type, entity, current_state)
    return {
        "object_type": ref.object_type,
        "object_code": ref.object_code,
        "verdict": verdict,
        "current_state": current_state,
        "detail": detail,
    }


def _expected_state(kind: str, object_type: str, entity: dict[str, Any], current_state: str) -> tuple[str, str]:
    if object_type == "SHIFT":
        if kind == "SHIFT_CLOSED":
            if current_state == "CLOSED":
                return TRACE_CONSISTENT, "shift is closed"
            return TRACE_INCONSISTENT, f"event closed the shift but current state is {current_state}"
        return TRACE_CONSISTENT, f"shift exists and is {current_state}"

    if object_type == "INTAKE":
        if kind == "TRAIN_CLASSIFIED":
            if current_state in {"CLASSIFIED", "PARTIAL"}:
                return TRACE_CONSISTENT, f"intake classification recorded; current state is {current_state}"
            return TRACE_INCONSISTENT, f"classification event present but intake is {current_state}"
        return TRACE_CONSISTENT, f"intake exists and is {current_state}"

    if object_type == "OUTBOUND":
        if kind == "TRAIN_DEPARTED":
            if current_state == "DEPARTED" and entity.get("departed_at"):
                return TRACE_CONSISTENT, "outbound train is departed"
            return TRACE_INCONSISTENT, f"departure event present but outbound is {current_state}"
        return TRACE_CONSISTENT, f"outbound exists and is {current_state}"

    if object_type == "PULL_RUN":
        if kind == "PULL_RUN_STARTED":
            if current_state in {"RUNNING", "COMPLETED", "FAILED"}:
                return TRACE_CONSISTENT, f"pull run progressed past QUEUED; current state is {current_state}"
            return TRACE_INCONSISTENT, "start event present but pull run is still QUEUED"
        if kind == "PULL_RUN_ADVANCED":
            if int(entity.get("current_step", 0) or 0) > 0:
                return TRACE_CONSISTENT, f"pull run advanced to step {entity.get('current_step')}"
            return TRACE_INCONSISTENT, "advance event present but current_step is still 0"
        if kind == "PULL_RUN_COMPLETED":
            steps = entity.get("steps", [])
            total = len(steps) if isinstance(steps, list) else 0
            if current_state == "COMPLETED" and int(entity.get("current_step", 0) or 0) >= total:
                return TRACE_CONSISTENT, "pull run is complete with all steps applied"
            return TRACE_INCONSISTENT, f"completion event present but pull run is {current_state}"
        return TRACE_CONSISTENT, f"pull run exists and is {current_state}"

    return TRACE_REVIEW, f"no state rule for object type {object_type}"


def _trace_cars(ref: ObjectRef, kind: str, snapshot: _SnapshotIndex) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for car_code in ref.car_codes:
        car = snapshot.cars.get(car_code)
        if car is None:
            traces.append(
                {
                    "car_code": car_code,
                    "current_state": None,
                    "location": None,
                    "verdict": TRACE_OBJECT_MISSING,
                    "detail": "car absent from the current snapshot",
                }
            )
            continue
        state = str(car.get("state", ""))
        verdict, detail = _expected_car_state(kind, state)
        traces.append(
            {
                "car_code": car_code,
                "current_state": state,
                "location": car.get("location"),
                "verdict": verdict,
                "detail": detail,
            }
        )
    return traces


def _expected_car_state(kind: str, state: str) -> tuple[str, str]:
    progressed = {"STANDING", "RESERVED", "ASSEMBLED", "DEPARTED", "REMOVED"}
    if kind == "TRAIN_RECEIVED":
        return TRACE_CONSISTENT, f"car exists and is {state}"
    if kind == "TRAIN_CLASSIFIED":
        if state in progressed:
            return TRACE_CONSISTENT, f"classifiable car progressed; current state is {state}"
        return TRACE_INCONSISTENT, f"classification event present but car is still {state}"
    if kind == "PULL_RUN_COMPLETED":
        if state in {"ASSEMBLED", "DEPARTED"}:
            return TRACE_CONSISTENT, f"assembled car is {state}"
        return TRACE_INCONSISTENT, f"completion event present but car is {state}"
    if kind == "TRAIN_DEPARTED":
        if state == "DEPARTED":
            return TRACE_CONSISTENT, "car is departed"
        return TRACE_INCONSISTENT, f"departure event present but car is {state}"
    return TRACE_CONSISTENT, f"car exists and is {state}"


def _check_sequence_integrity(
    records: list[EventRecord],
    *,
    side: str,
    duplicate_code: str,
    gap_code: str,
    order_code: str,
    time_code: str,
    next_sequence: int | None,
    issues: _IssueBuilder,
) -> dict[str, Any]:
    valid = [record for record in records if record.sequence is not None]
    by_sequence: dict[int, list[EventRecord]] = {}
    for record in valid:
        by_sequence.setdefault(record.sequence, []).append(record)

    for sequence, group in sorted(by_sequence.items()):
        if len(group) <= 1:
            continue
        identities = {record.identity_key() for record in group}
        issues.add(
            duplicate_code,
            f"{side} event sequence {sequence} appears {len(group)} times",
            sequence=sequence,
            side=side,
            occurrences=[{"ordinal": record.ordinal, "identical": record.identity_key()} for record in group],
            occurrence_count=len(group),
            identical=len(identities) == 1,
            content_varies=len(identities) > 1,
            ordinals=[record.ordinal for record in group],
        )

    present_sequences = set(by_sequence)
    if present_sequences:
        low, high = min(present_sequences), max(present_sequences)
        upper = high
        if next_sequence is not None and side == "journal":
            # The state counter is authoritative for how many events existed.
            upper = max(high, max(1, next_sequence) - 1)
        missing = sorted(value for value in range(1, upper + 1) if value not in present_sequences)
        if missing:
            issues.add(
                gap_code,
                f"{side} is missing {len(missing)} event sequence(s)",
                side=side,
                missing_sequences=missing,
                missing_ranges=_ranges(missing),
            )

    physical = sorted(valid, key=lambda record: record.ordinal)
    previous: EventRecord | None = None
    for record in physical:
        if previous is not None and record.sequence < previous.sequence:
            issues.add(
                order_code,
                f"{side} event at position {record.ordinal} has sequence {record.sequence} after {previous.sequence}",
                sequence=record.sequence,
                side=side,
                ordinal=record.ordinal,
                previous_ordinal=previous.ordinal,
                previous_sequence=previous.sequence,
            )
        previous = record

    timed = [(record, _parse_time(record.at)) for record in physical]
    timed = [(record, parsed) for record, parsed in timed if parsed is not None]
    latest_by_sequence: dict[int, Any] = {}
    for record, parsed in timed:
        latest_by_sequence[record.sequence] = parsed
    for record, parsed in timed:
        earlier = [value for sequence, value in latest_by_sequence.items() if sequence < record.sequence]
        if earlier and parsed < max(earlier):
            previous_at = max(earlier).strftime("%Y-%m-%dT%H:%M:%SZ")
            issues.add(
                time_code,
                f"{side} timestamp {record.at} at sequence {record.sequence} precedes {previous_at}",
                sequence=record.sequence,
                side=side,
                at=record.at,
                previous_at=previous_at,
            )

    return {
        "max_sequence": max(present_sequences) if present_sequences else None,
        "min_sequence": min(present_sequences) if present_sequences else None,
        "duplicate_records": sum(len(group) - 1 for group in by_sequence.values() if len(group) > 1),
        "sequence_count": len(present_sequences),
    }


def _pair_and_diff(
    journal_records: list[EventRecord],
    state_records: list[EventRecord],
    issues: _IssueBuilder,
) -> tuple[dict[int, EventRecord], dict[int, list[EventRecord]], set[int]]:
    journal_valid = [record for record in journal_records if record.sequence is not None]
    state_valid = [record for record in state_records if record.sequence is not None]
    journal_by_seq: dict[int, list[EventRecord]] = {}
    for record in journal_valid:
        journal_by_seq.setdefault(record.sequence, []).append(record)
    state_by_seq: dict[int, EventRecord] = {record.sequence: record for record in state_valid}

    paired_sequences: set[int] = set()
    for sequence, journal_group in sorted(journal_by_seq.items()):
        state_record = state_by_seq.get(sequence)
        if state_record is None:
            for record in journal_group:
                issues.add(
                    "journal_only_event",
                    f"journal sequence {sequence} ({record.kind}) has no counterpart in the state snapshot",
                    sequence=sequence,
                    side="journal",
                    kind=record.kind,
                    ordinal=record.ordinal,
                )
            continue
        paired_sequences.add(sequence)
        canonical = journal_group[0]
        mismatches: dict[str, dict[str, Any]] = {}
        for field_name, left, right in (
            ("shift_code", canonical.shift_code, state_record.shift_code),
            ("kind", canonical.kind, state_record.kind),
            ("at", canonical.at, state_record.at),
            ("message", canonical.message, state_record.message),
        ):
            if left != right:
                mismatches[field_name] = {"journal": left, "state": right}
        if json.dumps(canonical.payload, ensure_ascii=False, sort_keys=True) != json.dumps(
            state_record.payload, ensure_ascii=False, sort_keys=True
        ):
            mismatches["payload"] = {"journal": canonical.payload, "state": state_record.payload}
        if mismatches:
            issues.add(
                "event_field_mismatch",
                f"sequence {sequence} differs between journal and state",
                sequence=sequence,
                fields=mismatches,
            )

    for record in sorted(state_valid, key=lambda item: item.sequence):
        if record.sequence not in journal_by_seq:
            issues.add(
                "state_only_event",
                f"state sequence {record.sequence} ({record.kind}) has no counterpart in the journal",
                sequence=record.sequence,
                side="state",
                kind=record.kind,
            )
    return state_by_seq, journal_by_seq, paired_sequences


def reconcile(
    journal_records: list[EventRecord],
    snapshot: _SnapshotIndex,
    *,
    data_dir: str = "",
    state_file: str = "",
    journal_file: str = "",
    generated_at: str = "",
    state_read_error: str | None = None,
) -> dict[str, Any]:
    """Build the full audit document from normalized journal and snapshot."""

    issues = _IssueBuilder()

    for record in journal_records:
        if record.malformed:
            issues.add(
                "malformed_journal_line",
                f"journal line {record.ordinal} could not be parsed",
                severity=SEVERITY_REVIEW,
                review=True,
                side="journal",
                ordinal=record.ordinal,
                raw=record.raw[:500],
                reason=record.malformed_reason,
            )

    state_records = normalize_state_events(snapshot.events)

    if state_read_error is not None:
        issues.add(
            "state_snapshot_unreadable",
            f"state snapshot could not be read: {state_read_error}",
            severity=SEVERITY_ERROR,
            side="state",
            reason=state_read_error,
        )
    elif not snapshot.present:
        issues.add(
            "state_snapshot_missing",
            "state snapshot is absent; journal events cannot be traced to current state",
            severity=SEVERITY_REVIEW,
            review=True,
            side="state",
        )

    journal_integrity = _check_sequence_integrity(
        journal_records,
        side="journal",
        duplicate_code="duplicate_journal_sequence",
        gap_code="journal_sequence_gap",
        order_code="journal_out_of_order",
        time_code="journal_time_inversion",
        next_sequence=snapshot.next_event_sequence,
        issues=issues,
    )
    state_integrity = _check_sequence_integrity(
        state_records,
        side="state",
        duplicate_code="duplicate_state_sequence",
        gap_code="state_sequence_gap",
        order_code="state_out_of_order",
        time_code="state_time_inversion",
        next_sequence=snapshot.next_event_sequence,
        issues=issues,
    )

    if (
        snapshot.present
        and snapshot.next_event_sequence is not None
        and state_integrity["max_sequence"] is not None
        and snapshot.next_event_sequence != state_integrity["max_sequence"] + 1
    ):
        issues.add(
            "sequence_counter_mismatch",
            "next_event_sequence does not follow the highest embedded event",
            side="state",
            next_event_sequence=snapshot.next_event_sequence,
            expected=state_integrity["max_sequence"] + 1,
            max_state_sequence=state_integrity["max_sequence"],
        )

    state_by_seq, journal_by_seq, paired = _pair_and_diff(journal_records, state_records, issues)

    _annotate_record_issues(journal_records, "journal", snapshot, issues)
    _annotate_record_issues(state_records, "state", snapshot, issues)
    _check_unjournaled_objects(journal_records, snapshot, issues)

    issue_codes_by_sequence: dict[int, list[str]] = {}
    for issue in issues.issues:
        if issue["sequence"] is not None:
            issue_codes_by_sequence.setdefault(issue["sequence"], []).append(issue["code"])

    events_view = _build_events_view(
        journal_records, state_records, state_by_seq, journal_by_seq, paired, snapshot, issue_codes_by_sequence
    )

    review_count = sum(1 for issue in issues.issues if issue["review"])
    error_count = sum(1 for issue in issues.issues if issue["severity"] == SEVERITY_ERROR)
    trace_counts = {"consistent": 0, "inconsistent": 0, "object_missing": 0, "review": 0, "no_object": 0}
    for event in events_view:
        verdict = event["trace"]["verdict"]
        if verdict == TRACE_CONSISTENT:
            trace_counts["consistent"] += 1
        elif verdict == TRACE_INCONSISTENT:
            trace_counts["inconsistent"] += 1
        elif verdict == TRACE_OBJECT_MISSING:
            trace_counts["object_missing"] += 1
        elif verdict == TRACE_REVIEW:
            trace_counts["review"] += 1
        else:
            trace_counts["no_object"] += 1

    status = "RECONCILED"
    if error_count:
        status = "DISCREPANCY"
    elif review_count:
        status = "REVIEW"

    summary = {
        "status": status,
        "journal_records": len([record for record in journal_records if not record.malformed]),
        "journal_malformed_lines": len([record for record in journal_records if record.malformed]),
        "state_events": len(state_records),
        "paired_events": len(paired),
        "journal_only_events": sum(1 for issue in issues.issues if issue["code"] == "journal_only_event"),
        "state_only_events": sum(1 for issue in issues.issues if issue["code"] == "state_only_event"),
        "field_mismatches": sum(1 for issue in issues.issues if issue["code"] == "event_field_mismatch"),
        "duplicate_journal_records": journal_integrity["duplicate_records"],
        "duplicate_state_records": state_integrity["duplicate_records"],
        "journal_gap_sequences": _gap_sequence_count(issues.issues, "journal_sequence_gap"),
        "state_gap_sequences": _gap_sequence_count(issues.issues, "state_sequence_gap"),
        "trace": trace_counts,
        "issue_total": len(issues.issues),
        "error_count": error_count,
        "review_count": review_count,
        "journal_max_sequence": journal_integrity["max_sequence"],
        "state_max_sequence": state_integrity["max_sequence"],
        "next_event_sequence": snapshot.next_event_sequence,
    }

    return {
        "module": "event-audit-reconciliation",
        "read_only": True,
        "generated_at": generated_at,
        "data_dir": data_dir,
        "state_file": state_file,
        "journal_file": journal_file,
        "summary": summary,
        "issues": sorted(issues.issues, key=_issue_sort_key),
        "review_items": [issue for issue in issues.issues if issue["review"]],
        "events": events_view,
    }


def _gap_sequence_count(issues: list[dict[str, Any]], code: str) -> int:
    total = 0
    for issue in issues:
        if issue["code"] == code:
            total += len(issue["details"].get("missing_sequences", []))
    return total


def _issue_sort_key(issue: dict[str, Any]) -> tuple[int, int, str]:
    sequence = issue["sequence"]
    side_rank = {"journal": 0, "state": 1}.get(issue["side"], 2)
    return (sequence if sequence is not None else 10**9, side_rank, issue["code"])


def _annotate_record_issues(records: list[EventRecord], side: str, snapshot: _SnapshotIndex, issues: _IssueBuilder) -> None:
    """Collect review/error issues derived from single events."""

    for record in records:
        if record.malformed:
            continue
        if record.kind not in KNOWN_KINDS:
            issues.add(
                "unknown_event_kind",
                f"{side} sequence {record.sequence} uses unrecognized event kind {record.kind!r}",
                severity=SEVERITY_REVIEW,
                review=True,
                sequence=record.sequence,
                side=side,
                kind=record.kind,
            )
            continue
        ref = resolve_object(record, snapshot)
        if ref.object_type is not None and ref.object_code is None:
            issues.add(
                "unattributable_event",
                f"{side} sequence {record.sequence} ({record.kind}) cannot be mapped to an object",
                severity=SEVERITY_REVIEW,
                review=True,
                sequence=record.sequence,
                side=side,
                kind=record.kind,
            )
        if record.shift_code and snapshot.present and record.shift_code not in snapshot.shifts:
            issues.add(
                "orphan_shift_reference",
                f"{side} sequence {record.sequence} references unknown shift {record.shift_code}",
                severity=SEVERITY_REVIEW,
                review=True,
                sequence=record.sequence,
                side=side,
                shift_code=record.shift_code,
            )
        trace = _trace_object(record.kind, ref, snapshot)
        if trace["verdict"] == TRACE_OBJECT_MISSING:
            issues.add(
                "trace_object_missing",
                f"{trace['object_type']} {trace['object_code']} from sequence {record.sequence} is absent from the snapshot",
                severity=SEVERITY_ERROR,
                sequence=record.sequence,
                side=side,
                object_type=trace["object_type"],
                object_code=trace["object_code"],
            )
        for car_trace in _trace_cars(ref, record.kind, snapshot):
            if car_trace["verdict"] == TRACE_OBJECT_MISSING:
                issues.add(
                    "trace_car_missing",
                    f"car {car_trace['car_code']} from sequence {record.sequence} is absent from the snapshot",
                    severity=SEVERITY_ERROR,
                    sequence=record.sequence,
                    side=side,
                    car_code=car_trace["car_code"],
                )


def _build_events_view(
    journal_records: list[EventRecord],
    state_records: list[EventRecord],
    state_by_seq: dict[int, EventRecord],
    journal_by_seq: dict[int, list[EventRecord]],
    paired: set[int],
    snapshot: _SnapshotIndex,
    issue_codes_by_sequence: dict[int, list[str]],
) -> list[dict[str, Any]]:
    view: list[dict[str, Any]] = []

    # One row per physical journal record so duplicated lines stay visible.
    for record in sorted(journal_records, key=lambda item: (item.sequence is None, item.sequence, item.ordinal)):
        if record.malformed:
            view.append(
                {
                    "source": "journal",
                    "ordinal": record.ordinal,
                    "sequence": None,
                    "at": None,
                    "shift_code": None,
                    "kind": None,
                    "message": None,
                    "payload": None,
                    "paired": False,
                    "review": True,
                    "issue_codes": ["malformed_journal_line"],
                    "object": None,
                    "trace": {
                        "object_type": None,
                        "object_code": None,
                        "verdict": TRACE_REVIEW,
                        "current_state": None,
                        "detail": record.malformed_reason,
                    },
                    "car_traces": [],
                }
            )
            continue
        ref = resolve_object(record, snapshot)
        trace = _trace_object(record.kind, ref, snapshot)
        car_traces = _trace_cars(ref, record.kind, snapshot)
        review = record.kind not in KNOWN_KINDS or (
            record.kind in KNOWN_KINDS and ref.object_type is not None and ref.object_code is None
        )
        view.append(
            {
                "source": "journal",
                "ordinal": record.ordinal,
                "sequence": record.sequence,
                "at": record.at,
                "shift_code": record.shift_code,
                "kind": record.kind,
                "message": record.message,
                "payload": record.payload,
                "paired": record.sequence in paired,
                "review": review,
                "issue_codes": sorted(set(issue_codes_by_sequence.get(record.sequence, []))),
                "object": {
                    "object_type": ref.object_type,
                    "object_code": ref.object_code,
                    "car_codes": ref.car_codes,
                    "attribution": ref.attribution,
                },
                "trace": trace,
                "car_traces": car_traces,
            }
        )

    for record in sorted(state_records, key=lambda item: (item.sequence is None, item.sequence)):
        if record.sequence in journal_by_seq:
            continue
        ref = resolve_object(record, snapshot)
        trace = _trace_object(record.kind, ref, snapshot)
        car_traces = _trace_cars(ref, record.kind, snapshot)
        review = record.kind not in KNOWN_KINDS or (ref.object_type is not None and ref.object_code is None)
        view.append(
            {
                "source": "state",
                "ordinal": record.ordinal,
                "sequence": record.sequence,
                "at": record.at,
                "shift_code": record.shift_code,
                "kind": record.kind,
                "message": record.message,
                "payload": record.payload,
                "paired": False,
                "review": review,
                "issue_codes": sorted(set(issue_codes_by_sequence.get(record.sequence, []))),
                "object": {
                    "object_type": ref.object_type,
                    "object_code": ref.object_code,
                    "car_codes": ref.car_codes,
                    "attribution": ref.attribution,
                },
                "trace": trace,
                "car_traces": car_traces,
            }
        )

    return view


def _check_unjournaled_objects(journal_records: list[EventRecord], snapshot: _SnapshotIndex, issues: _IssueBuilder) -> None:
    """Flag snapshot entities that no journal event accounts for."""

    if not snapshot.present:
        return
    referenced_shifts: set[str] = set()
    referenced_intakes: set[str] = set()
    referenced_outbounds: set[str] = set()
    referenced_runs: set[str] = set()
    for record in journal_records:
        if record.malformed or record.sequence is None:
            continue
        ref = resolve_object(record, snapshot)
        if ref.object_code is None:
            continue
        if ref.object_type == "SHIFT":
            referenced_shifts.add(ref.object_code)
        elif ref.object_type == "INTAKE":
            referenced_intakes.add(ref.object_code)
        elif ref.object_type == "OUTBOUND":
            referenced_outbounds.add(ref.object_code)
        elif ref.object_type == "PULL_RUN":
            referenced_runs.add(ref.object_code)

    for code in sorted(set(snapshot.shifts) - referenced_shifts):
        issues.add(
            "object_without_journal_event",
            f"shift {code} exists in the snapshot but has no journal event",
            severity=SEVERITY_ERROR,
            object_type="SHIFT",
            object_code=code,
        )
    for code in sorted(set(snapshot.intakes) - referenced_intakes):
        issues.add(
            "object_without_journal_event",
            f"intake {code} exists in the snapshot but has no journal event",
            severity=SEVERITY_ERROR,
            object_type="INTAKE",
            object_code=code,
        )
    for code in sorted(set(snapshot.outbounds) - referenced_outbounds):
        issues.add(
            "object_without_journal_event",
            f"outbound {code} exists in the snapshot but has no journal event",
            severity=SEVERITY_ERROR,
            object_type="OUTBOUND",
            object_code=code,
        )
    for code in sorted(set(snapshot.runs) - referenced_runs):
        issues.add(
            "object_without_journal_event",
            f"pull run {code} exists in the snapshot but has no journal event",
            severity=SEVERITY_ERROR,
            object_type="PULL_RUN",
            object_code=code,
        )

    # Cars belong to an intake consist; account for them through the intake's
    # received/classified journal coverage plus explicit car references.
    covered_intake_cars: set[str] = set()
    for code in referenced_intakes:
        intake = snapshot.intakes.get(code)
        if intake is not None:
            covered_intake_cars.update(str(item) for item in intake.get("consist", []))
    for record in journal_records:
        if record.malformed:
            continue
        covered_intake_cars.update(resolve_object(record, snapshot).car_codes)
    for code in sorted(set(snapshot.cars) - covered_intake_cars):
        issues.add(
            "object_without_journal_event",
            f"car {code} exists in the snapshot but has no journal event",
            severity=SEVERITY_ERROR,
            object_type="CAR",
            object_code=code,
        )


def apply_filters(
    document: dict[str, Any],
    *,
    shift: str | None = None,
    kind: str | None = None,
    car: str | None = None,
    pull_run: str | None = None,
) -> dict[str, Any]:
    """Return a copy of the audit document with a filtered event view.

    Issues and review items are never filtered out: a narrowed view must not
    hide divergence that a human reviewer still has to adjudicate.
    """

    kind_value = kind.upper() if kind else None
    filtered: list[dict[str, Any]] = []
    for event in document["events"]:
        if shift and event.get("shift_code") != shift:
            continue
        if kind_value and event.get("kind") != kind_value:
            continue
        obj = event.get("object") or {}
        if pull_run and not (obj.get("object_type") == "PULL_RUN" and obj.get("object_code") == pull_run):
            continue
        if car:
            car_codes = set(obj.get("car_codes") or [])
            car_codes.update(item.get("car_code") for item in event.get("car_traces", []))
            if car not in car_codes:
                continue
        filtered.append(event)

    view = dict(document)
    view["events"] = filtered
    view["filters"] = {"shift": shift, "kind": kind_value, "car": car, "pull_run": pull_run}
    view["filtered_event_count"] = len(filtered)
    return view


__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_REVIEW",
    "TRACE_CONSISTENT",
    "TRACE_INCONSISTENT",
    "TRACE_NO_OBJECT",
    "TRACE_OBJECT_MISSING",
    "TRACE_REVIEW",
    "apply_filters",
    "index_snapshot",
    "normalize_journal",
    "reconcile",
    "resolve_object",
]
