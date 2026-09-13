"""Pull run advancement, retry, and outbound departure commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, DomainError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.executor import execute_step
from ..domain.pull import MoveStep
from ..domain.sequencer import plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_car, transition_outbound, transition_run
from ..domain.undo import release_run_reservation, rollback_run_attempt
from ..domain.validators import parse_advance_steps, parse_transfer_code
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before moving cars")


def _step_dicts(steps: list[MoveStep]) -> list[dict[str, str]]:
    return [step.to_dict() for step in steps]


def _latest_run_for(workspace: Any, outbound_code: str) -> Any:
    matches = [run for run in workspace.runs.values() if run.outbound_code == outbound_code]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (item.attempt, item.code))[-1]


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
    outbound = workspace.outbounds.get(run.outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", run.outbound_code)
    events: list[Any] = []
    if run.state == RunState.QUEUED:
        transition_run(run, RunState.RUNNING)
        run.started_at = now_iso()
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_STARTED,
                f"pull run {run_code} started",
                {
                    "run_code": run_code,
                    "outbound_code": run.outbound_code,
                    "attempt": run.attempt,
                    "total_steps": len(run.steps),
                },
            )
        )
    step_before = run.current_step
    executed_steps: list[MoveStep] = []
    failure: DomainError | None = None
    try:
        while len(executed_steps) < requested_steps and run.current_step < len(run.steps):
            step = run.steps[run.current_step]
            execute_step(workspace, run, step)
            executed_steps.append(step)
            run.current_step += 1
        if run.current_step >= len(run.steps) and not outbound.assembly_complete():
            raise ValidationError(
                "pull run finished without matching the planned consist",
                **{"assembled": outbound.assembled_car_codes},
            )
    except DomainError as exc:
        failure = exc
    if failure is not None:
        rollback_run_attempt(workspace, run, executed_steps, step_before)
        run.current_step = step_before
        transition_run(run, RunState.FAILED)
        run.failed_at = now_iso()
        run.error = failure.message
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_FAILED,
                f"pull run {run_code} failed after {len(executed_steps)} step(s): {failure.message}",
                {
                    "run_code": run_code,
                    "outbound_code": run.outbound_code,
                    "attempt": run.attempt,
                    "executed_steps": _step_dicts(executed_steps),
                    "buffer_count": sum(1 for step in executed_steps if str(step.verb) == "BUFFER"),
                    "return_count": sum(1 for step in executed_steps if str(step.verb) == "RETURN"),
                    "pull_count": sum(1 for step in executed_steps if str(step.verb) == "PULL"),
                    "failed_at": run.failed_at,
                    "error": failure.message,
                },
            )
        )
        app.commit(workspace, events)
        raise failure
    executed = len(executed_steps)
    completed = run.current_step >= len(run.steps)
    if completed:
        run.state = RunState.COMPLETED
        run.completed_at = now_iso()
        transition_outbound(outbound, OutboundState.READY)
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_COMPLETED,
                f"pull run {run_code} completed",
                {
                    "run_code": run_code,
                    "outbound_code": run.outbound_code,
                    "attempt": run.attempt,
                    "assembled_car_codes": list(outbound.assembled_car_codes),
                    "steps": len(run.steps),
                    "executed_steps": _step_dicts(run.steps),
                    "buffer_count": sum(1 for step in run.steps if str(step.verb) == "BUFFER"),
                    "return_count": sum(1 for step in run.steps if str(step.verb) == "RETURN"),
                    "pull_count": sum(1 for step in run.steps if str(step.verb) == "PULL"),
                    "started_at": run.started_at,
                    "completed_at": run.completed_at,
                },
            )
        )
    else:
        segment = run.steps[step_before : run.current_step]
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_ADVANCED,
                f"pull run {run_code} advanced {executed} steps",
                {
                    "run_code": run_code,
                    "outbound_code": run.outbound_code,
                    "attempt": run.attempt,
                    "current_step": run.current_step,
                    "remaining": run.remaining(),
                    "executed_steps": _step_dicts(segment),
                    "buffer_count": sum(1 for step in segment if str(step.verb) == "BUFFER"),
                    "return_count": sum(1 for step in segment if str(step.verb) == "RETURN"),
                    "pull_count": sum(1 for step in segment if str(step.verb) == "PULL"),
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


def retry_run(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    transfer_code = parse_transfer_code(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    latest = _latest_run_for(workspace, outbound_code)
    if latest is None or latest.state != RunState.FAILED:
        raise ValidationError(
            "a retry can only replace the latest failed pull run",
            **{"outbound_code": [f"latest run state is {latest.state.value if latest else 'NONE'}"]},
        )
    failed = latest
    attempt = failed.attempt + 1
    run_code = f"RUN-{outbound.code}-R{attempt}"
    if run_code in workspace.runs:
        raise ConflictError("pull run already exists", code=run_code)
    if transfer_code not in workspace.buffer_bays:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})
    # Only the first failed attempt leaves the outbound drafted; a failure
    # after committed advances keeps the run reservation and partial
    # assembly, which is released here before re-planning.
    release_run_reservation(workspace, failed)
    run = plan_pull_run(
        run_code,
        outbound,
        workspace.cars,
        workspace.tracks,
        workspace.buffer_bays,
        transfer_code,
        attempt=attempt,
    )
    workspace.runs[run.code] = run
    event = workspace.record_event(
        shift_code,
        EventKind.PULL_RUN_RETRIED,
        f"pull run {run.code} replaces failed attempt {failed.code} for {outbound.code}",
        {
            "run_code": run.code,
            "outbound_code": outbound.code,
            "attempt": attempt,
            "failed_run_code": failed.code,
            "failed_attempt": failed.attempt,
            "steps": len(run.steps),
            "transfer_code": transfer_code,
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
    car_codes = list(outbound.assembled_car_codes)
    for code in car_codes:
        transition_car(workspace.cars[code], CarState.DEPARTED)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_DEPARTED,
        f"outbound {outbound.code} departed for {outbound.destination}",
        {
            "outbound_code": outbound.code,
            "destination": outbound.destination,
            "car_count": len(car_codes),
            "car_codes": car_codes,
            "departed_at": departed_at,
        },
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "departed_car_count": len(car_codes),
    }


__all__ = ["advance_run", "depart_outbound", "retry_run"]
