"""Pull dispatch board commands: register, claim, cancel, and board views."""

from __future__ import annotations

import secrets
from typing import Any

from ..domain.dispatch import (
    DispatchTicket,
    build_resources,
    declared_resources,
    describe_ticket,
    evaluate_ticket,
    resource_board,
)
from ..domain.enums import CarState, EventKind, OutboundState, RunState, TicketState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, StateTransitionError
from ..domain.sequencer import plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.transitions import (
    transition_car,
    transition_outbound,
    transition_run,
    transition_ticket,
)
from ..domain.validators import (
    build_dispatch_claim,
    build_dispatch_register,
    parse_claim_token,
    parse_client_id,
)
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before pulling")


def _ticket(workspace: Any, ticket_code: str) -> DispatchTicket:
    ticket = workspace.dispatch_tickets.get(ticket_code)
    if ticket is None:
        raise NotFoundError("dispatch ticket", ticket_code)
    return ticket


def _active_ticket_for_outbound(workspace: Any, outbound_code: str) -> DispatchTicket | None:
    for ticket in workspace.dispatch_tickets.values():
        if ticket.outbound_code == outbound_code and ticket.is_active():
            return ticket
    return None


def _view(workspace: Any, ticket: DispatchTicket) -> dict[str, Any]:
    run = workspace.runs.get(ticket.run_code)
    active = sorted(workspace.active_tickets(), key=lambda item: item.queue_order)
    return describe_ticket(ticket, active, run)


def register_dispatch(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    transfer_code, client_id = build_dispatch_register(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if transfer_code not in workspace.buffer_bays:
        raise ResourceBusyError(f"transfer bay {transfer_code} does not exist", transfer_code=transfer_code)
    if outbound.state.value != "DRAFT":
        raise StateTransitionError(
            "outbound train",
            str(outbound.state),
            "PLANNED",
            "only draft trains can register at the dispatch board",
        )
    order = workspace.next_dispatch_order
    ticket_code = f"TKT-{order:04d}"
    run_code = f"RUN-{outbound.code}-{order}"
    if run_code in workspace.runs:
        raise ConflictError("pull run already exists", code=run_code)
    run = plan_pull_run(
        run_code,
        outbound,
        workspace.cars,
        workspace.tracks,
        workspace.buffer_bays,
        transfer_code,
        # A blocker reserved by an earlier queued ticket is a normal board
        # conflict: arbitration decides order instead of rejecting the queue.
        allow_reserved_blockers=True,
        # Blocker cars stay STANDING; the ticket's declared car resources
        # protect them so the leading ticket can still buffer them.
        reserve_only_targets=True,
    )
    declaration = declared_resources(run)
    ticket = DispatchTicket(
        code=ticket_code,
        queue_order=order,
        outbound_code=outbound.code,
        run_code=run.code,
        source_tracks=declaration["source_tracks"],
        transfer_bays=declaration["transfer_bays"],
        buffer_car_codes=declaration["buffer_car_codes"],
        target_car_codes=declaration["target_car_codes"],
        resources=build_resources(declaration),
    )
    workspace.runs[run.code] = run
    workspace.dispatch_tickets[ticket.code] = ticket
    workspace.next_dispatch_order = order + 1
    event = workspace.record_event(
        shift_code,
        EventKind.DISPATCH_REGISTERED,
        f"dispatch ticket {ticket.code} queued for {outbound.code} at order {order}",
        {
            "ticket_code": ticket.code,
            "run_code": run.code,
            "queue_order": order,
            "client_id": client_id,
            "resources": list(ticket.resources),
            "steps": len(run.steps),
        },
    )
    app.commit(workspace, event)
    return {"ticket": _view(workspace, ticket), "pull_run": run.to_dict()}


def claim_ticket(app: YardApplication, ticket_code: str, payload: Any) -> dict[str, Any]:
    client_id, retried_token = build_dispatch_claim(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    ticket = _ticket(workspace, ticket_code)
    if ticket.state in {TicketState.CANCELLED, TicketState.COMPLETED}:
        raise ConflictError(f"dispatch ticket {ticket.code} is {ticket.state.value}", code=ticket.code)
    if ticket.state in {TicketState.CLAIMED, TicketState.RUNNING}:
        if ticket.claimed_by == client_id and retried_token and retried_token == ticket.claim_token:
            run = workspace.runs.get(ticket.run_code)
            return {
                "ticket": _view(workspace, ticket),
                "pull_run": run.to_dict() if run is not None else None,
                "claim_token": ticket.claim_token,
                "idempotent": True,
            }
        raise ResourceBusyError(
            f"dispatch ticket {ticket.code} is already claimed by another client",
            ticket_code=ticket.code,
            claimed_by=ticket.claimed_by,
        )
    active = sorted(workspace.active_tickets(), key=lambda item: item.queue_order)
    blockers = evaluate_ticket(ticket, active)
    if blockers:
        raise ResourceBusyError(
            f"dispatch ticket {ticket.code} is blocked by {len(blockers)} earlier ticket(s)",
            ticket_code=ticket.code,
            blockers=blockers,
        )
    token = secrets.token_hex(16)
    transition_ticket(ticket, TicketState.CLAIMED)
    ticket.claimed_by = client_id
    ticket.claim_token = token
    ticket.claimed_at = now_iso()
    event = workspace.record_event(
        shift_code,
        EventKind.DISPATCH_CLAIMED,
        f"dispatch ticket {ticket.code} claimed by {client_id}",
        {"ticket_code": ticket.code, "run_code": ticket.run_code, "client_id": client_id},
    )
    app.commit(workspace, event)
    run = workspace.runs.get(ticket.run_code)
    return {
        "ticket": _view(workspace, ticket),
        "pull_run": run.to_dict() if run is not None else None,
        "claim_token": token,
        "idempotent": False,
    }


def cancel_ticket(app: YardApplication, ticket_code: str, payload: Any) -> dict[str, Any]:
    client_id = parse_client_id(payload, field_name="client_id", required=True)
    token = parse_claim_token(payload, required=False)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    ticket = _ticket(workspace, ticket_code)
    if ticket.state == TicketState.CANCELLED:
        raise ConflictError(f"dispatch ticket {ticket.code} is already cancelled", code=ticket.code)
    if ticket.state == TicketState.COMPLETED:
        raise ConflictError(f"dispatch ticket {ticket.code} is completed", code=ticket.code)
    if ticket.state == TicketState.RUNNING:
        raise StateTransitionError(
            "dispatch ticket",
            ticket.state.value,
            "CANCELLED",
            "execution has begun; only unstarted tickets can be cancelled",
        )
    if ticket.state == TicketState.CLAIMED:
        if ticket.claimed_by != client_id or not token or token != ticket.claim_token:
            raise ResourceBusyError(
                f"dispatch ticket {ticket.code} belongs to another client",
                ticket_code=ticket.code,
                claimed_by=ticket.claimed_by,
            )
    _release_ticket(workspace, ticket)
    event = workspace.record_event(
        shift_code,
        EventKind.DISPATCH_CANCELLED,
        f"dispatch ticket {ticket.code} cancelled by {client_id}",
        {"ticket_code": ticket.code, "run_code": ticket.run_code, "client_id": client_id},
    )
    app.commit(workspace, event)
    return {"ticket": ticket.public_view()}


def _release_ticket(workspace: Any, ticket: DispatchTicket) -> None:
    run = workspace.runs.get(ticket.run_code)
    if run is not None and run.state != RunState.CANCELLED:
        transition_run(run, RunState.CANCELLED)
    outbound = workspace.outbounds.get(ticket.outbound_code)
    if outbound is not None and outbound.state == OutboundState.PLANNED:
        transition_outbound(outbound, OutboundState.DRAFT)
    for car_code in ticket.target_car_codes:
        car = workspace.cars.get(car_code)
        if car is not None and car.state == CarState.RESERVED:
            transition_car(car, CarState.STANDING)
    transition_ticket(ticket, TicketState.CANCELLED)
    ticket.cancelled_at = now_iso()
    ticket.claim_token = None


def dispatch_board(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    active = sorted(workspace.active_tickets(), key=lambda item: item.queue_order)
    tickets = [describe_ticket(ticket, active, workspace.runs.get(ticket.run_code)) for ticket in active]
    queue_codes = [ticket["code"] for ticket in tickets]
    return {
        "queue_order": queue_codes,
        "tickets": tickets,
        "resources": resource_board(active),
        "waiting": [ticket["code"] for ticket in tickets if ticket["blocked_by"]],
        "eligible": [ticket["code"] for ticket in tickets if ticket["eligible"]],
    }


def get_ticket(app: YardApplication, ticket_code: str) -> dict[str, Any]:
    workspace = app.load()
    ticket = _ticket(workspace, ticket_code)
    active = sorted(workspace.active_tickets(), key=lambda item: item.queue_order)
    view = describe_ticket(ticket, active, workspace.runs.get(ticket.run_code))
    return {"ticket": view}


__all__ = [
    "cancel_ticket",
    "claim_ticket",
    "dispatch_board",
    "get_ticket",
    "register_dispatch",
]
