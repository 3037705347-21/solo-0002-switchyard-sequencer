"""Classification allocation onto standing tracks."""

from __future__ import annotations

from dataclasses import dataclass, field

from .car import FreightCar
from .enums import CarState, IntakeState, TrackPurpose
from .errors import ConflictError
from .intake import IntakeTrain
from .rules import remaining_capacity_score, stack_occupancy, track_receives_car
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
class CandidateOption:
    """An acceptable track with its remaining capacity at decision time."""

    track_code: str
    rank: int
    remaining_cars: int
    remaining_length_m: int

    def to_dict(self) -> dict[str, object]:
        return {
            "track_code": self.track_code,
            "rank": self.rank,
            "remaining_cars": self.remaining_cars,
            "remaining_length_m": self.remaining_length_m,
        }


@dataclass(slots=True)
class TrackRejection:
    """A track that was evaluated and refused, in evaluation order."""

    track_code: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"track_code": self.track_code, "reason": self.reason}


@dataclass(slots=True)
class CarDecision:
    """Per-car trace of one classification round."""

    car_code: str
    outcome: str
    track_code: str | None = None
    index: int | None = None
    candidates: list[CandidateOption] = field(default_factory=list)
    remaining_cars_after: int | None = None
    remaining_length_m_after: int | None = None
    rejections: list[TrackRejection] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "car_code": self.car_code,
            "outcome": self.outcome,
            "track_code": self.track_code,
            "index": self.index,
            "candidates": [item.to_dict() for item in self.candidates],
            "remaining_cars_after": self.remaining_cars_after,
            "remaining_length_m_after": self.remaining_length_m_after,
            "rejections": [item.to_dict() for item in self.rejections],
            "detail": self.detail,
        }


@dataclass(slots=True)
class ClassificationResult:
    spots: list[SpotRecord]
    decisions: list[CarDecision]


def _remaining_capacity(track: StandingTrack, cars: dict[str, FreightCar]) -> tuple[int, int]:
    count, length = stack_occupancy(track, cars)
    return track.capacity_cars - count, track.capacity_length_m - length


def _evaluate_tracks(
    car: FreightCar,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
) -> tuple[list[StandingTrack], list[TrackRejection]]:
    accepted: list[StandingTrack] = []
    rejections: list[TrackRejection] = []
    for track in tracks.values():
        reason = track_receives_car(track, car, cars)
        if reason is None:
            accepted.append(track)
        else:
            rejections.append(TrackRejection(track.code, reason))
    return accepted, rejections


def _rank_accepted(accepted: list[StandingTrack], cars: dict[str, FreightCar]) -> list[StandingTrack]:
    destination = [item for item in accepted if item.purpose == TrackPurpose.DESTINATION]
    general = [item for item in accepted if item.purpose == TrackPurpose.GENERAL]
    destination.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    general.sort(key=lambda item: remaining_capacity_score(item, cars), reverse=True)
    return destination + general


def classify_intake(
    train: IntakeTrain,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
) -> ClassificationResult:
    if train.is_terminal():
        raise ConflictError(f"intake {train.code} is already terminal")
    spots: list[SpotRecord] = []
    decisions: list[CarDecision] = []
    unplaced: list[str] = []
    for code in train.consist:
        car = cars.get(code)
        if car is None:
            unplaced.append(code)
            decisions.append(
                CarDecision(car_code=code, outcome="unplaced", detail="car is not recorded in the yard")
            )
            continue
        if car.state != CarState.RECEIVED:
            unplaced.append(code)
            decisions.append(
                CarDecision(
                    car_code=code,
                    outcome="unplaced",
                    detail=f"car state is {car.state}, expected RECEIVED",
                )
            )
            continue
        accepted, rejections = _evaluate_tracks(car, cars, tracks)
        ranked = _rank_accepted(accepted, cars)
        candidates: list[CandidateOption] = []
        for rank, track in enumerate(ranked, start=1):
            remaining_cars, remaining_length = _remaining_capacity(track, cars)
            candidates.append(CandidateOption(track.code, rank, remaining_cars, remaining_length))
        if not ranked:
            unplaced.append(code)
            decisions.append(
                CarDecision(
                    car_code=code,
                    outcome="unplaced",
                    candidates=candidates,
                    rejections=rejections,
                    detail="no candidate track available",
                )
            )
            continue
        target = ranked[0]
        target.stack.append(car.code)
        car.state = CarState.STANDING
        car.location = target.code
        index = len(target.stack) - 1
        spots.append(SpotRecord(code, target.code, index))
        remaining_cars, remaining_length = _remaining_capacity(target, cars)
        decisions.append(
            CarDecision(
                car_code=code,
                outcome="placed",
                track_code=target.code,
                index=index,
                candidates=candidates,
                remaining_cars_after=remaining_cars,
                remaining_length_m_after=remaining_length,
                rejections=rejections,
            )
        )
    train.unplaced = unplaced
    if unplaced:
        train.state = IntakeState.PARTIAL
    else:
        train.state = IntakeState.CLASSIFIED
    return ClassificationResult(spots=spots, decisions=decisions)


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
    "CandidateOption",
    "CarDecision",
    "ClassificationResult",
    "SpotRecord",
    "TrackRejection",
    "classify_intake",
    "rollback_classification",
]
