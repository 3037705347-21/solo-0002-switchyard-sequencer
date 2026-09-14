"""Classification allocation onto standing tracks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .car import FreightCar
from .enums import CarState, IntakeState, TrackPurpose
from .errors import ConflictError, ResourceBusyError
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


def _raise_if_blocked_by_plan(
    car_code: str,
    ranked: list[StandingTrack],
    pinned_tracks: Mapping[str, list[str]],
) -> None:
    blocked = [track for track in ranked if track.code in pinned_tracks]
    if not blocked:
        return
    track_codes = [track.code for track in blocked]
    run_codes = sorted({run_code for track in blocked for run_code in pinned_tracks[track.code]})
    raise ResourceBusyError(
        f"car {car_code} can only stand on {', '.join(track_codes)}, which "
        f"{'is' if len(track_codes) == 1 else 'are'} pinned by active pull "
        f"run(s) {', '.join(run_codes)}; finish the pull run before spotting "
        "more cars there",
        car_code=car_code,
        track_codes=track_codes,
        run_codes=run_codes,
    )


def classify_intake(
    train: IntakeTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    pinned_tracks: Mapping[str, list[str]] | None = None,
) -> list[SpotRecord]:
    if train.is_terminal():
        raise ConflictError(f"intake {train.code} is already terminal")
    pinned = dict(pinned_tracks or {})
    spots: list[SpotRecord] = []
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
        available = [track for track in ranked if track.code not in pinned]
        if not available:
            _raise_if_blocked_by_plan(code, ranked, pinned)
            unplaced.append(code)
            continue
        target = available[0]
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
