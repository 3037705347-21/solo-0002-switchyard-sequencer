"""Fixed yard rules and compatibility helpers."""

from __future__ import annotations

from typing import Iterable

from .car import FreightCar
from .enums import CarKind, TrackPurpose, TrackState
from .track import StandingTrack

DESTINATION_CODES = ("N4", "E7", "S2", "W9")
HAZARD_CLASSES = ("NONE", "D1", "D2")
ALL_KINDS = {item.value for item in CarKind}
MAX_TRAIN_CONSIST = 20
MAX_CAR_LENGTH_M = 35
MIN_CAR_LENGTH_M = 8
MAX_PLANNED_CARS = 16
DEFAULT_BUFFER_CAPACITY = 10
TRANSFER_BAY_CODE = "X1"

# Stable forecast block reason codes shared by the forecast report and checks.
REASON_TRACK_MAINTENANCE = "TRACK_MAINTENANCE"
REASON_TRACK_RESTRICTED = "TRACK_RESTRICTED"
REASON_TRANSFER_PURPOSE = "TRANSFER_PURPOSE"
REASON_DESTINATION_MISMATCH = "DESTINATION_MISMATCH"
REASON_KIND_NOT_ALLOWED = "KIND_NOT_ALLOWED"
REASON_HAZARD_NOT_RATED = "HAZARD_NOT_RATED"
REASON_HAZARD_UNAVAILABLE = "HAZARD_TRACK_UNAVAILABLE"
REASON_HAZARD_CAPACITY = "HAZARD_CAPACITY_FULL"
REASON_CAR_CAPACITY = "CAR_CAPACITY_FULL"
REASON_LENGTH_CAPACITY = "LENGTH_CAPACITY_FULL"
REASON_NO_CANDIDATE = "NO_CANDIDATE_TRACK"
REASON_AFTER_HORIZON = "AFTER_SHIFT_HORIZON"
REASON_BEFORE_OPEN = "BEFORE_SHIFT_OPEN"

REASON_MESSAGES = {
    REASON_TRACK_MAINTENANCE: "eligible track is in maintenance",
    REASON_TRACK_RESTRICTED: "eligible track is restricted",
    REASON_TRANSFER_PURPOSE: "eligible track is assigned to transfer duty",
    REASON_DESTINATION_MISMATCH: "no track serves this destination",
    REASON_KIND_NOT_ALLOWED: "no track accepts this car kind",
    REASON_HAZARD_NOT_RATED: "yard has no hazard-rated track",
    REASON_HAZARD_UNAVAILABLE: "hazard-rated track cannot receive cars this shift",
    REASON_HAZARD_CAPACITY: "hazard-rated track has no remaining capacity",
    REASON_CAR_CAPACITY: "all eligible tracks are at car capacity",
    REASON_LENGTH_CAPACITY: "all eligible tracks are at length capacity",
    REASON_NO_CANDIDATE: "no candidate track can receive the car",
    REASON_AFTER_HORIZON: "train arrives after the shift horizon",
    REASON_BEFORE_OPEN: "train arrives before the shift opens",
}

# Structural reasons beat capacity reasons; order inside is reporting priority.
# Restriction/transfer reflect arrangements made for this shift, so they are
# surfaced ahead of a track that is permanently in maintenance in the seed yard.
_STRUCTURAL_PRIORITY = (
    REASON_TRACK_RESTRICTED,
    REASON_TRANSFER_PURPOSE,
    REASON_TRACK_MAINTENANCE,
    REASON_KIND_NOT_ALLOWED,
    REASON_HAZARD_NOT_RATED,
    REASON_DESTINATION_MISMATCH,
)


def normalize_destination(value: str) -> str:
    normalized = value.strip().upper()
    return normalized


def destination_known(value: str) -> bool:
    return normalize_destination(value) in DESTINATION_CODES


def kind_known(value: str) -> bool:
    return value.strip().upper() in ALL_KINDS


def hazard_known(value: str) -> bool:
    return value.strip().upper() in HAZARD_CLASSES


def is_car_code(value: str) -> bool:
    text = value.strip().upper()
    if not text.startswith("C-"):
        return False
    if len(text) < 6 or len(text) > 24:
        return False
    return all(char.isalnum() or char in {"-", "_"} for char in text)


def is_entity_code(value: str, prefix: str | None = None) -> bool:
    text = value.strip().upper()
    if prefix and not text.startswith(prefix + "-"):
        return False
    if not text:
        return False
    return all(char.isalnum() or char in {"-", "_"} for char in text)


def stack_occupancy(track: StandingTrack, cars: dict[str, FreightCar]) -> tuple[int, int]:
    count = len(track.stack)
    length = 0
    for code in track.stack:
        car = cars.get(code)
        if car is not None:
            length += car.length_m
    return count, length


def kind_allowed(track: StandingTrack, car: FreightCar) -> bool:
    if not track.allowed_kinds:
        return True
    return car.kind in {CarKind.parse(item) for item in track.allowed_kinds}


def hazard_allowed(track: StandingTrack, car: FreightCar) -> bool:
    if not car.is_hazardous():
        return True
    return track.hazard_rated


def destination_allowed(track: StandingTrack, car: FreightCar) -> bool:
    if track.purpose == TrackPurpose.DESTINATION:
        return track.destination == car.destination
    return track.purpose == TrackPurpose.GENERAL


def track_receives_car(
    track: StandingTrack,
    car: FreightCar,
    cars: dict[str, FreightCar],
) -> str | None:
    if track.state != TrackState.OPERATIONAL:
        return f"track {track.code} is {track.state.value}"
    if not destination_allowed(track, car):
        return f"track {track.code} rejects destination {car.destination}"
    if not kind_allowed(track, car):
        return f"track {track.code} rejects kind {car.kind.value}"
    if not hazard_allowed(track, car):
        return f"track {track.code} is not hazard rated"
    count, length = stack_occupancy(track, cars)
    if count + 1 > track.capacity_cars:
        return f"track {track.code} is at car capacity"
    if length + car.length_m > track.capacity_length_m:
        return f"track {track.code} is at length capacity"
    return None


def candidate_tracks_for(car: FreightCar, cars: dict[str, FreightCar], tracks: Iterable[StandingTrack]) -> list[StandingTrack]:
    result: list[StandingTrack] = []
    for track in tracks:
        if track_receives_car(track, car, cars) is None:
            result.append(track)
    return result


def ranked_candidate_tracks(
    car: FreightCar,
    cars: dict[str, FreightCar],
    tracks: Iterable[StandingTrack],
) -> list[StandingTrack]:
    """Pick order shared by real classification and the read-only forecast.

    Destination tracks come before general tracks; within each group the track
    with the most remaining space wins, ties breaking deterministically by code.
    """

    candidates = candidate_tracks_for(car, cars, tracks)
    destination = [item for item in candidates if item.purpose == TrackPurpose.DESTINATION]
    general = [item for item in candidates if item.purpose == TrackPurpose.GENERAL]
    destination.sort(key=lambda item: (-remaining_capacity_score(item, cars), item.code))
    general.sort(key=lambda item: (-remaining_capacity_score(item, cars), item.code))
    return destination + general


def _structural_reason(track: StandingTrack) -> str | None:
    if track.state == TrackState.MAINTENANCE:
        return REASON_TRACK_MAINTENANCE
    if track.state == TrackState.RESTRICTED:
        return REASON_TRACK_RESTRICTED
    if track.purpose == TrackPurpose.TRANSFER:
        return REASON_TRANSFER_PURPOSE
    return None


def _has_headroom(track: StandingTrack, car: FreightCar, cars: dict[str, FreightCar]) -> bool:
    count, length = stack_occupancy(track, cars)
    if count + 1 > track.capacity_cars:
        return False
    return length + car.length_m <= track.capacity_length_m


def _capacity_block(track: StandingTrack, car: FreightCar, cars: dict[str, FreightCar]) -> str:
    count, _length = stack_occupancy(track, cars)
    if count + 1 > track.capacity_cars:
        return REASON_CAR_CAPACITY
    return REASON_LENGTH_CAPACITY


def _routing_tracks(car: FreightCar, tracks: list[StandingTrack]) -> tuple[list[StandingTrack], bool]:
    """Tracks whose fixed attributes (purpose, affinity, kind, hazard rating)
    could serve the car. Transfer-duty tracks are kept (they would normally
    classify but have been reassigned this shift); maintenance/restriction state
    is evaluated separately because it can change within a shift.
    Returns (routing tracks, kind_only_block)."""

    hazardous = car.is_hazardous()
    routing: list[StandingTrack] = []
    kind_blocked = False
    for track in tracks:
        if hazardous and not track.hazard_rated:
            continue
        if track.purpose == TrackPurpose.DESTINATION:
            if not hazardous and track.destination != car.destination:
                continue
        elif track.purpose not in {TrackPurpose.GENERAL, TrackPurpose.TRANSFER}:
            continue
        if not kind_allowed(track, car):
            kind_blocked = True
            continue
        routing.append(track)
    return routing, kind_blocked


def car_block_reason(car: FreightCar, cars: dict[str, FreightCar], tracks: Iterable[StandingTrack]) -> str:
    """Explain why ``car`` cannot be spotted on any track.

    Structural incompatibilities (maintenance, restriction, transfer duty) are
    reported before full-capacity reasons so dispatch sees yard-level limits
    first; hazard-rated shortages use dedicated codes.
    """

    track_list = list(tracks)
    routing, kind_blocked = _routing_tracks(car, track_list)
    if not routing:
        if car.is_hazardous():
            return REASON_HAZARD_NOT_RATED
        has_destination = any(
            item.purpose == TrackPurpose.DESTINATION and item.destination == car.destination
            for item in track_list
        )
        if kind_blocked and has_destination:
            return REASON_KIND_NOT_ALLOWED
        if not has_destination:
            return REASON_DESTINATION_MISMATCH
        return REASON_KIND_NOT_ALLOWED if kind_blocked else REASON_NO_CANDIDATE

    structural = [reason for reason in (_structural_reason(item) for item in routing) if reason is not None]
    usable = [item for item in routing if _structural_reason(item) is None]
    if not usable:
        # Every routing track is structurally blocked; yard-level limits win.
        # Shift arrangements (restriction, transfer duty) are reported ahead of
        # a maintenance track that may simply be background infrastructure.
        for code in (REASON_TRACK_RESTRICTED, REASON_TRANSFER_PURPOSE, REASON_TRACK_MAINTENANCE):
            if code in structural:
                return REASON_HAZARD_UNAVAILABLE if car.is_hazardous() else code
    if any(_has_headroom(item, car, cars) for item in usable):
        return REASON_NO_CANDIDATE
    # Usable routing tracks are all full. If a track was also removed from this
    # shift by a restriction/transfer arrangement, that arrangement is the
    # actionable cause; otherwise report the capacity shortfall directly.
    if car.is_hazardous():
        return REASON_HAZARD_CAPACITY
    for code in (REASON_TRACK_RESTRICTED, REASON_TRANSFER_PURPOSE):
        if code in structural:
            return code
    # All usable tracks are full. CAR_CAPACITY wins when any track rejects on
    # count; LENGTH_CAPACITY only when every track has count headroom but not
    # enough length headroom for this car.
    capacity = {_capacity_block(item, car, cars) for item in usable}
    if REASON_CAR_CAPACITY in capacity:
        return REASON_CAR_CAPACITY
    if REASON_LENGTH_CAPACITY in capacity:
        return REASON_LENGTH_CAPACITY
    return REASON_NO_CANDIDATE


def remaining_capacity_score(track: StandingTrack, cars: dict[str, FreightCar]) -> int:
    count, length = stack_occupancy(track, cars)
    cars_left = track.capacity_cars - count
    length_left = track.capacity_length_m - length
    return cars_left * 1000 + length_left


__all__ = [
    "ALL_KINDS",
    "DEFAULT_BUFFER_CAPACITY",
    "DESTINATION_CODES",
    "HAZARD_CLASSES",
    "MAX_CAR_LENGTH_M",
    "MAX_PLANNED_CARS",
    "MAX_TRAIN_CONSIST",
    "MIN_CAR_LENGTH_M",
    "REASON_AFTER_HORIZON",
    "REASON_BEFORE_OPEN",
    "REASON_CAR_CAPACITY",
    "REASON_DESTINATION_MISMATCH",
    "REASON_HAZARD_CAPACITY",
    "REASON_HAZARD_NOT_RATED",
    "REASON_HAZARD_UNAVAILABLE",
    "REASON_KIND_NOT_ALLOWED",
    "REASON_LENGTH_CAPACITY",
    "REASON_MESSAGES",
    "REASON_NO_CANDIDATE",
    "REASON_TRACK_MAINTENANCE",
    "REASON_TRACK_RESTRICTED",
    "REASON_TRANSFER_PURPOSE",
    "TRANSFER_BAY_CODE",
    "candidate_tracks_for",
    "car_block_reason",
    "destination_allowed",
    "destination_known",
    "hazard_allowed",
    "hazard_known",
    "is_car_code",
    "is_entity_code",
    "kind_allowed",
    "kind_known",
    "normalize_destination",
    "ranked_candidate_tracks",
    "remaining_capacity_score",
    "stack_occupancy",
    "track_receives_car",
]
