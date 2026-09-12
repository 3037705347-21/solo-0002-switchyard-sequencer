"""Pull dispatch board: queue registration, resource claims, and conflict arbitration.

A dispatch ticket registers a queued pull plan in queue order and declares the
yard resources the plan occupies while it is pending or executing:

- source standing tracks (physical track is taken when cars are buffered/pulled),
- the single transfer bay X1 (only declared when the plan buffers blocker cars),
- individual blocker cars that must be parked in X1 during the pull,
- the planned target cars (kept reserved until completion or cancellation).

Tickets move QUEUED -> CLAIMED -> RUNNING -> COMPLETED, or to CANCELLED before
execution starts. The claim token granted to a client is persisted so the
execution right survives a service restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import MoveVerb, TicketState
from .pull import PullRun
from .timeutil import now_iso

ACTIVE_TICKET_STATES = {
    TicketState.QUEUED,
    TicketState.CLAIMED,
    TicketState.RUNNING,
}


@dataclass(slots=True)
class DispatchTicket:
    code: str
    queue_order: int
    outbound_code: str
    run_code: str
    source_tracks: list[str] = field(default_factory=list)
    transfer_bays: list[str] = field(default_factory=list)
    buffer_car_codes: list[str] = field(default_factory=list)
    target_car_codes: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    state: TicketState = TicketState.QUEUED
    claimed_by: str | None = None
    claim_token: str | None = None
    created_at: str = field(default_factory=now_iso)
    claimed_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "queue_order": self.queue_order,
            "outbound_code": self.outbound_code,
            "run_code": self.run_code,
            "source_tracks": list(self.source_tracks),
            "transfer_bays": list(self.transfer_bays),
            "buffer_car_codes": list(self.buffer_car_codes),
            "target_car_codes": list(self.target_car_codes),
            "resources": list(self.resources),
            "state": str(self.state),
            "claimed_by": self.claimed_by,
            "claim_token": self.claim_token,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cancelled_at": self.cancelled_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "DispatchTicket":
        def text(key: str) -> str | None:
            value = raw.get(key)
            return None if value is None else str(value)

        return cls(
            code=str(raw["code"]),
            queue_order=int(raw["queue_order"]),
            outbound_code=str(raw["outbound_code"]),
            run_code=str(raw["run_code"]),
            source_tracks=[str(item) for item in raw.get("source_tracks", [])],
            transfer_bays=[str(item) for item in raw.get("transfer_bays", [])],
            buffer_car_codes=[str(item) for item in raw.get("buffer_car_codes", [])],
            target_car_codes=[str(item) for item in raw.get("target_car_codes", [])],
            resources=[str(item) for item in raw.get("resources", [])],
            state=TicketState.parse(str(raw.get("state", TicketState.QUEUED.value))),
            claimed_by=text("claimed_by"),
            claim_token=text("claim_token"),
            created_at=str(raw.get("created_at", "")),
            claimed_at=text("claimed_at"),
            started_at=text("started_at"),
            completed_at=text("completed_at"),
            cancelled_at=text("cancelled_at"),
        )

    def is_active(self) -> bool:
        return self.state in ACTIVE_TICKET_STATES

    def has_begun(self) -> bool:
        """True once crew actions have moved cars (cancellation forbidden)."""
        return self.state == TicketState.RUNNING or self.state == TicketState.COMPLETED

    def public_view(self) -> dict[str, Any]:
        """Board-safe serialization without the execution token."""
        data = self.to_dict()
        data.pop("claim_token", None)
        return data


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def declared_resources(run: PullRun) -> dict[str, list[str]]:
    """Derive the resource declaration of a pull run from its move steps."""
    return declared_resources_from_steps(run.steps)


def build_resources(declaration: dict[str, list[str]]) -> list[str]:
    resources: list[str] = []
    resources.extend(f"track:{code}" for code in declaration["source_tracks"])
    resources.extend(f"bay:{code}" for code in declaration["transfer_bays"])
    resources.extend(f"car:{code}" for code in declaration["buffer_car_codes"])
    return resources


def release_actions_for(steps: list[MoveStep], transfer_code: str) -> list[dict[str, Any]]:
    """Steps that hand declared yard resources back to the board."""
    actions: list[dict[str, Any]] = []
    if any(step.verb == MoveVerb.BUFFER for step in steps):
        last_return = max(index for index, step in enumerate(steps) if step.verb == MoveVerb.RETURN)
        actions.append(
            {
                "resource": f"bay:{transfer_code}",
                "action": f"final RETURN into transfer bay {transfer_code}",
                "step_index": last_return,
                "step_total": len(steps),
            }
        )
    last_track_step: dict[str, tuple[int, MoveVerb]] = {}
    for index, step in enumerate(steps):
        if step.verb in {MoveVerb.BUFFER, MoveVerb.PULL}:
            last_track_step[step.source_code] = (index, step.verb)
        elif step.verb == MoveVerb.RETURN:
            last_track_step[step.target_code] = (index, step.verb)
    for track_code, (index, verb) in last_track_step.items():
        if verb == MoveVerb.RETURN:
            action = f"final RETURN clears source track {track_code}"
        else:
            action = f"final PULL clears source track {track_code}"
        actions.append(
            {
                "resource": f"track:{track_code}",
                "action": action,
                "step_index": index,
                "step_total": len(steps),
            }
        )
    for index, step in enumerate(steps):
        if step.verb == MoveVerb.RETURN:
            actions.append(
                {
                    "resource": f"car:{step.car_code}",
                    "action": f"RETURN {step.car_code} from {transfer_code} to {step.target_code}",
                    "step_index": index,
                    "step_total": len(steps),
                }
            )
    actions.sort(key=lambda item: (int(item["step_index"]), str(item["resource"])))
    return actions


def declaration_for(steps: list[MoveStep], target_codes: list[str]) -> dict[str, list[str]]:
    """Resource declaration implied by a step list (targets stay reserved)."""
    derived = declared_resources_from_steps(steps)
    # Keep the original target list even if a target is pulled in a prior step
    # of the same multi-car plan.
    derived["target_car_codes"] = _ordered_unique(list(target_codes))
    return derived


def declared_resources_from_steps(steps: list[MoveStep]) -> dict[str, list[str]]:
    tracks: list[str] = []
    bays: list[str] = []
    buffer_cars: list[str] = []
    target_cars: list[str] = []
    for step in steps:
        if step.verb == MoveVerb.BUFFER:
            tracks.append(step.source_code)
            bays.append(step.target_code)
            buffer_cars.append(step.car_code)
        elif step.verb == MoveVerb.PULL:
            tracks.append(step.source_code)
            target_cars.append(step.car_code)
    return {
        "source_tracks": _ordered_unique(tracks),
        "transfer_bays": _ordered_unique(bays),
        "buffer_car_codes": _ordered_unique(buffer_cars),
        "target_car_codes": _ordered_unique(target_cars),
    }


def evaluate_ticket(
    ticket: DispatchTicket,
    active_tickets: list[DispatchTicket],
    resources: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return blocker entries; an empty list means the ticket is eligible.

    `resources` overrides the ticket's stored declaration with a set derived
    from the current track stacks; this prevents stale stored resources (for
    example a transfer bay no longer needed) from blocking a claim.
    """
    mine = set(ticket.resources) if resources is None else set(resources)
    blockers: list[dict[str, Any]] = []
    for other in active_tickets:
        if other.code == ticket.code or other.queue_order >= ticket.queue_order:
            continue
        overlapping = sorted(mine.intersection(other.resources))
        if not overlapping:
            continue
        reason = "ahead-in-queue" if other.state == TicketState.QUEUED else "resource-held"
        blockers.append(
            {
                "ticket_code": other.code,
                "outbound_code": other.outbound_code,
                "state": str(other.state),
                "reason": reason,
                "resources": overlapping,
            }
        )
    return blockers


def resource_board(active_tickets: list[DispatchTicket]) -> list[dict[str, Any]]:
    holders: dict[str, dict[str, Any]] = {}
    waiting: dict[str, list[str]] = {}
    for ticket in sorted(active_tickets, key=lambda item: item.queue_order):
        for resource in ticket.resources:
            entry = holders.get(resource)
            if entry is None:
                holders[resource] = {
                    "resource": resource,
                    "held_by": ticket.code,
                    "state": str(ticket.state),
                }
            else:
                waiting.setdefault(resource, []).append(ticket.code)
    rows = [holders[key] for key in sorted(holders)]
    for row in rows:
        row["waiting_tickets"] = waiting.get(row["resource"], [])
    return rows


def describe_ticket(
    ticket: DispatchTicket,
    active_tickets: list[DispatchTicket],
    run: PullRun | None,
    preview_steps: list[MoveStep] | None = None,
    preview_resources: set[str] | None = None,
) -> dict[str, Any]:
    """Public ticket view with eligibility, blockers, and release actions.

    `preview_steps`/`preview_resources` (when derivable from the current yard)
    are preferred over the stored values so the dispatcher sees release points
    and blocking after blockers have been pulled away by earlier tickets.
    """
    data = ticket.public_view()
    blockers = (
        evaluate_ticket(ticket, active_tickets, resources=preview_resources)
        if ticket.is_active()
        else []
    )
    data["eligible"] = not blockers and ticket.state == TicketState.QUEUED
    data["blocked_by"] = blockers
    if run is None:
        data["release_actions"] = []
    else:
        steps = preview_steps if preview_steps is not None else run.steps
        data["release_actions"] = release_actions_for(steps, run.transfer_code)
        data["plan_stale"] = [step.to_dict() for step in steps] != [
            step.to_dict() for step in run.steps
        ]
    return data


__all__ = [
    "ACTIVE_TICKET_STATES",
    "DispatchTicket",
    "build_resources",
    "declaration_for",
    "declared_resources",
    "declared_resources_from_steps",
    "describe_ticket",
    "evaluate_ticket",
    "release_actions_for",
    "resource_board",
]
