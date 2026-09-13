"""Track reorganization work order commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.executor import execute_step
from ..domain.reorder import ReorderOrder, StepRecord, plan_reorder_order
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_reorder
from ..domain.validators import build_reorder_payload, parse_advance_steps
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before reordering tracks")


def create_reorder_order(app: YardApplication, payload: Any) -> dict[str, Any]:
    request = build_reorder_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    if request.code in workspace.reorders:
        raise ConflictError("reorder order already exists", code=request.code)
    order = plan_reorder_order(request, workspace.cars, workspace.tracks, workspace.buffer_bays)
    workspace.reorders[order.code] = order
    event = workspace.record_event(
        shift_code,
        EventKind.REORDER_PLANNED,
        f"reorder order {order.code} planned with {len(order.steps)} steps",
        {
            "mode": str(order.mode),
            "steps": len(order.steps),
            "transfer_code": order.transfer_code,
            "moves": [move.to_dict() for move in order.moves],
        },
    )
    app.commit(workspace, event)
    return {"reorder_order": order.to_dict()}


def get_reorder_order(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    order = workspace.reorders.get(code)
    if order is None:
        raise NotFoundError("reorder order", code)
    return {"reorder_order": order.to_dict()}


def advance_reorder(app: YardApplication, code: str, payload: Any) -> dict[str, Any]:
    requested_steps = parse_advance_steps(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    order = workspace.reorders.get(code)
    if order is None:
        raise NotFoundError("reorder order", code)
    if order.state == RunState.COMPLETED:
        raise ConflictError("reorder order is already complete", code=code)
    if order.state == RunState.FAILED:
        raise ConflictError("reorder order has failed", code=code)
    events: list[Any] = []
    if order.state == RunState.QUEUED:
        transition_reorder(order, RunState.RUNNING)
        order.started_at = now_iso()
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.REORDER_STARTED,
                f"reorder order {code} started",
                {"total_steps": len(order.steps)},
            )
        )
    executed = 0
    while executed < requested_steps and order.current_step < len(order.steps):
        step = order.steps[order.current_step]
        execute_step(workspace, order, step)
        car = workspace.cars[step.car_code]
        order.step_records.append(
            StepRecord(
                step_index=order.current_step,
                verb=step.verb,
                car_code=step.car_code,
                source_code=step.source_code,
                target_code=step.target_code,
                location_after=car.location,
                at=now_iso(),
            )
        )
        order.current_step += 1
        executed += 1
    completed = order.current_step >= len(order.steps)
    if completed:
        _verify_completion(workspace, order)
        transition_reorder(order, RunState.COMPLETED)
        order.completed_at = now_iso()
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.REORDER_COMPLETED,
                f"reorder order {code} completed",
                {
                    "steps": len(order.steps),
                    "final_locations": dict(order.expected_locations),
                },
            )
        )
    else:
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.REORDER_ADVANCED,
                f"reorder order {code} advanced {executed} steps",
                {
                    "current_step": order.current_step,
                    "remaining": order.remaining(),
                },
            )
        )
    app.commit(workspace, events)
    return {
        "reorder_order": order.to_dict(),
        "executed_steps": executed,
        "completed": completed,
    }


def _verify_completion(workspace: Any, order: ReorderOrder) -> None:
    bay = workspace.buffer_bays[order.transfer_code]
    if bay.stack:
        raise ValidationError(
            "reorder order finished with cars still in the transfer bay",
            **{"transfer_bay": list(bay.stack)},
        )
    problems: list[str] = []
    for car_code, expected in order.expected_locations.items():
        car = workspace.cars.get(car_code)
        if car is None or car.location != expected:
            problems.append(f"{car_code} should be on {expected}")
        elif car.state != CarState.STANDING:
            problems.append(f"{car_code} is {car.state.value}, not STANDING")
    if problems:
        raise ValidationError(
            "reorder order finished without reaching the planned positions",
            **{"expected_locations": problems},
        )


__all__ = ["advance_reorder", "create_reorder_order", "get_reorder_order"]
