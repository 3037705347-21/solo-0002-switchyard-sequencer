"""Classification allocation onto standing tracks."""

from __future__ import annotations

from dataclasses import dataclass, field

from .car import FreightCar
from .enums import CarState, IntakeState, TrackPurpose
from .errors import ConflictError
from .intake import IntakeTrain
from .rules import candidate_tracks_for, remaining_capacity_score, track_receives_car
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


@dataclass(slots=True)
class ManualSpotOutcome:
    """Result of a dispatcher request to force a car onto a specific track."""

    car_code: str
    requested_track: str
    applied: bool
    reason: str
    automatic_track: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "car_code": self.car_code,
            "requested_track": self.requested_track,
            "applied": self.applied,
            "reason": self.reason,
            "automatic_track": self.automatic_track,
        }


@dataclass(slots=True)
class ClassificationResult:
    spots: list[SpotRecord] = field(default_factory=list)
    manual_outcomes: list[ManualSpotOutcome] = field(default_factory=list)


def _ranked_candidates(car: FreightCar, cars: dict[str, FreightCar], tracks: dict[str, StandingTrack]) -> list[StandingTrack]:
    candidates = candidate_tracks_for(car, cars, tracks.values())
    destination = [item for item in candidates if item.purpose == TrackPurpose.DESTINATION]
    general = [item for item in candidates if item.purpose == TrackPurpose.GENERAL]
    destination.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    general.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    return destination + general


def _place(car: FreightCar, track: StandingTrack, spots: list[SpotRecord]) -> None:
    track.stack.append(car.code)
    car.state = CarState.STANDING
    car.location = track.code
    spots.append(SpotRecord(car.code, track.code, len(track.stack) - 1))


def classify_intake(
    train: IntakeTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    manual_spots: dict[str, str] | None = None,
) -> ClassificationResult:
    if train.is_terminal():
        raise ConflictError(f"intake {train.code} is already terminal")
    requested = dict(manual_spots or {})
    spots: list[SpotRecord] = []
    outcomes: list[ManualSpotOutcome] = []
    unplaced: list[str] = []
    for code in train.consist:
        car = cars.get(code)
        if car is None:
            unplaced.append(code)
            continue
        if car.state != CarState.RECEIVED:
            unplaced.append(code)
            continue
        ranked = _ranked_candidates(car, cars, tracks)
        automatic = ranked[0] if ranked else None
        automatic_code = automatic.code if automatic is not None else None
        wanted_code = requested.get(code)
        target = automatic
        if wanted_code is not None:
            wanted = tracks.get(wanted_code)
            rejection = None
            if wanted is None:
                rejection = f"track {wanted_code} is not known"
            else:
                rejection = track_receives_car(wanted, car, cars)
            if rejection is None:
                target = wanted
                outcomes.append(
                    ManualSpotOutcome(code, wanted_code, True, "manual placement accepted", automatic_code)
                )
            else:
                outcomes.append(ManualSpotOutcome(code, wanted_code, False, rejection, automatic_code))
        if target is None:
            unplaced.append(code)
            continue
        _place(car, target, spots)
    train.unplaced = unplaced
    if unplaced:
        train.state = IntakeState.PARTIAL
    else:
        train.state = IntakeState.CLASSIFIED
    return ClassificationResult(spots=spots, manual_outcomes=outcomes)


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


__all__ = [
    "ClassificationResult",
    "ManualSpotOutcome",
    "SpotRecord",
    "classify_intake",
    "rollback_classification",
]
