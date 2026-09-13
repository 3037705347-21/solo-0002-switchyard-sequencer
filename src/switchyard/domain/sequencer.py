"""LIFO-aware pull plan derivation.

The module has two halves built on the same logic:

- ``simulate_pull`` is a pure, side-effect-free simulation of the LIFO
  buffer/pull/return moves. It never raises for planning conflicts and never
  touches car or train state, so both the formal planner and pull plan trials
  derive their feasibility verdict from the exact same code path.
- ``plan_pull_run`` is the formal entry: it runs the simulation, keeps the
  original first-failure error behaviour, and only then reserves cars and
  advances the outbound train state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .car import FreightCar
from .enums import CarState, MoveVerb, OutboundState
from .errors import StateTransitionError, ValidationError
from .outbound import OutboundTrain
from .pull import MoveStep, PullRun
from .track import BufferBay, StandingTrack
from .transitions import transition_car, transition_outbound


@dataclass(slots=True)
class PlanningFailure:
    code: str
    message: str
    car_code: str | None = None
    track_code: str | None = None


@dataclass(slots=True)
class BlockedTarget:
    """A planned car that is not on top of its source stack."""

    car_code: str
    track_code: str
    depth_from_top: int
    blockers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PullSimulation:
    steps: list[MoveStep] = field(default_factory=list)
    failures: list[PlanningFailure] = field(default_factory=list)
    blocked_targets: list[BlockedTarget] = field(default_factory=list)
    assembly_order: list[str] = field(default_factory=list)
    max_transfer_occupancy: int = 0
    transfer_capacity: int | None = None

    @property
    def feasible(self) -> bool:
        return not self.failures

    @property
    def buffer_move_count(self) -> int:
        return sum(1 for step in self.steps if step.verb == MoveVerb.BUFFER)


def _planning_failure(code: str, message: str, car_code: str | None = None, track_code: str | None = None) -> PlanningFailure:
    return PlanningFailure(code=code, message=message, car_code=car_code, track_code=track_code)


def simulate_pull(
    car_codes: list[str],
    destination: str,
    outbound_code: str,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
    transfer_code: str,
    owned_car_codes: set[str] | frozenset[str] = frozenset(),
) -> PullSimulation:
    """Derive buffer/pull/return moves without mutating anything.

    ``owned_car_codes`` marks cars already owned by the plan being simulated.
    A baseline replay uses it so its own reserved cars count as available
    instead of as "not standing" conflicts.
    """
    planned = list(car_codes)
    planned_set = set(planned)
    owned = set(owned_car_codes)
    simulation = PullSimulation()
    transfer = transfer_bays.get(transfer_code)
    if transfer is None:
        simulation.transfer_capacity = None
        simulation.failures.append(
            _planning_failure(
                "transfer-missing",
                f"transfer bay {transfer_code} not found",
                track_code=transfer_code,
            )
        )
    else:
        simulation.transfer_capacity = transfer.capacity_cars
    working_stacks: dict[str, list[str]] = {}
    source_of: dict[str, str] = {}
    for code in planned:
        car = cars.get(code)
        if car is None:
            simulation.failures.append(_planning_failure("car-missing", f"car {code} does not exist", code))
            continue
        if car.state != CarState.STANDING and code not in owned:
            simulation.failures.append(_planning_failure("car-not-standing", f"car {code} is not standing", code))
            continue
        if car.destination != destination:
            simulation.failures.append(
                _planning_failure(
                    "destination-mismatch",
                    f"car {code} is not for destination {destination}",
                    code,
                )
            )
            continue
        location = car.location
        if location is None or location not in tracks:
            simulation.failures.append(
                _planning_failure("car-not-on-track", f"car {code} has no standing location", code)
            )
            continue
        stack = working_stacks.setdefault(location, list(tracks[location].stack))
        if code not in stack:
            simulation.failures.append(
                _planning_failure("car-not-in-stack", f"car {code} is not in track {location}", code, location)
            )
            continue
        source_of[code] = location
    for code in planned:
        source_code = source_of.get(code)
        if source_code is None:
            continue
        stack = working_stacks[source_code]
        bottom_index = stack.index(code)
        above = stack[bottom_index + 1 :]
        if above:
            simulation.blocked_targets.append(
                BlockedTarget(
                    car_code=code,
                    track_code=source_code,
                    depth_from_top=len(above),
                    blockers=list(reversed(above)),
                )
            )
        simulation.max_transfer_occupancy = max(simulation.max_transfer_occupancy, len(above))
        buffered: list[str] = []
        for blocker in reversed(above):
            blocker_car = cars.get(blocker)
            if blocker_car is None:
                simulation.failures.append(
                    _planning_failure("blocker-missing", f"blocker {blocker} is missing", blocker, source_code)
                )
                continue
            if blocker_car.state != CarState.STANDING and blocker not in owned:
                simulation.failures.append(
                    _planning_failure(
                        "blocker-reserved",
                        f"car {blocker} is reserved elsewhere and cannot be buffered",
                        blocker,
                        source_code,
                    )
                )
                continue
            if blocker in planned_set:
                simulation.failures.append(
                    _planning_failure(
                        "blocked-sequence",
                        f"car {blocker} must be pulled before {code}",
                        blocker,
                        source_code,
                    )
                )
                continue
            buffered.append(blocker)
            if transfer is not None:
                simulation.steps.append(
                    MoveStep(MoveVerb.BUFFER, blocker, source_code, transfer.code)
                )
        simulation.steps.append(MoveStep(MoveVerb.PULL, code, source_code, outbound_code))
        if transfer is not None:
            for blocker in above:
                if blocker in buffered:
                    simulation.steps.append(
                        MoveStep(MoveVerb.RETURN, blocker, transfer.code, source_code)
                    )
        del stack[bottom_index]
        simulation.assembly_order.append(code)
    if transfer is not None and simulation.max_transfer_occupancy > transfer.capacity_cars:
        simulation.failures.append(
            _planning_failure(
                "buffer-overflow",
                f"transfer bay {transfer.code} needs {simulation.max_transfer_occupancy} slots "
                f"but has {transfer.capacity_cars}",
                track_code=transfer.code,
            )
        )
    return simulation


def plan_pull_run(
    run_code: str,
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
    transfer_code: str,
) -> PullRun:
    if outbound.state != OutboundState.DRAFT:
        raise StateTransitionError("outbound train", str(outbound.state), "PLANNED", "already has a plan")
    planned = list(outbound.planned_car_codes)
    if not planned:
        raise ValidationError("outbound train has no planned cars", **{"car_codes": ["must not be empty"]})
    if transfer_code not in transfer_bays:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})
    simulation = simulate_pull(
        planned,
        outbound.destination,
        outbound.code,
        cars,
        tracks,
        transfer_bays,
        transfer_code,
    )
    if simulation.failures:
        first = simulation.failures[0]
        raise ValidationError(first.message, **{"sequencer": [first.code]})
    run = PullRun(
        code=run_code,
        outbound_code=outbound.code,
        transfer_code=transfer_code,
        steps=simulation.steps,
    )
    for code in planned:
        transition_car(cars[code], CarState.RESERVED)
    transition_outbound(outbound, OutboundState.PLANNED)
    outbound.run_codes.append(run.code)
    return run


def can_sequence(
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
) -> list[str]:
    failures: list[str] = []
    planned_set = set(outbound.planned_car_codes)
    for code in outbound.planned_car_codes:
        car = cars.get(code)
        if car is None or car.state != CarState.STANDING or car.destination != outbound.destination:
            failures.append(f"{code}: unavailable for {outbound.destination}")
            continue
        location = car.location
        if location not in tracks or code not in tracks[location].stack:
            failures.append(f"{code}: not stacked on a standing track")
    if not failures and transfer_bays:
        for code in outbound.planned_car_codes:
            stack = tracks[str(cars[code].location)].stack
            blockers = stack[stack.index(code) + 1 :]
            if any(blocker in planned_set for blocker in blockers):
                failures.append(f"{code}: blocked by a later planned car")
    return failures


__all__ = [
    "BlockedTarget",
    "PlanningFailure",
    "PullSimulation",
    "can_sequence",
    "plan_pull_run",
    "simulate_pull",
]
