"""LIFO-aware pull plan derivation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .car import FreightCar
from .enums import CarState, MoveVerb, OutboundState
from .errors import PlanValidationError, StateTransitionError, ValidationError
from .intake import IntakeTrain
from .outbound import OutboundTrain
from .pull import MoveStep, PullRun
from .track import BufferBay, StandingTrack
from .transitions import transition_car, transition_outbound

# Disposition categories attached to every planning failure. They tell the
# dispatcher which kind of remedy applies without the service touching the
# plan itself.
CATEGORY_SWAP_CAR = "swap-car"
CATEGORY_ACT_FIRST = "act-first"
CATEGORY_FIX_RESOURCE = "fix-resource"


@dataclass(slots=True)
class PlanningFailure:
    car_code: str | None
    reason: str
    category: str
    message: str
    car_state: str | None = None
    location: str | None = None
    held_by: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    conflict_with: str | None = None
    suggestion_action: str = ""
    suggestion_target: str | None = None
    suggestion_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "car_code": self.car_code,
            "reason": self.reason,
            "category": self.category,
            "message": self.message,
            "car_state": self.car_state,
            "location": self.location,
            "held_by": self.held_by,
            "blocked_by": list(self.blocked_by),
            "conflict_with": self.conflict_with,
            "suggestion": {
                "action": self.suggestion_action or self.category,
                "target_code": self.suggestion_target,
                "note": self.suggestion_note,
            },
        }


def _holder_of(
    car_code: str,
    outbounds: dict[str, OutboundTrain] | None,
    exclude: str | None = None,
) -> str | None:
    """Return the planned or ready outbound train currently holding a car."""
    if not outbounds:
        return None
    for code in sorted(outbounds):
        if code == exclude:
            continue
        train = outbounds[code]
        if train.state not in {OutboundState.PLANNED, OutboundState.READY}:
            continue
        if car_code in train.planned_car_codes or car_code in train.assembled_car_codes:
            return train.code
    return None


def _intake_of(car_code: str, intakes: dict[str, IntakeTrain] | None) -> str | None:
    """Return the open intake train whose consist still holds a car."""
    if not intakes:
        return None
    for code in sorted(intakes):
        train = intakes[code]
        if not train.is_terminal() and car_code in train.consist:
            return train.code
    return None


def _replacement_candidate(
    destination: str,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    planned_set: set[str],
) -> str | None:
    """Pick a deterministic standing car that could take the failed slot."""
    candidates: list[str] = []
    for code, car in cars.items():
        if car.state != CarState.STANDING or car.destination != destination:
            continue
        if code in planned_set:
            continue
        track = tracks.get(str(car.location))
        if track is None or not track.can_operate():
            continue
        candidates.append(code)
    return sorted(candidates)[0] if candidates else None


def _swap_note(destination: str, candidate: str | None) -> str:
    if candidate is None:
        return f"no standing car is available for {destination}; classify or free one first"
    return f"swap the plan entry for standing car {candidate}"


def _car_failure(
    code: str,
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    planned_set: set[str],
    outbounds: dict[str, OutboundTrain] | None,
    intakes: dict[str, IntakeTrain] | None,
) -> PlanningFailure | None:
    """First-pass eligibility check for one planned car."""
    car = cars.get(code)
    if car is None:
        candidate = _replacement_candidate(outbound.destination, cars, tracks, planned_set)
        return PlanningFailure(
            car_code=code,
            reason="car-missing",
            category=CATEGORY_SWAP_CAR,
            message=f"car {code} does not exist in the yard",
            suggestion_action=CATEGORY_SWAP_CAR,
            suggestion_target=candidate,
            suggestion_note=_swap_note(outbound.destination, candidate),
        )
    if car.state != CarState.STANDING:
        return _not_standing_failure(car, outbound, cars, tracks, planned_set, outbounds, intakes)
    if car.destination != outbound.destination:
        candidate = _replacement_candidate(outbound.destination, cars, tracks, planned_set)
        return PlanningFailure(
            car_code=code,
            reason="destination-mismatch",
            category=CATEGORY_SWAP_CAR,
            message=f"car {code} is for {car.destination}, not {outbound.destination}",
            car_state=str(car.state),
            location=car.location,
            suggestion_action=CATEGORY_SWAP_CAR,
            suggestion_target=candidate,
            suggestion_note=_swap_note(outbound.destination, candidate),
        )
    location = car.location
    if location is None or location not in tracks:
        return PlanningFailure(
            car_code=code,
            reason="car-not-on-track",
            category=CATEGORY_FIX_RESOURCE,
            message=f"car {code} is standing but has no known spot location",
            car_state=str(car.state),
            location=location,
            suggestion_action=CATEGORY_FIX_RESOURCE,
            suggestion_target=location,
            suggestion_note=f"inspect and fix the spot record for {code} before planning",
        )
    track = tracks[location]
    if code not in track.stack:
        return PlanningFailure(
            car_code=code,
            reason="car-not-in-stack",
            category=CATEGORY_FIX_RESOURCE,
            message=f"car {code} is recorded on {location} but is not in the track stack",
            car_state=str(car.state),
            location=location,
            suggestion_action=CATEGORY_FIX_RESOURCE,
            suggestion_target=location,
            suggestion_note=f"reconcile the stack of track {location} with the spot record of {code}",
        )
    if not track.can_operate():
        return PlanningFailure(
            car_code=code,
            reason="track-not-operational",
            category=CATEGORY_FIX_RESOURCE,
            message=f"track {location} is {track.state.value} and cannot be a pull source",
            car_state=str(car.state),
            location=location,
            suggestion_action=CATEGORY_FIX_RESOURCE,
            suggestion_target=location,
            suggestion_note=f"clear {track.state.value.lower()} on track {location} or move {code} first",
        )
    return None


def _not_standing_failure(
    car: FreightCar,
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    planned_set: set[str],
    outbounds: dict[str, OutboundTrain] | None,
    intakes: dict[str, IntakeTrain] | None,
) -> PlanningFailure:
    state = car.state
    if state in {CarState.RESERVED, CarState.ASSEMBLED}:
        holder = _holder_of(car.code, outbounds, exclude=outbound.code)
        if holder is None and state == CarState.ASSEMBLED and car.location:
            holder = str(car.location)
        note = (
            f"complete or abandon outbound {holder} to release {car.code}"
            if holder
            else f"release {car.code} from its current assignment first"
        )
        return PlanningFailure(
            car_code=car.code,
            reason="car-not-standing",
            category=CATEGORY_ACT_FIRST,
            message=f"car {car.code} is {state.value} and held by {holder or 'another train'}",
            car_state=str(state),
            location=car.location,
            held_by=holder,
            suggestion_action=CATEGORY_ACT_FIRST,
            suggestion_target=holder,
            suggestion_note=note,
        )
    if state == CarState.RECEIVED:
        intake = _intake_of(car.code, intakes)
        note = (
            f"classify intake {intake} to spot {car.code} on a track"
            if intake
            else f"classify the intake holding {car.code} before planning"
        )
        return PlanningFailure(
            car_code=car.code,
            reason="car-not-standing",
            category=CATEGORY_ACT_FIRST,
            message=f"car {car.code} is still received and not classified",
            car_state=str(state),
            location=car.location,
            held_by=intake,
            suggestion_action=CATEGORY_ACT_FIRST,
            suggestion_target=intake,
            suggestion_note=note,
        )
    candidate = _replacement_candidate(outbound.destination, cars, tracks, planned_set)
    return PlanningFailure(
        car_code=car.code,
        reason="car-not-standing",
        category=CATEGORY_SWAP_CAR,
        message=f"car {car.code} is {state.value} and cannot be planned",
        car_state=str(state),
        location=car.location,
        suggestion_action=CATEGORY_SWAP_CAR,
        suggestion_target=candidate,
        suggestion_note=_swap_note(outbound.destination, candidate),
    )


def plan_pull_run(
    run_code: str,
    outbound: OutboundTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
    transfer_code: str,
    outbounds: dict[str, OutboundTrain] | None = None,
    intakes: dict[str, IntakeTrain] | None = None,
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
    failures: list[PlanningFailure] = []
    working_stacks: dict[str, list[str]] = {}
    source_of: dict[str, str] = {}
    seen: set[str] = set()
    for code in planned:
        if code in seen:
            candidate = _replacement_candidate(outbound.destination, cars, tracks, planned_set)
            failures.append(
                PlanningFailure(
                    car_code=code,
                    reason="car-duplicate",
                    category=CATEGORY_SWAP_CAR,
                    message=f"car {code} appears more than once in the plan",
                    suggestion_action=CATEGORY_SWAP_CAR,
                    suggestion_target=candidate,
                    suggestion_note=f"drop the duplicate entry; {_swap_note(outbound.destination, candidate)}",
                )
            )
            continue
        seen.add(code)
        failure = _car_failure(code, outbound, cars, tracks, planned_set, outbounds, intakes)
        if failure is not None:
            failures.append(failure)
            continue
        car = cars[code]
        location = str(car.location)
        working_stacks.setdefault(location, list(tracks[location].stack))
        source_of[code] = location
    steps: list[MoveStep] = []
    max_blockers = 0
    deepest_car: str | None = None
    deepest_blockers: list[str] = []
    for code in source_of:
        location = source_of[code]
        stack = working_stacks[location]
        bottom_index = stack.index(code)
        above = stack[bottom_index + 1 :]
        blockers_top_first = list(reversed(above))
        for blocker in blockers_top_first:
            blocker_car = cars.get(blocker)
            if blocker_car is None:
                failures.append(
                    PlanningFailure(
                        car_code=code,
                        reason="blocker-missing",
                        category=CATEGORY_FIX_RESOURCE,
                        message=f"stack of {location} references unknown car {blocker} above {code}",
                        car_state=str(cars[code].state),
                        location=location,
                        blocked_by=blockers_top_first,
                        conflict_with=blocker,
                        suggestion_action=CATEGORY_FIX_RESOURCE,
                        suggestion_target=location,
                        suggestion_note=f"reconcile the stack of track {location}; {blocker} has no car record",
                    )
                )
                continue
            if blocker_car.state != CarState.STANDING:
                holder = _holder_of(blocker, outbounds, exclude=outbound.code)
                failures.append(
                    PlanningFailure(
                        car_code=code,
                        reason="blocker-reserved",
                        category=CATEGORY_ACT_FIRST,
                        message=(
                            f"car {code} is buried under {blocker} on {location}, and {blocker} is "
                            f"{blocker_car.state.value} by {holder or 'another train'}"
                        ),
                        car_state=str(cars[code].state),
                        location=location,
                        blocked_by=blockers_top_first,
                        conflict_with=blocker,
                        suggestion_action=CATEGORY_ACT_FIRST,
                        suggestion_target=holder or blocker,
                        suggestion_note=(
                            f"complete or abandon outbound {holder} so {blocker} can be buffered"
                            if holder
                            else f"free {blocker} so it can be buffered out of the way"
                        ),
                    )
                )
                continue
            if blocker in planned_set:
                failures.append(
                    PlanningFailure(
                        car_code=code,
                        reason="blocked-sequence",
                        category=CATEGORY_ACT_FIRST,
                        message=f"car {code} is planned before {blocker} but sits below it on {location}",
                        car_state=str(cars[code].state),
                        location=location,
                        blocked_by=blockers_top_first,
                        conflict_with=blocker,
                        suggestion_action=CATEGORY_ACT_FIRST,
                        suggestion_target=blocker,
                        suggestion_note=f"pull {blocker} before {code}; move it earlier in the plan",
                    )
                )
                continue
            steps.append(MoveStep(MoveVerb.BUFFER, blocker, location, transfer.code))
        steps.append(MoveStep(MoveVerb.PULL, code, location, outbound.code))
        for blocker in above:
            steps.append(MoveStep(MoveVerb.RETURN, blocker, transfer.code, location))
        if len(above) > max_blockers:
            max_blockers = len(above)
            deepest_car = code
            deepest_blockers = blockers_top_first
        del stack[bottom_index]
    if max_blockers > transfer.capacity_cars:
        failures.append(
            PlanningFailure(
                car_code=deepest_car,
                reason="buffer-overflow",
                category=CATEGORY_FIX_RESOURCE,
                message=(
                    f"transfer bay {transfer.code} needs {max_blockers} slot(s) for {deepest_car} "
                    f"but has {transfer.capacity_cars}"
                ),
                car_state=str(cars[deepest_car].state) if deepest_car in cars else None,
                location=source_of.get(deepest_car) if deepest_car else None,
                blocked_by=deepest_blockers,
                suggestion_action=CATEGORY_FIX_RESOURCE,
                suggestion_target=transfer.code,
                suggestion_note=(
                    f"free or expand transfer bay {transfer.code}, or split the pull so at most "
                    f"{transfer.capacity_cars} car(s) are buffered"
                ),
            )
        )
    if failures:
        raise PlanValidationError(
            f"pull plan for {outbound.code} has {len(failures)} blocking problem(s)",
            [failure.to_dict() for failure in failures],
            outbound_code=outbound.code,
            transfer_code=transfer.code,
        )
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


__all__ = [
    "CATEGORY_ACT_FIRST",
    "CATEGORY_FIX_RESOURCE",
    "CATEGORY_SWAP_CAR",
    "PlanningFailure",
    "can_sequence",
    "plan_pull_run",
]
