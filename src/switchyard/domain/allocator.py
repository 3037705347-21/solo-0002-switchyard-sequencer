"""Classification allocation onto standing tracks."""

from __future__ import annotations

from dataclasses import dataclass, field

from .car import FreightCar
from .enums import CarState, IntakeState, TrackPurpose
from .errors import ConflictError
from .intake import IntakeTrain
from .rules import candidate_tracks_for, remaining_capacity_score
from .track import StandingTrack


@dataclass(slots=True)
class SpotRecord:
    car_code: str
    track_code: str
    index: int
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "car_code": self.car_code,
            "track_code": self.track_code,
            "index": self.index,
            "reason": self.reason,
        }


def _ranked_candidates(car: FreightCar, cars: dict[str, FreightCar], tracks: dict[str, StandingTrack]) -> list[StandingTrack]:
    candidates = candidate_tracks_for(car, cars, tracks.values())
    destination = [item for item in candidates if item.purpose == TrackPurpose.DESTINATION]
    general = [item for item in candidates if item.purpose == TrackPurpose.GENERAL]
    destination.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    general.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    return destination + general


def classify_intake(
    train: IntakeTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
) -> list[SpotRecord]:
    if train.is_terminal():
        raise ConflictError(f"intake {train.code} is already terminal")
    spots: list[SpotRecord] = []
    unplaced: list[str] = []
    for code in train.consist:
        car = cars.get(code)
        if car is None:
            unplaced.append(code)
            continue
        if car.state != CarState.RECEIVED:
            # Already placed by an earlier classification pass of this train.
            continue
        ranked = _ranked_candidates(car, cars, tracks)
        target = ranked[0] if ranked else None
        if target is None:
            unplaced.append(code)
            continue
        target.stack.append(car.code)
        car.state = CarState.STANDING
        car.location = target.code
        spots.append(SpotRecord(code, target.code, len(target.stack) - 1))
    train.unplaced = unplaced
    if unplaced:
        train.state = IntakeState.PARTIAL
    else:
        train.state = IntakeState.CLASSIFIED
    return spots


def rollback_classification(train: IntakeTrain, cars: dict[str, FreightCar], tracks: dict[str, StandingTrack]) -> None:
    for code in train.consist:
        car = cars.get(code)
        if car is None:
            continue
        if car.location in tracks:
            track = tracks[car.location]
            if code in track.stack:
                track.stack.remove(code)
        if car.state in {CarState.STANDING}:
            car.state = CarState.RECEIVED
            car.location = "INTAKE"
    train.state = IntakeState.OPEN
    train.unplaced = list(train.consist)


__all__ = ["SpotRecord", "classify_intake", "rollback_classification"]
