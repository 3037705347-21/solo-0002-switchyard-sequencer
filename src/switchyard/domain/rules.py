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
    "TRANSFER_BAY_CODE",
    "candidate_tracks_for",
    "destination_allowed",
    "destination_known",
    "hazard_allowed",
    "hazard_known",
    "is_car_code",
    "is_entity_code",
    "kind_allowed",
    "kind_known",
    "normalize_destination",
    "remaining_capacity_score",
    "stack_occupancy",
    "track_receives_car",
]
