"""Pull run advancement and outbound departure commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState, TicketState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.executor import execute_step
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_car, transition_outbound, transition_run, transition_ticket
from ..domain.validators import parse_advance_steps
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before moving cars")


def _claim_token(payload: Any) -> str | None:
    body = payload if isinstance(payload, dict) else {}
    value = body.get("claim_token")
    return None if value is None else str(value).strip() or None


def _authorize_ticket(workspace: Any, run_code: str, token: str | None) -> Any | None:
    ticket = workspace.ticket_for_run(run_code)
    if ticket is None:
        return None  # legacy pull run created through the sequencer endpoint
    if ticket.state == TicketState.CANCELLED:
        raise ConflictError(f"dispatch ticket {ticket.code} was cancelled", code=ticket.code)
    if ticket.state == TicketState.COMPLETED:
        return ticket
    if not token:
        raise ResourceBusyError(
            f"pull run {run_code} requires dispatch ticket {ticket.code} claim token",
            ticket_code=ticket.code,
        )
    if token != ticket.claim_token:
        raise ResourceBusyError(
            f"pull run {run_code} execution right belongs to another client",
            ticket_code=ticket.code,
            claimed_by=ticket.claimed_by,
        )
    return ticket


def advance_run(app: YardApplication, run_code: str, payload: Any) -> dict[str, Any]:
    requested_steps = parse_advance_steps(payload)
    token = _claim_token(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    ticket = _authorize_ticket(workspace, run_code, token)
    if run.state == RunState.CANCELLED:
        raise ConflictError("pull run was cancelled", code=run_code)
    if run.state == RunState.COMPLETED:
        raise ConflictError("pull run is already complete", code=run_code)
    if run.state == RunState.FAILED:
        raise ConflictError("pull run has failed", code=run_code)
    events: list[Any] = []
    if run.state == RunState.QUEUED:
        transition_run(run, RunState.RUNNING)
        run.started_at = now_iso()
        if ticket is not None:
            transition_ticket(ticket, TicketState.RUNNING)
            ticket.started_at = run.started_at
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_STARTED,
                f"pull run {run_code} started",
                {"total_steps": len(run.steps), "ticket_code": None if ticket is None else ticket.code},
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
        if ticket is not None:
            transition_ticket(ticket, TicketState.COMPLETED)
            ticket.completed_at = run.completed_at
            ticket.claim_token = None
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_COMPLETED,
                f"pull run {run_code} completed",
                {
                    "assembled_car_codes": list(outbound.assembled_car_codes),
                    "steps": len(run.steps),
                    "ticket_code": None if ticket is None else ticket.code,
                },
            )
        )
        if ticket is not None:
            events.append(
                workspace.record_event(
                    shift_code,
                    EventKind.DISPATCH_COMPLETED,
                    f"dispatch ticket {ticket.code} released all declared resources",
                    {"ticket_code": ticket.code, "run_code": run.code},
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
        "ticket": None if ticket is None else ticket.public_view(),
    }


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


__all__ = ["advance_run", "depart_outbound"]
