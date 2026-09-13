"""Read-only shift handover briefing derived from persisted state and events.

The briefing is a pure projection: it never mutates the workspace and never
records events. Every listed object carries its code and the timestamp of the
last event that mentioned it during the shift, so an incoming crew can see at a
glance what the outgoing crew received, classified, assembled, pulled,
departed, and left blocked.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.enums import CarState, EventKind, IntakeState, OutboundState, RunState
from ..domain.timeutil import now_iso
from .closure import closure_blockers

# Object codes embedded in historical event messages; payload codes are
# preferred and this only covers events persisted before payload enrichment.
_CODE_RE = re.compile(r"(?:INT|OB|RUN|SHIFT|SNAP)-[A-Z0-9_-]+|C-[A-Z0-9_-]+")

_PAYLOAD_CODE_KEYS = ("intake_code", "outbound_code", "run_code", "shift_code")


def _event_codes(event: Any) -> set[str]:
    codes: set[str] = set()
    payload = event.payload or {}
    for key in _PAYLOAD_CODE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            codes.add(value)
    for match in _CODE_RE.findall(event.message or ""):
        codes.add(match)
    return codes


def _codes_by_event_kind(events: list[Any], kind: Any) -> dict[str, Any]:
    """Map each object code to the latest event of the given kind."""
    indexed: dict[str, Any] = {}
    for event in events:
        if event.kind != kind:
            continue
        for code in _event_codes(event):
            indexed[code] = event
    return indexed


def build_handoff_briefing(workspace: Any, shift: Any, generated_at: str | None = None) -> dict[str, Any]:
    """Project a one-page handover briefing for one persisted shift."""
    shift_code = shift.code
    events = [event for event in workspace.events if event.shift_code == shift_code]

    # Latest timestamp per object code across every shift event.
    last_at: dict[str, str] = {}
    for event in events:
        at = event.at
        for code in _event_codes(event):
            last_at[code] = at
        if event.kind.value == "CLOSURE_BLOCKED":
            for blocker in (event.payload or {}).get("blockers", []) or []:
                code = blocker.get("code") if isinstance(blocker, dict) else None
                if isinstance(code, str) and code:
                    last_at[code] = at

    received_index = _codes_by_event_kind(events, EventKind.TRAIN_RECEIVED)
    created_index = _codes_by_event_kind(events, EventKind.TRAIN_CREATED)
    planned_index = _codes_by_event_kind(events, EventKind.PULL_PLANNED)

    intake_codes = set(received_index)
    outbound_codes = set(created_index)
    run_codes = set(planned_index)
    car_codes: set[str] = set()
    for intake_code in intake_codes:
        train = workspace.intakes.get(intake_code)
        if train is not None:
            car_codes.update(train.consist)

    received_items: list[dict[str, Any]] = []
    classified_items: list[dict[str, Any]] = []
    pending_items: list[dict[str, Any]] = []
    classified_car_total = 0

    for intake_code in sorted(intake_codes):
        train = workspace.intakes.get(intake_code)
        if train is None:
            continue
        consist = list(train.consist)
        placed_codes = [
            code
            for code in consist
            if (car := workspace.cars.get(code)) is not None and car.state != CarState.RECEIVED
        ]
        unplaced_codes = [code for code in consist if code not in placed_codes]
        classified_count = len(placed_codes)
        classified_car_total += classified_count
        received_items.append(
            {
                "code": train.code,
                "state": train.state.value,
                "route": train.route,
                "car_count": len(consist),
                "car_codes": consist,
                "last_event_at": last_at.get(train.code) or train.arrival_at,
            }
        )
        if classified_count:
            classified_items.append(
                {
                    "code": train.code,
                    "state": train.state.value,
                    "classified_car_count": classified_count,
                    "classified_car_codes": placed_codes,
                    "unplaced_car_codes": unplaced_codes,
                    "last_event_at": last_at.get(train.code) or train.arrival_at,
                }
            )
        if train.state in {IntakeState.OPEN, IntakeState.PARTIAL}:
            pending_items.append(
                {
                    "code": train.code,
                    "kind": "intake",
                    "state": train.state.value,
                    "unplaced_car_codes": unplaced_codes,
                    "last_event_at": last_at.get(train.code) or train.arrival_at,
                }
            )

    assembled_items: list[dict[str, Any]] = []
    departed_items: list[dict[str, Any]] = []
    for outbound_code in sorted(outbound_codes):
        train = workspace.outbounds.get(outbound_code)
        if train is None:
            continue
        if train.state == OutboundState.DRAFT:
            pending_items.append(
                {
                    "code": train.code,
                    "kind": "outbound",
                    "state": train.state.value,
                    "planned_car_count": len(train.planned_car_codes),
                    "last_event_at": last_at.get(train.code) or train.created_at,
                }
            )
        elif train.state == OutboundState.READY:
            assembled_items.append(
                {
                    "code": train.code,
                    "state": train.state.value,
                    "destination": train.destination,
                    "assembled_car_count": len(train.assembled_car_codes),
                    "assembled_car_codes": list(train.assembled_car_codes),
                    "last_event_at": last_at.get(train.code) or train.created_at,
                }
            )
        elif train.state == OutboundState.DEPARTED:
            departed_items.append(
                {
                    "code": train.code,
                    "state": train.state.value,
                    "destination": train.destination,
                    "departed_car_count": len(train.assembled_car_codes),
                    "departed_car_codes": list(train.assembled_car_codes),
                    "last_event_at": last_at.get(train.code) or train.departed_at or train.created_at,
                }
            )

    active_run_items: list[dict[str, Any]] = []
    for run_code in sorted(run_codes):
        run = workspace.runs.get(run_code)
        if run is None or run.state not in {RunState.QUEUED, RunState.RUNNING}:
            continue
        active_run_items.append(
            {
                "code": run.code,
                "state": run.state.value,
                "outbound_code": run.outbound_code,
                "current_step": run.current_step,
                "total_steps": len(run.steps),
                "remaining_steps": run.remaining(),
                "last_event_at": last_at.get(run.code) or run.started_at or run.created_at,
            }
        )

    blockers: list[dict[str, Any]] = []
    for blocker in closure_blockers(workspace):
        code = blocker["code"]
        kind = blocker["kind"]
        if kind == "intake" and code not in intake_codes:
            continue
        if kind == "outbound" and code not in outbound_codes:
            continue
        if kind == "pull_run" and code not in run_codes:
            continue
        if kind == "unclassified_car" and code not in car_codes:
            continue
        # maintenance_track blockers describe yard infrastructure and stay
        # visible regardless of which shift persisted the cars.
        blockers.append(
            {
                "code": code,
                "kind": kind,
                "message": blocker["message"],
                "last_event_at": last_at.get(code),
            }
        )

    last_event = events[-1] if events else None
    source_sequence = last_event.sequence if last_event else 0
    assembled_car_total = sum(item["assembled_car_count"] for item in assembled_items)
    departed_car_total = sum(item["departed_car_count"] for item in departed_items)
    return {
        "briefing_code": f"BRIEF-{shift_code}-{source_sequence}",
        "shift_code": shift_code,
        "briefing_version": workspace.version,
        "generated_at": generated_at or now_iso(),
        "source_event_sequence": source_sequence,
        "last_event_at": last_event.at if last_event else None,
        "read_only": True,
        "shift": {
            "code": shift.code,
            "state": str(shift.state),
            "dispatcher": shift.dispatcher,
            "opened_at": shift.opened_at,
            "closed_at": shift.closed_at,
        },
        "totals": {
            "received_cars": sum(item["car_count"] for item in received_items),
            "classified_cars": classified_car_total,
            "pending_items": len(pending_items),
            "ready_outbound_trains": len(assembled_items),
            "ready_outbound_cars": assembled_car_total,
            "active_pull_runs": len(active_run_items),
            "departed_trains": len(departed_items),
            "departed_cars": departed_car_total,
            "blockers": len(blockers),
        },
        "received": {"car_count": sum(item["car_count"] for item in received_items), "items": received_items},
        "classified": {"car_count": classified_car_total, "items": classified_items},
        "pending": pending_items,
        "assembled_outbound": {
            "train_count": len(assembled_items),
            "car_count": assembled_car_total,
            "items": assembled_items,
        },
        "active_pull_runs": {"count": len(active_run_items), "items": active_run_items},
        "departed": {
            "train_count": len(departed_items),
            "car_count": departed_car_total,
            "items": departed_items,
        },
        "blockers": blockers,
    }


__all__ = ["build_handoff_briefing"]
