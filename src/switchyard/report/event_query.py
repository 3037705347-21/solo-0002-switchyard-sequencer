"""Read model for shift events.

This module never mutates the workspace. It derives, from a single workspace
snapshot:

* the subject object of an event (a shift, intake train, outbound train, or
  pull run) and every object code an event touches (including individual cars),
* the current state of those objects,
* whether each event still describes the current state of its subject, was
  superseded by a later event, or describes an action that never took effect
  (for example a closure that was blocked).

The relationship data lets callers distinguish an attempted/rolled-back action
from the final state of an object.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..domain.enums import EventKind

# event kind -> (subject type, payload key holding the subject code)
SUBJECT_SPECS: dict[str, tuple[str, str]] = {
    EventKind.SHIFT_OPENED.value: ("shift", "shift_code"),
    EventKind.SHIFT_CLOSED.value: ("shift", "shift_code"),
    EventKind.CLOSURE_BLOCKED.value: ("shift", "shift_code"),
    EventKind.TRAIN_RECEIVED.value: ("intake", "intake_code"),
    EventKind.TRAIN_CLASSIFIED.value: ("intake", "intake_code"),
    EventKind.TRAIN_CREATED.value: ("outbound", "outbound_code"),
    EventKind.TRAIN_DEPARTED.value: ("outbound", "outbound_code"),
    EventKind.PULL_PLANNED.value: ("run", "run_code"),
    EventKind.PULL_RUN_STARTED.value: ("run", "run_code"),
    EventKind.PULL_RUN_ADVANCED.value: ("run", "run_code"),
    EventKind.PULL_RUN_COMPLETED.value: ("run", "run_code"),
}

_SUBJECT_COLLECTIONS: dict[str, str] = {
    "shift": "shifts",
    "intake": "intakes",
    "outbound": "outbounds",
    "run": "runs",
}

# Event kinds that establish a new lifecycle state for their subject. Other
# kinds (for example CLOSURE_BLOCKED, which explicitly does not change state)
# are never considered the "current" state-bearing event.
STATE_BEARING_KINDS: dict[str, frozenset[str]] = {
    "shift": frozenset({EventKind.SHIFT_OPENED.value, EventKind.SHIFT_CLOSED.value}),
    "intake": frozenset({EventKind.TRAIN_RECEIVED.value, EventKind.TRAIN_CLASSIFIED.value}),
    "outbound": frozenset({EventKind.TRAIN_CREATED.value, EventKind.TRAIN_DEPARTED.value}),
    "run": frozenset(
        {
            EventKind.PULL_PLANNED.value,
            EventKind.PULL_RUN_STARTED.value,
            EventKind.PULL_RUN_ADVANCED.value,
            EventKind.PULL_RUN_COMPLETED.value,
        }
    ),
}

# State an event asserted its subject was moving into, when that state is not
# simply derivable from the event kind.
DECLARED_SUBJECT_STATE: dict[str, str] = {
    EventKind.SHIFT_OPENED.value: "OPEN",
    EventKind.SHIFT_CLOSED.value: "CLOSED",
    EventKind.TRAIN_RECEIVED.value: "OPEN",
    EventKind.TRAIN_CLASSIFIED.value: "CLASSIFIED",
    EventKind.TRAIN_CREATED.value: "DRAFT",
    EventKind.PULL_PLANNED.value: "PLANNED",
    EventKind.PULL_RUN_STARTED.value: "RUNNING",
    EventKind.PULL_RUN_ADVANCED.value: "RUNNING",
    EventKind.PULL_RUN_COMPLETED.value: "COMPLETED",
    EventKind.TRAIN_DEPARTED.value: "DEPARTED",
}

# Fallback extraction for events written before payloads carried explicit
# object codes. Messages use stable formats such as "shift SHIFT-01 opened",
# "intake INT-01 ...", "outbound OB-01 ...", "pull run RUN-OB-01 ...".
_LEGACY_CODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"pull run (RUN-\S+)"),
    re.compile(r"shift (SHIFT-\S+)"),
    re.compile(r"intake (INT-\S+)"),
    re.compile(r"outbound (OB-\S+)"),
)


def event_kind(event: Any) -> str:
    kind = event.kind
    return str(kind)


def event_subject(event: Any) -> tuple[str | None, str | None]:
    """Return ``(subject_type, subject_code)`` for an event.

    ``(None, None)`` is returned for informational events that do not act on a
    single lifecycle object.
    """

    kind = event_kind(event)
    spec = SUBJECT_SPECS.get(kind)
    if spec is None:
        return None, None
    subject_type, payload_key = spec
    code = event.payload.get(payload_key)
    if code is None:
        code = _legacy_subject_code(event.message)
    if code is None:
        return subject_type, None
    return subject_type, str(code)


def _legacy_subject_code(message: str) -> str | None:
    for pattern in _LEGACY_CODE_PATTERNS:
        match = pattern.search(message)
        if match:
            return match.group(1).rstrip(".,)")
    return None


def referenced_codes(event: Any) -> set[str]:
    """All object codes an event references: subject, related entities, cars."""

    codes: set[str] = set()
    _, subject_code = event_subject(event)
    if subject_code:
        codes.add(subject_code)
    payload = event.payload
    for key in ("shift_code", "intake_code", "outbound_code", "run_code", "transfer_code"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            codes.add(value)
    for key in ("car_codes", "assembled_car_codes", "planned_car_codes"):
        value = payload.get(key)
        if isinstance(value, list):
            codes.update(str(item) for item in value if isinstance(item, str))
    raw_steps = payload.get("steps", [])
    for step in raw_steps if isinstance(raw_steps, list) else []:
        if isinstance(step, dict):
            for key in ("car_code", "source_code", "target_code"):
                value = step.get(key)
                if isinstance(value, str) and value:
                    codes.add(str(value))
    for spot in payload.get("spots", []):
        if isinstance(spot, dict) and isinstance(spot.get("car_code"), str):
            codes.add(str(spot["car_code"]))
    return codes


def _current_subject(workspace: Any, subject_type: str, code: str) -> Any:
    collection = getattr(workspace, _SUBJECT_COLLECTIONS[subject_type])
    return collection.get(code)


def build_subject_index(workspace: Any) -> dict[str, list[Any]]:
    """Map ``"TYPE:CODE"`` to the state-bearing events for that subject."""

    chains: dict[str, list[Any]] = {}
    for event in sorted(workspace.events, key=lambda item: item.sequence):
        subject_type, subject_code = event_subject(event)
        if subject_type is None or subject_code is None:
            continue
        if event_kind(event) not in STATE_BEARING_KINDS.get(subject_type, frozenset()):
            continue
        chains.setdefault(f"{subject_type}:{subject_code}", []).append(event)
    return chains


def _step_car_states(event: Any, workspace: Any) -> list[dict[str, Any]]:
    cars: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(code: str | None) -> None:
        if not code or code in seen:
            return
        seen.add(code)
        car = workspace.cars.get(code)
        if car is None:
            cars.append({"code": code, "exists": False})
            return
        cars.append(
            {
                "code": code,
                "exists": True,
                "state": str(car.state),
                "location": car.location,
            }
        )

    payload = event.payload
    for key in ("car_codes", "assembled_car_codes"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    add(item)
    raw_steps = payload.get("steps", [])
    for step in raw_steps if isinstance(raw_steps, list) else []:
        if isinstance(step, dict):
            add(step.get("car_code"))
    return cars


def annotate_event(
    event: Any,
    workspace: Any,
    subject_index: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Return the serialized event plus its relationship to current state."""

    document = event.to_dict()
    kind = event_kind(event)
    subject_type, subject_code = event_subject(event)

    relation: dict[str, Any] = {
        "subject_type": subject_type,
        "subject_code": subject_code,
        "status": "INFORMATIONAL",
        "state_matches": None,
        "current_state": None,
        "declared_state": DECLARED_SUBJECT_STATE.get(kind),
        "current_exists": None,
    }

    if subject_type is not None and subject_code is not None:
        index = subject_index if subject_index is not None else build_subject_index(workspace)
        chain = index.get(f"{subject_type}:{subject_code}", [])
        latest = chain[-1] if chain else None
        subject = _current_subject(workspace, subject_type, subject_code)
        relation["current_exists"] = subject is not None
        relation["current_state"] = str(subject.state) if subject is not None else None

        if kind == EventKind.CLOSURE_BLOCKED.value:
            # A blocked closure is recorded for audit but never moves the
            # shift; it must never be read as a CLOSED outcome.
            relation["status"] = "REJECTED"
            relation["state_matches"] = False
        elif subject is None:
            relation["status"] = "MISSING"
            relation["state_matches"] = False
        elif latest is not None and latest.sequence == event.sequence:
            relation["status"] = "CURRENT"
            declared = DECLARED_SUBJECT_STATE.get(kind)
            relation["state_matches"] = declared is None or declared == str(subject.state)
        else:
            relation["status"] = "SUPERSEDED"
            declared = DECLARED_SUBJECT_STATE.get(kind)
            relation["state_matches"] = declared is None or declared == str(subject.state)

    document["relation"] = relation
    car_states = _step_car_states(event, workspace)
    if car_states:
        document["car_states"] = car_states
    return document


def annotate_events(workspace: Any, events: Iterable[Any]) -> list[dict[str, Any]]:
    index = build_subject_index(workspace)
    return [annotate_event(event, workspace, index) for event in events]


__all__ = [
    "DECLARED_SUBJECT_STATE",
    "STATE_BEARING_KINDS",
    "annotate_event",
    "annotate_events",
    "build_subject_index",
    "event_subject",
    "referenced_codes",
]
