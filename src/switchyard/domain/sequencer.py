"""LIFO-aware pull plan derivation."""

from __future__ import annotations

from dataclasses import dataclass

from .car import FreightCar
from .enums import CarState, MoveVerb, OutboundState
from .errors import StateTransitionError, ValidationError
from .outbound import OutboundTrain
from .pull import MoveStep, PullRun
from .track import BufferBay, StandingTrack
from .transfer import TransferSelection, select_transfer
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


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """A verb/car/source triple derived before any transfer line is chosen."""

    verb: MoveVerb
    car_code: str
    source_code: str


def _simulate_pull(
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
) -> tuple[list[PlannedAction], int]:
    """Validate the LIFO stacks and derive actions, independent of the bay.

    Returns every planned action (buffer/pull/return in execution order) and
    the peak number of cars that must sit on the transfer line at once.
    """
    planned = list(outbound.planned_car_codes)
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
    actions: list[PlannedAction] = []
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
            actions.append(PlannedAction(MoveVerb.BUFFER, blocker, source_code))
        actions.append(PlannedAction(MoveVerb.PULL, code, source_code))
        for blocker in above:
            actions.append(PlannedAction(MoveVerb.RETURN, blocker, source_code))
        del stack[bottom_index]
    return actions, max_blockers


def _build_steps(actions: list[PlannedAction], outbound: OutboundTrain, transfer_code: str) -> list[MoveStep]:
    steps: list[MoveStep] = []
    for action in actions:
        if action.verb == MoveVerb.BUFFER:
            steps.append(MoveStep(MoveVerb.BUFFER, action.car_code, action.source_code, transfer_code))
        elif action.verb == MoveVerb.PULL:
            steps.append(MoveStep(MoveVerb.PULL, action.car_code, action.source_code, outbound.code))
        else:
            steps.append(MoveStep(MoveVerb.RETURN, action.car_code, transfer_code, action.source_code))
    return steps


def plan_pull_run(
    run_code: str,
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
    transfer_code: str | None,
    runs: dict[str, PullRun] | None = None,
) -> tuple[PullRun, TransferSelection]:
    if outbound.state != OutboundState.DRAFT:
        raise StateTransitionError("outbound train", str(outbound.state), "PLANNED", "already has a plan")
    planned = list(outbound.planned_car_codes)
    if not planned:
        raise ValidationError("outbound train has no planned cars", **{"car_codes": ["must not be empty"]})
    active_runs = runs or {}
    actions, required_cars = _simulate_pull(outbound, cars, tracks)
    selection = select_transfer(transfer_code, required_cars, transfer_bays, active_runs)
    steps = _build_steps(actions, outbound, selection.transfer_code)
    run = PullRun(
        code=run_code,
        outbound_code=outbound.code,
        transfer_code=selection.transfer_code,
        steps=steps,
        required_cars=required_cars,
        transfer_capacity_cars=selection.capacity_cars,
        transfer_available_cars=selection.available_cars,
        transfer_mode=selection.mode,
        selection_reason=selection.reason,
    )
    for code in planned:
        transition_car(cars[code], CarState.RESERVED)
    transition_outbound(outbound, OutboundState.PLANNED)
    outbound.run_codes.append(run.code)
    return run, selection


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
