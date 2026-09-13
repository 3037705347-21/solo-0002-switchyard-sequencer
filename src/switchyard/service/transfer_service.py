"""Transfer line registration, capacity views, and operating state."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, RunState, TrackState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError
from ..domain.timeutil import now_iso
from ..domain.track import BufferBay
from ..domain.transfer import ACTIVE_RUN_STATES, line_capacities, reconcile_transfer_occupancy
from ..domain.validators import build_transfer_registration, parse_transfer_state
from .context import YardApplication


def _line_payload(bay: BufferBay, capacity: Any | None) -> dict[str, Any]:
    payload = bay.to_dict()
    payload["available"] = bay.state == TrackState.OPERATIONAL
    if capacity is not None:
        payload.update(
            {
                "physical_cars": capacity.physical_cars,
                "committed_cars": capacity.committed_cars,
                "available_cars": capacity.available_cars,
                "orphan_cars": capacity.orphan_cars,
                "held_by": [item.to_dict() for item in capacity.held_by],
            }
        )
    return payload


def register_transfer_line(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, capacity_cars = build_transfer_registration(payload)
    workspace = app.load()
    if code in workspace.buffer_bays:
        raise ConflictError("transfer line already registered", code=code)
    order = max((bay.registered_order for bay in workspace.buffer_bays.values()), default=0) + 1
    bay = BufferBay(
        code=code,
        capacity_cars=capacity_cars,
        state=TrackState.OPERATIONAL,
        registered_order=order,
        registered_at=now_iso(),
    )
    workspace.buffer_bays[code] = bay
    event = workspace.record_event(
        "NONE",
        EventKind.TRANSFER_REGISTERED,
        f"transfer line {code} registered with {capacity_cars} slot(s)",
        {"capacity_cars": capacity_cars, "registered_order": order},
    )
    app.commit(workspace, event)
    capacities = line_capacities(workspace.buffer_bays, workspace.runs, workspace.transfer_reservations)
    return {"transfer_line": _line_payload(bay, capacities[code])}


def list_transfer_lines(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    # Rebuild holds from persisted runs so a view after a restart still agrees
    # with live vehicle positions; persist only when audit records changed.
    before = [item.to_dict() for item in workspace.transfer_reservations.values()]
    capacities = reconcile_transfer_occupancy(workspace)
    after = [item.to_dict() for item in workspace.transfer_reservations.values()]
    if before != after:
        event = workspace.record_event(
            "NONE",
            EventKind.TRANSFER_RECONCILED,
            "transfer line occupancy reconciled",
            {},
        )
        app.commit(workspace, event)
    ordered = sorted(
        workspace.buffer_bays.values(),
        key=lambda bay: (bay.registered_order, bay.code),
    )
    return {
        "transfer_lines": [_line_payload(bay, capacities[bay.code]) for bay in ordered],
        "total_capacity_cars": sum(bay.capacity_cars for bay in ordered),
        "physical_cars": sum(len(bay.stack) for bay in ordered),
    }


def set_transfer_line_state(app: YardApplication, code: str, payload: Any) -> dict[str, Any]:
    state_text = parse_transfer_state(payload)
    target_state = TrackState.parse(state_text)
    workspace = app.load()
    bay = workspace.buffer_bays.get(code)
    if bay is None:
        raise NotFoundError("transfer line", code)
    if bay.state == target_state:
        raise ConflictError(f"transfer line {code} is already {state_text}", code=code)
    active = [
        run
        for run in workspace.runs.values()
        if run.state in ACTIVE_RUN_STATES and run.transfer_code == code
    ]
    if target_state != TrackState.OPERATIONAL and active:
        queued = sorted(run.code for run in active if run.state == RunState.QUEUED)
        running = sorted(run.code for run in active if run.state == RunState.RUNNING)
        raise ResourceBusyError(
            f"transfer line {code} still has {len(active)} active pull run(s)",
            queued=queued,
            running=running,
        )
    old_state = str(bay.state)
    bay.state = target_state
    event = workspace.record_event(
        "NONE",
        EventKind.TRANSFER_STATE_CHANGED,
        f"transfer line {code} moved {old_state} -> {state_text}",
        {"from": old_state, "to": state_text},
    )
    app.commit(workspace, event)
    capacities = line_capacities(workspace.buffer_bays, workspace.runs, workspace.transfer_reservations)
    return {"transfer_line": _line_payload(bay, capacities[code])}


__all__ = ["list_transfer_lines", "register_transfer_line", "set_transfer_line_state"]
