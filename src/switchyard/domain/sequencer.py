"""LIFO-aware pull plan derivation."""

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
class PlanShape:
    """Pure, state-free shape of a pull ticket.

    ``steps`` never reference a specific transfer line so the same ticket can
    be evaluated against every registered transfer line.
    """

    planned_codes: list[str]
    source_of: dict[str, str] = field(default_factory=dict)
    steps: list[MoveStep] = field(default_factory=list)
    peak_bay_occupancy: int = 0


def _fail(failure: PlanningFailure) -> None:
    raise ValidationError(failure.message, **{"sequencer": [failure.code]})


def _planning_failure(code: str, message: str, car_code: str | None = None, track_code: str | None = None) -> PlanningFailure:
    return PlanningFailure(code=code, message=message, car_code=car_code, track_code=track_code)


def derive_plan_shape(
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
) -> PlanShape:
    """Validate the ticket against standing state and derive its LIFO moves.

    Raises ``ValidationError`` for any car-level problem (missing car, not
    standing, destination mismatch, blocker reserved by another ticket). It
    never mutates car or outbound state.
    """
    if outbound.state != OutboundState.DRAFT:
        raise StateTransitionError(
            "outbound train", str(outbound.state), "PLANNED", "already has a plan"
        )
    planned = list(outbound.planned_car_codes)
    if not planned:
        raise ValidationError(
            "outbound train has no planned cars", **{"car_codes": ["must not be empty"]}
        )
    planned_set = set(planned)
    working_stacks: dict[str, list[str]] = {}
    source_of: dict[str, str] = {}
    for code in planned:
        car = cars.get(code)
        if car is None:
            _fail(_planning_failure("car-missing", f"car {code} does not exist", code))
        if car.state != CarState.STANDING:
            _fail(_planning_failure("car-not-standing", f"car {code} is not standing", code))
        if car.destination != outbound.destination:
            _fail(
                _planning_failure(
                    "destination-mismatch",
                    f"car {code} is not for destination {outbound.destination}",
                    code,
                )
            )
        location = car.location
        if location is None or location not in tracks:
            _fail(_planning_failure("car-not-on-track", f"car {code} has no standing location", code))
        stack = working_stacks.setdefault(location, list(tracks[location].stack))
        if code not in stack:
            _fail(_planning_failure("car-not-in-stack", f"car {code} is not in track {location}", code, location))
        source_of[code] = location
    steps: list[MoveStep] = []
    max_blockers = 0
    for code in planned:
        source_code = source_of[code]
        stack = working_stacks[source_code]
        bottom_index = stack.index(code)
        above = stack[bottom_index + 1 :]
        max_blockers = max(max_blockers, len(above))
        for blocker in reversed(above):
            blocker_car = cars.get(blocker)
            if blocker_car is None:
                _fail(_planning_failure("blocker-missing", f"blocker {blocker} is missing", blocker, source_code))
            if blocker_car.state != CarState.STANDING:
                _fail(
                    _planning_failure(
                        "blocker-reserved",
                        f"car {blocker} is reserved elsewhere and cannot be buffered",
                        blocker,
                        source_code,
                    )
                )
            if blocker in planned_set:
                _fail(
                    _planning_failure(
                        "blocked-sequence",
                        f"car {blocker} must be pulled before {code}",
                        blocker,
                        source_code,
                    )
                )
            steps.append(MoveStep(MoveVerb.BUFFER, blocker, source_code, ""))
        steps.append(MoveStep(MoveVerb.PULL, code, source_code, outbound.code))
        for blocker in above:
            steps.append(MoveStep(MoveVerb.RETURN, blocker, "", source_code))
        del stack[bottom_index]
    return PlanShape(
        planned_codes=planned,
        source_of=source_of,
        steps=steps,
        peak_bay_occupancy=max_blockers,
    )


def plan_pull_run(
    run_code: str,
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
    transfer_code: str,
) -> PullRun:
    shape = derive_plan_shape(outbound, cars, tracks)
    transfer = transfer_bays.get(transfer_code)
    if transfer is None:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})
    if not transfer.can_operate():
        raise ValidationError(
            f"transfer line {transfer.code} is {transfer.state.value.lower()}",
            **{"transfer_code": [f"line is {transfer.state.value.lower()}"]},
        )
    if shape.peak_bay_occupancy > transfer.capacity_cars:
        _fail(
            _planning_failure(
                "buffer-overflow",
                f"transfer bay {transfer.code} needs {shape.peak_bay_occupancy} slots but has {transfer.capacity_cars}",
                track_code=transfer.code,
            )
        )
    steps: list[MoveStep] = []
    for step in shape.steps:
        if step.verb == MoveVerb.BUFFER:
            steps.append(MoveStep(step.verb, step.car_code, step.source_code, transfer.code))
        elif step.verb == MoveVerb.RETURN:
            steps.append(MoveStep(step.verb, step.car_code, transfer.code, step.target_code))
        else:
            steps.append(MoveStep(step.verb, step.car_code, step.source_code, step.target_code))
    run = PullRun(code=run_code, outbound_code=outbound.code, transfer_code=transfer.code, steps=steps)
    for code in shape.planned_codes:
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
    "PlanShape",
    "PlanningFailure",
    "can_sequence",
    "derive_plan_shape",
    "plan_pull_run",
]
