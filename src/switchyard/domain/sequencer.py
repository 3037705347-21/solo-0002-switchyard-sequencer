"""LIFO-aware pull plan derivation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .car import FreightCar
from .enums import CarState, MoveVerb, OutboundState
from .errors import StateTransitionError, ValidationError
from .outbound import OutboundTrain
from .pull import MoveStep, PullRun
from .timeutil import now_iso
from .track import BufferBay, StandingTrack
from .transitions import transition_car, transition_outbound


@dataclass(slots=True)
class PlanningFailure:
    code: str
    message: str
    car_code: str | None = None
    track_code: str | None = None


def _fail(failure: PlanningFailure) -> None:
    raise ValidationError(failure.message, **{"sequencer": [failure.code]})


def _planning_failure(code: str, message: str, car_code: str | None = None, track_code: str | None = None) -> PlanningFailure:
    return PlanningFailure(code=code, message=message, car_code=car_code, track_code=track_code)


def plan_pull_run(
    run_code: str,
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
    transfer_code: str,
    attempt: int = 1,
) -> PullRun:
    if outbound.state != OutboundState.DRAFT:
        raise StateTransitionError("outbound train", str(outbound.state), "PLANNED", "already has a plan")
    planned = list(outbound.planned_car_codes)
    if not planned:
        raise ValidationError("outbound train has no planned cars", **{"car_codes": ["must not be empty"]})
    planned_set = set(planned)
    transfer = transfer_bays.get(transfer_code)
    if transfer is None:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})
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
            steps.append(MoveStep(MoveVerb.BUFFER, blocker, source_code, transfer.code))
        steps.append(MoveStep(MoveVerb.PULL, code, source_code, outbound.code))
        for blocker in above:
            steps.append(MoveStep(MoveVerb.RETURN, blocker, transfer.code, source_code))
        del stack[bottom_index]
    if max_blockers > transfer.capacity_cars:
        _fail(
            _planning_failure(
                "buffer-overflow",
                f"transfer bay {transfer.code} needs {max_blockers} slots but has {transfer.capacity_cars}",
                track_code=transfer.code,
            )
        )
    run = PullRun(
        code=run_code,
        outbound_code=outbound.code,
        transfer_code=transfer.code,
        steps=steps,
        attempt=attempt,
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


__all__ = ["PlanningFailure", "can_sequence", "plan_pull_run"]
