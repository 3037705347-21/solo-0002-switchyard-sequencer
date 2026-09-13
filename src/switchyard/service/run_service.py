"""Pull run advancement and outbound departure commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.executor import execute_step
from ..domain.rules import MAX_DEPARTURE_CLOCK_SKEW_SECONDS
from ..domain.timeutil import is_after_or_equal, now_iso, parse_iso
from ..domain.transitions import transition_car, transition_outbound, transition_run
from ..domain.validators import parse_advance_steps, parse_departure_payload
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
    if completed:
        if not outbound.assembly_complete():
            raise ValidationError(
                "pull run finished without matching the planned consist",
                **{"assembled": outbound.assembled_car_codes},
            )
        run.state = RunState.COMPLETED
        run.completed_at = now_iso()
        transition_outbound(outbound, OutboundState.READY)
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_COMPLETED,
                f"pull run {run_code} completed",
                {
                    "assembled_car_codes": list(outbound.assembled_car_codes),
                    "steps": len(run.steps),
                },
            )
        )
    else:
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_ADVANCED,
                f"pull run {run_code} advanced {executed} steps",
                {
                    "current_step": run.current_step,
                    "remaining": run.remaining(),
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


def depart_outbound(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    details = parse_departure_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state == OutboundState.DEPARTED:
        raise ConflictError(
            "outbound train has already departed",
            outbound_code=outbound_code,
            departed_at=outbound.departed_at,
        )
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
    if details.departed_at is not None:
        departed_at = details.departed_at
        completed_at = _run_completed_at(workspace, outbound)
        if completed_at and not is_after_or_equal(departed_at, completed_at):
            raise ValidationError(
                "departed_at must not be earlier than assembly completion",
                fields={"departed_at": [f"must be at or after {completed_at}"]},
            )
        skew = (parse_iso(departed_at) - parse_iso(now_iso())).total_seconds()
        if skew > MAX_DEPARTURE_CLOCK_SKEW_SECONDS:
            raise ValidationError(
                "departed_at is too far in the future",
                fields={"departed_at": [f"must be within {MAX_DEPARTURE_CLOCK_SKEW_SECONDS} seconds of server time"]},
            )
    else:
        departed_at = now_iso()
    transition_outbound(outbound, OutboundState.DEPARTED)
    outbound.departed_at = departed_at
    outbound.note = details.note
    outbound.late_reason = details.late_reason
    outbound.confirmed_by = details.confirmed_by
    for code in outbound.assembled_car_codes:
        transition_car(workspace.cars[code], CarState.DEPARTED)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_DEPARTED,
        f"outbound {outbound.code} departed for {outbound.destination}",
        {
            "car_count": len(outbound.assembled_car_codes),
            "departed_at": departed_at,
            "note": outbound.note,
            "late_reason": outbound.late_reason,
            "confirmed_by": outbound.confirmed_by,
        },
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "departed_car_count": len(outbound.assembled_car_codes),
    }


def _run_completed_at(workspace: Any, outbound: Any) -> str:
    for run_code in reversed(outbound.run_codes):
        run = workspace.runs.get(run_code)
        if run is not None and run.state == RunState.COMPLETED and run.completed_at:
            return run.completed_at
    return ""


__all__ = ["advance_run", "depart_outbound"]
