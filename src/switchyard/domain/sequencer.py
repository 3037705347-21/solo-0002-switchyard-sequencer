"""LIFO-aware pull plan derivation and safe planned-car replacement."""

from __future__ import annotations

from dataclasses import dataclass

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


def _fail(failure: PlanningFailure) -> None:
    raise ValidationError(failure.message, **{"sequencer": [failure.code]})


def _planning_failure(code: str, message: str, car_code: str | None = None, track_code: str | None = None) -> PlanningFailure:
    return PlanningFailure(code=code, message=message, car_code=car_code, track_code=track_code)


def _operational_track(tracks: dict[str, StandingTrack], code: str, car_code: str | None = None) -> StandingTrack:
    track = tracks.get(code)
    if track is None:
        _fail(_planning_failure("car-not-on-track", f"track {code} does not exist", car_code, code))
    if not track.can_operate():
        _fail(
            _planning_failure(
                "track-unavailable",
                f"track {code} is {track.state.value} and cannot be used as a pull source",
                car_code,
                code,
            )
        )
    return track


def derive_pull_steps(
    outbound: OutboundTrain,
    target_codes: list[str],
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer: BufferBay,
    *,
    pending_target_code: str | None = None,
    released_codes: set[str] | frozenset[str] = frozenset(),
    active_buffers: list[tuple[str, str]] | None = None,
    retained_buffer_count: int = 0,
) -> list[MoveStep]:
    """Build pull steps without mutating cars, tracks, or the transfer bay.

    ``active_buffers`` are buffered cars belonging to this run, ordered from the
    bay bottom to its top. They are returned first. ``retained_buffer_count`` is
    the number of cars from other runs that must remain in the bay throughout
    the replacement suffix.
    """
    active_buffers = active_buffers or []
    if not target_codes:
        raise ValidationError("outbound train has no remaining planned cars", **{"car_codes": ["must not be empty"]})
    target_set = set(target_codes)
    working_stacks: dict[str, list[str]] = {code: list(track.stack) for code, track in tracks.items()}
    working_bay = ["<foreign>"] * retained_buffer_count
    prefix_steps: list[MoveStep] = []

    # Start with live foreign bay occupants, then this run's active buffers in
    # their recorded bottom-to-top order. The generated suffix pops those
    # buffers from the top first; executed BUFFER steps remain in run history.
    working_bay.extend(car_code for car_code, _source_code in active_buffers)
    for car_code, source_code in reversed(active_buffers):
        source = _operational_track(tracks, source_code, car_code)
        working_stacks[source.code].append(car_code)
        prefix_steps.append(MoveStep(MoveVerb.RETURN, car_code, transfer.code, source.code))
        working_bay.pop()

    source_of: dict[str, str] = {}
    for code in target_codes:
        car = cars.get(code)
        if car is None:
            _fail(_planning_failure("car-missing", f"car {code} does not exist", code))
        expected_state = CarState.STANDING if code == pending_target_code else CarState.RESERVED
        if pending_target_code is None:
            expected_state = CarState.STANDING
        if car.state != expected_state:
            _fail(_planning_failure("car-not-available", f"car {code} is {car.state.value}, not {expected_state.value}", code))
        if car.destination != outbound.destination:
            _fail(
                _planning_failure(
                    "destination-mismatch",
                    f"car {code} is not for destination {outbound.destination}",
                    code,
                )
            )
        location = car.location
        if location is None:
            _fail(_planning_failure("car-not-on-track", f"car {code} has no standing location", code))
        track = _operational_track(tracks, location, code)
        if code not in working_stacks[location]:
            _fail(_planning_failure("car-not-in-stack", f"car {code} is not in track {location}", code, location))
        source_of[code] = location

    steps: list[MoveStep] = list(prefix_steps)
    max_bay_occupancy = len(working_bay)
    for code in target_codes:
        source_code = source_of[code]
        stack = working_stacks[source_code]
        bottom_index = stack.index(code)
        above = stack[bottom_index + 1 :]
        for blocker in reversed(above):
            blocker_car = cars.get(blocker)
            if blocker_car is None:
                _fail(_planning_failure("blocker-missing", f"blocker {blocker} is missing", blocker, source_code))
            if blocker_car.state != CarState.STANDING and blocker not in released_codes:
                _fail(
                    _planning_failure(
                        "blocker-reserved",
                        f"car {blocker} is reserved elsewhere and cannot be buffered",
                        blocker,
                        source_code,
                    )
                )
            if blocker in target_set:
                _fail(
                    _planning_failure(
                        "blocked-sequence",
                        f"car {blocker} must be pulled before {code}",
                        blocker,
                        source_code,
                    )
                )
            steps.append(MoveStep(MoveVerb.BUFFER, blocker, source_code, transfer.code))
            working_bay.append(blocker)
            max_bay_occupancy = max(max_bay_occupancy, len(working_bay))
        steps.append(MoveStep(MoveVerb.PULL, code, source_code, outbound.code))
        for blocker in above:
            steps.append(MoveStep(MoveVerb.RETURN, blocker, transfer.code, source_code))
            working_bay.pop()
        del stack[bottom_index]

    if max_bay_occupancy > transfer.capacity_cars:
        _fail(
            _planning_failure(
                "buffer-overflow",
                f"transfer bay {transfer.code} needs {max_bay_occupancy} slots but has {transfer.capacity_cars}",
                track_code=transfer.code,
            )
        )
    return steps


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
    transfer = transfer_bays.get(transfer_code)
    if transfer is None:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})
    steps = derive_pull_steps(outbound, planned, cars, tracks, transfer)
    run = PullRun(code=run_code, outbound_code=outbound.code, transfer_code=transfer.code, steps=steps)
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


__all__ = ["PlanningFailure", "can_sequence", "derive_pull_steps", "plan_pull_run"]
