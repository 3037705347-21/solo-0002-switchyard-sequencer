"""Pull run advancement and outbound departure commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.executor import execute_step
from ..domain.timeutil import now_iso
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
    with app.update() as workspace:
        shift_code = _ensure_shift_open(workspace)
        run = workspace.runs.get(run_code)
        if run is None:
            raise NotFoundError("pull run", run_code)
        if run.state == RunState.COMPLETED:
            raise ConflictError("pull run is already complete", code=run_code)
        if run.state == RunState.FAILED:
            raise ConflictError("pull run has failed", code=run_code)
        if run.state == RunState.QUEUED:
            transition_run(run, RunState.RUNNING)
            run.started_at = now_iso()
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_STARTED,
                f"pull run {run_code} started",
                {
                    "total_steps": len(run.steps),
                    "shift_code": shift_code,
                    "run_code": run.code,
                    "outbound_code": run.outbound_code,
                },
            )
        executed = 0
        executed_steps: list[dict[str, str]] = []
        touched_cars: list[str] = []
        while executed < requested_steps and run.current_step < len(run.steps):
            step = run.steps[run.current_step]
            execute_step(workspace, run, step)
            executed_steps.append(step.to_dict())
            if step.car_code not in touched_cars:
                touched_cars.append(step.car_code)
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
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_COMPLETED,
                f"pull run {run_code} completed",
                {
                    "assembled_car_codes": list(outbound.assembled_car_codes),
                    "steps": len(run.steps),
                    "shift_code": shift_code,
                    "run_code": run.code,
                    "outbound_code": run.outbound_code,
                },
            )
        else:
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_ADVANCED,
                f"pull run {run_code} advanced {executed} steps",
                {
                    "current_step": run.current_step,
                    "remaining": run.remaining(),
                    "executed": executed,
                    "steps": executed_steps,
                    "car_codes": touched_cars,
                    "shift_code": shift_code,
                    "run_code": run.code,
                    "outbound_code": run.outbound_code,
                },
            )
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "executed_steps": executed,
        "completed": completed,
    }


def depart_outbound(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    with app.update() as workspace:
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
        workspace.record_event(
            shift_code,
            EventKind.TRAIN_DEPARTED,
            f"outbound {outbound.code} departed for {outbound.destination}",
            {
                "car_count": len(outbound.assembled_car_codes),
                "departed_at": departed_at,
                "shift_code": shift_code,
                "outbound_code": outbound.code,
                "car_codes": list(outbound.assembled_car_codes),
            },
        )
    return {
        "outbound": outbound.to_dict(),
        "departed_car_count": len(outbound.assembled_car_codes),
    }


__all__ = ["advance_run", "depart_outbound"]
