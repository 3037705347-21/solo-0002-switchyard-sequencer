"""Pull run advancement, cancellation, and outbound departure commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.executor import execute_step
from ..domain.timeutil import now_iso
from ..domain.transfer import (
    reconcile_transfer_occupancy,
    refresh_observed_peaks,
    release_reservation,
)
from ..domain.transitions import transition_car, transition_outbound, transition_run
from ..domain.validators import parse_advance_steps
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before moving cars")


def advance_run(app: YardApplication, run_code: str, payload: Any) -> dict[str, Any]:
    requested_steps = parse_advance_steps(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    if run.state == RunState.COMPLETED:
        raise ConflictError("pull run is already complete", code=run_code)
    if run.state == RunState.FAILED:
        raise ConflictError("pull run has failed", code=run_code)
    events: list[Any] = []
    if run.state == RunState.QUEUED:
        transition_run(run, RunState.RUNNING)
        run.started_at = now_iso()
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_STARTED,
                f"pull run {run_code} started",
                {"total_steps": len(run.steps)},
            )
        )
    executed = 0
    while executed < requested_steps and run.current_step < len(run.steps):
        step = run.steps[run.current_step]
        execute_step(workspace, run, step)
        run.current_step += 1
        executed += 1
    outbound = workspace.outbounds.get(run.outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", run.outbound_code)
    completed = run.current_step >= len(run.steps)
    # Recompute holds from live positions: buffer/return actions have moved
    # cars, so the run's hold must shrink with returned cars and track the
    # remaining peak. This is what keeps a partial run recoverable.
    capacities = reconcile_transfer_occupancy(workspace)
    refresh_observed_peaks(workspace)
    if completed:
        if not outbound.assembly_complete():
            raise ValidationError(
                "pull run finished without matching the planned consist",
                **{"assembled": outbound.assembled_car_codes},
            )
        run.state = RunState.COMPLETED
        run.completed_at = now_iso()
        transition_outbound(outbound, OutboundState.READY)
        release_reservation(workspace, run.code)
        line_view = capacities.get(run.transfer_code)
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_COMPLETED,
                f"pull run {run_code} completed",
                {
                    "assembled_car_codes": list(outbound.assembled_car_codes),
                    "steps": len(run.steps),
                    "transfer_code": run.transfer_code,
                    "transfer_physical_cars": None if line_view is None else line_view.physical_cars,
                },
            )
        )
    else:
        line_view = capacities.get(run.transfer_code)
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_ADVANCED,
                f"pull run {run_code} advanced {executed} steps",
                {
                    "current_step": run.current_step,
                    "remaining": run.remaining(),
                    "transfer_code": run.transfer_code,
                    "transfer_physical_cars": None if line_view is None else line_view.physical_cars,
                    "transfer_committed_cars": None if line_view is None else line_view.committed_cars,
                },
            )
        )
    app.commit(workspace, events)
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "executed_steps": executed,
        "completed": completed,
    }


def cancel_run(app: YardApplication, run_code: str) -> dict[str, Any]:
    """Cancel a queued pull run before any car has moved.

    The ticket goes back to DRAFT so it can be replanned, reserved cars return
    to STANDING, and the transfer-line hold is released. A running run is
    rejected: its cars are physically split between track and transfer line
    and must be advanced to completion instead.
    """
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    if run.state == RunState.CANCELLED:
        raise ConflictError("pull run is already cancelled", code=run_code)
    if run.state in {RunState.COMPLETED, RunState.FAILED}:
        raise ConflictError(f"pull run is already {run.state.value.lower()}", code=run_code)
    if run.state == RunState.RUNNING:
        raise ResourceBusyError(
            "running pull run cannot be cancelled; advance it to completion",
            run_code=run_code,
            current_step=run.current_step,
            total_steps=len(run.steps),
        )
    outbound = workspace.outbounds.get(run.outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", run.outbound_code)
    transition_run(run, RunState.CANCELLED)
    for code in outbound.planned_car_codes:
        car = workspace.cars.get(code)
        if car is not None and car.state == CarState.RESERVED:
            transition_car(car, CarState.STANDING)
    transition_outbound(outbound, OutboundState.DRAFT)
    if run.code in outbound.run_codes:
        outbound.run_codes.remove(run.code)
    release_reservation(workspace, run.code)
    capacities = reconcile_transfer_occupancy(workspace)
    line_view = capacities.get(run.transfer_code)
    event = workspace.record_event(
        shift_code,
        EventKind.PULL_RUN_CANCELLED,
        f"pull run {run_code} cancelled",
        {
            "outbound_code": outbound.code,
            "transfer_code": run.transfer_code,
            "transfer_available_cars": None if line_view is None else line_view.available_cars,
        },
    )
    app.commit(workspace, event)
    return {"pull_run": run.to_dict(), "outbound": outbound.to_dict()}


def depart_outbound(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state != OutboundState.READY:
        raise ValidationError(
            "outbound train is not ready",
            **{"outbound_code": [f"current state is {outbound.state.value}"]},
        )
    if not outbound.assembly_complete():
        raise ValidationError("outbound assembly is incomplete", **{"assembled": outbound.assembled_car_codes})
    for code in outbound.assembled_car_codes:
        car = workspace.cars.get(code)
        if car is None or car.state != CarState.ASSEMBLED:
            raise ValidationError(
                f"assembled car {code} is not in assembled state",
                **{"assembled": [code]},
            )
    departed_at = now_iso()
    transition_outbound(outbound, OutboundState.DEPARTED)
    outbound.departed_at = departed_at
    for code in outbound.assembled_car_codes:
        transition_car(workspace.cars[code], CarState.DEPARTED)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_DEPARTED,
        f"outbound {outbound.code} departed for {outbound.destination}",
        {"car_count": len(outbound.assembled_car_codes), "departed_at": departed_at},
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "departed_car_count": len(outbound.assembled_car_codes),
    }


__all__ = ["advance_run", "cancel_run", "depart_outbound"]
