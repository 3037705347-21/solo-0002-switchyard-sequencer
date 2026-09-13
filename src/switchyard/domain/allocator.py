"""Classification allocation onto standing tracks."""

from __future__ import annotations

from dataclasses import dataclass, field

from .car import FreightCar
from .enums import CarState, IntakeState, TrackPurpose, TrackState
from .errors import ConflictError
from .intake import IntakeTrain
from .rules import candidate_tracks_for, remaining_capacity_score, stack_occupancy
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
class ClassificationResult:
    """Outcome of one classification pass over an intake consist."""

    spots: list[SpotRecord] = field(default_factory=list)
    tradeoffs: list[str] = field(default_factory=list)


def _ranked_candidates(car: FreightCar, cars: dict[str, FreightCar], tracks: dict[str, StandingTrack]) -> list[StandingTrack]:
    candidates = candidate_tracks_for(car, cars, tracks.values())
    destination = [item for item in candidates if item.purpose == TrackPurpose.DESTINATION]
    general = [item for item in candidates if item.purpose == TrackPurpose.GENERAL]
    destination.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    general.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    return destination + general


def _hazard_pool(tracks: dict[str, StandingTrack]) -> list[StandingTrack]:
    return [
        track
        for track in tracks.values()
        if track.hazard_rated and track.state == TrackState.OPERATIONAL
    ]


def _pool_free(pool: list[StandingTrack], cars: dict[str, FreightCar]) -> tuple[int, int]:
    free_cars = 0
    free_length = 0
    for track in pool:
        used_cars, used_length = stack_occupancy(track, cars)
        free_cars += track.capacity_cars - used_cars
        free_length += track.capacity_length_m - used_length
    return free_cars, free_length


def _anticipated_hazard_demand(train: IntakeTrain, cars: dict[str, FreightCar]) -> tuple[int, int]:
    """Count and length of hazardous cars in this batch that still need a spot."""
    count = 0
    length = 0
    for code in train.consist:
        car = cars.get(code)
        if car is None or car.state != CarState.RECEIVED:
            continue
        if car.is_hazardous():
            count += 1
            length += car.length_m
    return count, length


def _spot_car(car: FreightCar, target: StandingTrack, spots: list[SpotRecord]) -> None:
    target.stack.append(car.code)
    car.state = CarState.STANDING
    car.location = target.code
    spots.append(SpotRecord(car.code, target.code, len(target.stack) - 1))


def _protected_target(
    car: FreightCar,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    pool: list[StandingTrack],
    reserve_cars: int,
    reserve_length: int,
) -> tuple[StandingTrack | None, list[StandingTrack]]:
    """Pick a track for a non-hazardous car without eating the hazard reserve.

    Hazard-rated candidates are vetoed while placing the car there would leave
    the pool with less free room than the remaining anticipated hazard demand.
    Returns the chosen track plus the candidates vetoed by the reserve.
    """
    vetoed: list[StandingTrack] = []
    for candidate in _ranked_candidates(car, cars, tracks):
        if candidate.hazard_rated and reserve_cars > 0:
            free_cars, free_length = _pool_free(pool, cars)
            if free_cars - 1 < reserve_cars or free_length - car.length_m < reserve_length:
                vetoed.append(candidate)
                continue
        return candidate, vetoed
    return None, vetoed


def classify_intake(
    train: IntakeTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
) -> ClassificationResult:
    """Place every receivable car of the consist onto a standing track.

    The pass is append-only: already completed stacks are never rewritten and
    each car is placed at most once. Hazardous cars must reach a hazard-rated
    track, so the batch's anticipated hazard demand is reserved up front and
    non-hazardous cars may only consume hazard-rated capacity that exceeds
    that reserve. When the reserve forces a choice, the car left behind is
    reported in ``tradeoffs`` instead of being silently sacrificed.
    """
    if train.is_terminal():
        raise ConflictError(f"intake {train.code} is already terminal")
    result = ClassificationResult()
    unplaced: list[str] = []
    reserve_cars, reserve_length = _anticipated_hazard_demand(train, cars)
    pool = _hazard_pool(tracks)
    for code in train.consist:
        car = cars.get(code)
        if car is None:
            unplaced.append(code)
            continue
        if car.state != CarState.RECEIVED:
            unplaced.append(code)
            continue
        if car.is_hazardous():
            reserve_cars -= 1
            reserve_length -= car.length_m
            ranked = _ranked_candidates(car, cars, tracks)
            if not ranked:
                unplaced.append(code)
                result.tradeoffs.append(
                    f"hazardous car {code} left unplaced: no hazard-rated track can receive it; "
                    "its reserved capacity is released for the rest of the batch"
                )
                continue
            _spot_car(car, ranked[0], result.spots)
            continue
        target, vetoed = _protected_target(car, cars, tracks, pool, reserve_cars, reserve_length)
        if target is None:
            unplaced.append(code)
            if vetoed:
                codes_text = ", ".join(item.code for item in vetoed)
                result.tradeoffs.append(
                    f"car {code} held back: only hazard-rated capacity remained ({codes_text}) and "
                    f"{reserve_cars} car slot(s) / {reserve_length}m are reserved for "
                    "anticipated hazardous cars"
                )
            continue
        _spot_car(car, target, result.spots)
    train.unplaced = unplaced
    if unplaced:
        train.state = IntakeState.PARTIAL
    else:
        train.state = IntakeState.CLASSIFIED
    return result


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


__all__ = ["ClassificationResult", "SpotRecord", "classify_intake", "rollback_classification"]
