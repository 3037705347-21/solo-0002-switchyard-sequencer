"""What-if pull plan trials.

A pull trial evaluates a *candidate* outbound consist against a detached
snapshot of the current yard. It answers the dispatcher's planning question
("which candidate list needs fewer reversals, and which one is blocked by a
reservation or by transfer capacity?") without reserving any car, creating a
pull run, or recording an event.

Trials can also be compared with the current formal plan for the destination:
the report splits conflicts into ``new`` (raised by the candidate but absent
from the formal plan) and ``resolved`` (present in the formal plan but gone).

Feasibility is derived by :func:`switchyard.domain.sequencer.simulate_pull`,
the exact same simulation core the formal planner uses, so a trial's verdict
matches what real planning would decide on the same snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .enums import CarState, OutboundState
from .sequencer import PlanningFailure, PullSimulation, simulate_pull

# Active plans whose cars count as formally occupied.
ACTIVE_PLAN_STATES = frozenset({OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.READY})
# Plans that can act as a formal baseline. READY trains are already assembled.
BASELINE_PLAN_STATES = frozenset({OutboundState.DRAFT, OutboundState.PLANNED})


@dataclass(slots=True)
class TrialConflict:
    code: str
    message: str
    car_code: str | None = None
    track_code: str | None = None
    owner_outbound: str | None = None

    @property
    def signature(self) -> tuple[str, str | None]:
        return self.code, self.car_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "car_code": self.car_code,
            "track_code": self.track_code,
            "owner_outbound": self.owner_outbound,
        }


@dataclass(slots=True)
class PullTrialResult:
    candidate_code: str
    destination: str
    transfer_code: str
    feasible: bool
    needs_reverse: bool
    reverse_count: int
    buffer_move_count: int
    max_transfer_occupancy: int
    transfer_capacity: int | None
    final_assembly_order: list[str]
    blocked_cars: list[dict[str, Any]]
    steps: list[dict[str, str]]
    conflicts: list[TrialConflict]
    compared_to: str | None
    baseline_conflicts: list[TrialConflict]
    new_conflicts: list[TrialConflict]
    resolved_conflicts: list[TrialConflict]
    snapshot_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_code": self.candidate_code,
            "destination": self.destination,
            "transfer_code": self.transfer_code,
            "feasible": self.feasible,
            "needs_reverse": self.needs_reverse,
            "reverse_count": self.reverse_count,
            "buffer_move_count": self.buffer_move_count,
            "max_transfer_occupancy": self.max_transfer_occupancy,
            "transfer_capacity": self.transfer_capacity,
            "final_assembly_order": list(self.final_assembly_order),
            "blocked_cars": [dict(item) for item in self.blocked_cars],
            "steps": [dict(step) for step in self.steps],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "compared_to": self.compared_to,
            "baseline_conflicts": [conflict.to_dict() for conflict in self.baseline_conflicts],
            "new_conflicts": [conflict.to_dict() for conflict in self.new_conflicts],
            "resolved_conflicts": [conflict.to_dict() for conflict in self.resolved_conflicts],
            "snapshot_version": self.snapshot_version,
        }


def occupied_car_owners(workspace: Any) -> dict[str, str]:
    """Map every car held by an active plan to the owning outbound code.

    Formal creation rejects reuse of cars on DRAFT/PLANNED/READY trains, and
    formal sequencing reserves the same set, so the map covers both standing
    cars drafted by another plan and cars already reserved/assembled.
    """
    owners: dict[str, str] = {}
    for outbound in workspace.outbounds.values():
        if outbound.state in ACTIVE_PLAN_STATES:
            for code in outbound.planned_car_codes:
                owners.setdefault(code, outbound.code)
    return owners


def choose_baseline(workspace: Any, destination: str) -> Any | None:
    """Pick the current formal plan for a destination, if one is active.

    The workspace keeps outbounds in creation order, so the earliest active
    plan for the destination is the original formal plan and wins
    deterministically.
    """
    for train in workspace.outbounds.values():
        if train.destination == destination and train.state in BASELINE_PLAN_STATES:
            return train
    return None


def _failure_to_conflict(failure: PlanningFailure, owner: str | None) -> TrialConflict:
    return TrialConflict(
        code=failure.code,
        message=failure.message,
        car_code=failure.car_code,
        track_code=failure.track_code,
        owner_outbound=owner,
    )


def evaluate_candidate(
    car_codes: list[str],
    destination: str,
    outbound_code: str,
    workspace: Any,
    transfer_code: str,
    occupied_owners: dict[str, str],
) -> tuple[PullSimulation, list[TrialConflict]]:
    """Run the shared simulation and layer formal-plan occupancy checks on top."""
    simulation = simulate_pull(
        car_codes,
        destination,
        outbound_code,
        workspace.cars,
        workspace.tracks,
        workspace.buffer_bays,
        transfer_code,
    )
    conflicts: list[TrialConflict] = []
    for failure in simulation.failures:
        owner = occupied_owners.get(failure.car_code) if failure.car_code else None
        if failure.code == "car-not-standing" and failure.car_code in occupied_owners:
            conflicts.append(
                TrialConflict(
                    code="car-occupied",
                    message=(
                        f"car {failure.car_code} is already planned on outbound train "
                        f"{occupied_owners[failure.car_code]}"
                    ),
                    car_code=failure.car_code,
                    track_code=failure.track_code,
                    owner_outbound=occupied_owners[failure.car_code],
                )
            )
        else:
            conflicts.append(_failure_to_conflict(failure, owner))
    for code in car_codes:
        car = workspace.cars.get(code)
        if car is not None and car.state == CarState.STANDING and code in occupied_owners:
            owner = occupied_owners[code]
            conflicts.append(
                TrialConflict(
                    code="car-occupied",
                    message=f"car {code} is already planned on outbound train {owner}",
                    car_code=code,
                    track_code=car.location,
                    owner_outbound=owner,
                )
            )
    return simulation, conflicts


def _blocked_cars_payload(simulation: PullSimulation) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for target in simulation.blocked_targets:
        payload.append(
            {
                "car_code": target.car_code,
                "track_code": target.track_code,
                "depth_from_top": target.depth_from_top,
                "blocker_car_codes": list(target.blockers),
            }
        )
    return payload


def _baseline_conflicts(
    workspace: Any,
    baseline: Any,
    transfer_code: str,
) -> list[TrialConflict]:
    """Replay the formal plan's own consist as it stands on the snapshot."""
    simulation = simulate_pull(
        baseline.planned_car_codes,
        baseline.destination,
        baseline.code,
        workspace.cars,
        workspace.tracks,
        workspace.buffer_bays,
        transfer_code,
        owned_car_codes=set(baseline.planned_car_codes),
    )
    owners = occupied_car_owners(workspace)
    return [
        _failure_to_conflict(failure, owners.get(failure.car_code) if failure.car_code else None)
        for failure in simulation.failures
    ]


def evaluate_pull_trial(
    workspace: Any,
    candidate_code: str,
    destination: str,
    car_codes: list[str],
    transfer_code: str,
    baseline: Any | None = None,
    snapshot_version: int | None = None,
) -> PullTrialResult:
    """Evaluate one candidate list against a detached yard snapshot."""
    owners = occupied_car_owners(workspace)
    simulation, conflicts = evaluate_candidate(
        car_codes,
        destination,
        candidate_code,
        workspace,
        transfer_code,
        owners,
    )
    feasible = not conflicts
    baseline_conflicts: list[TrialConflict] = []
    compared_to: str | None = None
    if baseline is not None:
        compared_to = baseline.code
        baseline_conflicts = _baseline_conflicts(workspace, baseline, transfer_code)
    baseline_signatures = {conflict.signature for conflict in baseline_conflicts}
    candidate_signatures = {conflict.signature for conflict in conflicts}
    new_conflicts = [
        conflict for conflict in conflicts if conflict.signature not in baseline_signatures
    ]
    resolved_conflicts = [
        conflict for conflict in baseline_conflicts if conflict.signature not in candidate_signatures
    ]
    return PullTrialResult(
        candidate_code=candidate_code,
        destination=destination,
        transfer_code=transfer_code,
        feasible=feasible,
        needs_reverse=simulation.buffer_move_count > 0,
        reverse_count=simulation.buffer_move_count,
        buffer_move_count=simulation.buffer_move_count,
        max_transfer_occupancy=simulation.max_transfer_occupancy,
        transfer_capacity=simulation.transfer_capacity,
        final_assembly_order=list(simulation.assembly_order),
        blocked_cars=_blocked_cars_payload(simulation),
        steps=[step.to_dict() for step in simulation.steps],
        conflicts=conflicts,
        compared_to=compared_to,
        baseline_conflicts=baseline_conflicts,
        new_conflicts=new_conflicts,
        resolved_conflicts=resolved_conflicts,
        snapshot_version=workspace.version if snapshot_version is None else snapshot_version,
    )


__all__ = [
    "PullTrialResult",
    "TrialConflict",
    "choose_baseline",
    "evaluate_candidate",
    "evaluate_pull_trial",
    "occupied_car_owners",
]
