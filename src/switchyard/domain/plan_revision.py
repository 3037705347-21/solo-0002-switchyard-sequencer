"""Draft outbound consist revision.

Allows a DRAFT outbound train's planned car sequence to be replaced (cars
added, removed, or reordered) before it is sequenced. The proposed consist is
validated against the same rules that govern creation and pull planning:

* every car exists, is standing, and matches the train destination;
* every car sits on an operational standing track (in-yard workable);
* no car is planned by another active outbound train;
* the ordered sequence is LIFO executable, meaning no planned car is blocked
  by a car planned later in the sequence, and every standing blocker could be
  buffered (a blocker reserved elsewhere cannot move).

This module is a pure check: it never mutates cars, tracks, or the train.
The service layer applies the new list only after the full check succeeds,
so a rejected revision leaves no reservation or half-applied state behind.
"""

from __future__ import annotations

from .car import FreightCar
from .enums import CarState, TrackState
from .errors import ResourceBusyError, ValidationError
from .track import StandingTrack


def validate_draft_consist(
    destination: str,
    car_codes: list[str],
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    occupied_car_codes: set[str],
) -> None:
    """Raise a domain error unless ``car_codes`` forms a workable draft plan.

    ``occupied_car_codes`` must already exclude the train being revised so its
    current members can be kept, replaced, or reordered.
    """
    if not car_codes:
        raise ValidationError("at least one planned car is required", **{"car_codes": ["must not be empty"]})
    planned_set = set(car_codes)
    source_of: dict[str, str] = {}
    for code in car_codes:
        car = cars.get(code)
        if car is None:
            raise ValidationError("planned cars do not exist", **{"car_codes": [code]})
        if car.state != CarState.STANDING:
            raise ValidationError(
                f"car {code} is not standing",
                **{"car_codes": [f"{code} is {car.state.value}"]},
            )
        if car.destination != destination:
            raise ValidationError(
                f"car {code} is for {car.destination}, not {destination}",
                **{"car_codes": [f"{code} destination mismatch"]},
            )
        location = car.location
        if location is None or location not in tracks or code not in tracks[location].stack:
            raise ValidationError(
                f"car {code} is not stacked on a standing track",
                **{"car_codes": [f"{code} has no stack location"]},
            )
        track = tracks[location]
        if track.state != TrackState.OPERATIONAL:
            raise ResourceBusyError(
                f"track {location} is {track.state.value.lower()} and cannot serve car {code}",
                car_code=code,
                track_code=location,
            )
        if code in occupied_car_codes:
            raise ResourceBusyError(
                f"car {code} is already planned on another outbound train",
                car_code=code,
            )
        source_of[code] = location
    _validate_lifo_order(car_codes, planned_set, source_of, cars, tracks)


def _validate_lifo_order(
    car_codes: list[str],
    planned_set: set[str],
    source_of: dict[str, str],
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
) -> None:
    """Simulate pulling in plan order from mutable per-track stack copies.

    Planned cars disappear from the working stack as they are pulled, so the
    "cars above" check must skip them: they are pulled in their own plan order,
    and the only ordering constraint between two planned cars is that a car
    physically above another must appear earlier in the plan.
    """
    working_stacks: dict[str, list[str]] = {}
    for code in car_codes:
        source_code = source_of[code]
        stack = working_stacks.setdefault(source_code, list(tracks[source_code].stack))
        if code not in stack:  # pragma: no cover - membership already established
            raise ValidationError(
                f"car {code} is not in track {source_code}",
                **{"car_codes": [code]},
            )
        bottom_index = stack.index(code)
        for blocker in stack[bottom_index + 1 :]:
            if blocker in planned_set:
                # Another planned car is still physically above this one, so it
                # would be pulled after code despite blocking it.
                raise ValidationError(
                    f"car {blocker} must be pulled before {code}",
                    **{"car_codes": [f"{code} blocked by later planned car {blocker}"]},
                )
            blocker_car = cars.get(blocker)
            if blocker_car is None:
                raise ValidationError(
                    f"blocker {blocker} is missing",
                    **{"car_codes": [blocker]},
                )
            if blocker_car.state != CarState.STANDING:
                raise ResourceBusyError(
                    f"car {blocker} is reserved elsewhere and cannot be buffered to reach {code}",
                    car_code=blocker,
                    track_code=source_code,
                )
        del stack[bottom_index]


__all__ = ["validate_draft_consist"]
