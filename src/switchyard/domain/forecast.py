"""Read-only arrival capacity forecasting.

The forecast answers, before an intake manifest is submitted, whether the next
inbound trains can be classified inside the current shift. It never mutates the
persisted workspace: classification is replayed on a deep copy, combining the
cars already physically on tracks ("current" occupancy) with cars that only
exist as plans ("planned" occupancy, split into already-received but not yet
classified intakes and prospective trains carried by the request).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .car import CarInput, FreightCar
from .enums import CarKind, CarState, IntakeState, TrackPurpose, TrackState
from .rules import (
    REASON_AFTER_HORIZON,
    REASON_BEFORE_OPEN,
    REASON_MESSAGES,
    car_block_reason,
    ranked_candidate_tracks,
    stack_occupancy,
)
from .track import StandingTrack

# Source tags used to separate occupancy kinds in the report.
SOURCE_PERSISTED = "PLANNED_PERSISTED"
SOURCE_FORECAST = "PLANNED_FORECAST"
SOURCE_CURRENT = "CURRENT"


@dataclass(slots=True)
class ProspectiveTrain:
    """An inbound train that only exists in a forecast request."""

    code: str
    route: str
    arrival_at: str
    cars: list[CarInput] = field(default_factory=list)

    @property
    def consist(self) -> list[str]:
        return [item.code for item in self.cars]


@dataclass(slots=True)
class PlannedSpot:
    car_code: str
    track_code: str
    source: str
    train_code: str


@dataclass(slots=True)
class BlockRecord:
    car_code: str
    reason_code: str
    reason: str
    source: str
    train_code: str
    track_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "car_code": self.car_code,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "source": self.source,
            "train_code": self.train_code,
            "track_code": self.track_code,
        }


@dataclass(slots=True)
class TrainForecast:
    code: str
    route: str
    arrival_at: str
    source: str
    within_shift: bool
    window_reason_code: str | None
    total_cars: int
    placeable_cars: int
    blocked_cars: int
    already_standing: int
    spots: list[PlannedSpot] = field(default_factory=list)
    blocks: list[BlockRecord] = field(default_factory=list)

    @property
    def fully_placeable(self) -> bool:
        return self.within_shift and self.blocked_cars == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "route": self.route,
            "arrival_at": self.arrival_at,
            "source": self.source,
            "within_shift": self.within_shift,
            "window_reason_code": self.window_reason_code,
            "total_cars": self.total_cars,
            "placeable_cars": self.placeable_cars,
            "blocked_cars": self.blocked_cars,
            "already_standing": self.already_standing,
            "fully_placeable": self.fully_placeable,
            "spots": [
                {
                    "car_code": item.car_code,
                    "track_code": item.track_code,
                    "source": item.source,
                    "train_code": item.train_code,
                }
                for item in self.spots
            ],
            "blocked": [item.to_dict() for item in self.blocks],
        }


def track_type_label(track: StandingTrack) -> str:
    if track.purpose == TrackPurpose.DESTINATION:
        return f"DEST:{track.destination or 'UNKNOWN'}"
    if track.purpose == TrackPurpose.GENERAL:
        return "HAZ:GENERAL" if track.hazard_rated else "GENERAL"
    return "TRANSFER"


def _car_from_input(item: CarInput) -> FreightCar:
    return FreightCar(
        code=item.code,
        kind=CarKind.parse(item.kind),
        destination=item.destination,
        loaded=item.loaded,
        length_m=item.length_m,
        danger_class=item.danger_class,
        state=CarState.RECEIVED,
        location="INTAKE",
        note=item.note,
    )


@dataclass(slots=True)
class _TimelineEntry:
    train_code: str
    route: str
    arrival_at: str
    source: str
    consist: list[str]


def _persisted_open_intakes(workspace: object) -> list[_TimelineEntry]:
    entries: list[_TimelineEntry] = []
    for code, intake in workspace.intakes.items():
        if intake.state in {IntakeState.OPEN, IntakeState.PARTIAL}:
            entries.append(
                _TimelineEntry(
                    train_code=code,
                    route=intake.route,
                    arrival_at=intake.arrival_at,
                    source=SOURCE_PERSISTED,
                    consist=list(intake.consist),
                )
            )
    return entries


def _track_dict(
    track: StandingTrack,
    cars: dict[str, FreightCar],
    planned: list[PlannedSpot],
    baseline: dict[str, tuple[int, int]],
) -> dict[str, object]:
    # "current" is the physically occupied baseline captured before any plan is
    # replayed; the shadow stack must only contribute to "planned"/"projected".
    current_count, current_length = baseline.get(track.code, (0, 0))
    planned_codes = [item.car_code for item in planned if item.track_code == track.code]
    planned_length = sum(cars[code].length_m for code in planned_codes if code in cars)
    sources: dict[str, int] = {}
    for item in planned:
        if item.track_code == track.code:
            sources[item.source] = sources.get(item.source, 0) + 1
    return {
        "code": track.code,
        "track_type": track_type_label(track),
        "purpose": str(track.purpose),
        "state": str(track.state),
        "hazard_rated": track.hazard_rated,
        "destination": track.destination,
        "capacity_cars": track.capacity_cars,
        "capacity_length_m": track.capacity_length_m,
        "current_cars": current_count,
        "current_length_m": current_length,
        "planned_cars": len(planned_codes),
        "planned_length_m": planned_length,
        "planned_by_source": [
            {"source": source, "cars": count} for source, count in sorted(sources.items())
        ],
        "planned_car_codes": list(planned_codes),
        "projected_cars": current_count + len(planned_codes),
        "projected_length_m": current_length + planned_length,
        "remaining_cars": max(0, track.capacity_cars - current_count - len(planned_codes)),
        "remaining_length_m": max(
            0, track.capacity_length_m - current_length - planned_length
        ),
        "receives_cars": track.state == TrackState.OPERATIONAL
        and track.purpose != TrackPurpose.TRANSFER,
    }


def _remaining_track_types(track_rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    grouped: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for row in track_rows:
        if not row["receives_cars"]:
            continue
        label = str(row["track_type"])
        if label not in grouped:
            grouped[label] = {
                "track_type": label,
                "remaining_cars": 0,
                "remaining_length_m": 0,
                "tracks_with_room": [],
                "track_codes": [],
            }
            order.append(label)
        bucket = grouped[label]
        bucket["track_codes"].append(row["code"])
        if int(row["remaining_cars"]) > 0:
            bucket["remaining_cars"] = int(bucket["remaining_cars"]) + int(row["remaining_cars"])
            bucket["remaining_length_m"] = int(bucket["remaining_length_m"]) + int(
                row["remaining_length_m"]
            )
            bucket["tracks_with_room"].append(row["code"])
    available = [grouped[label] for label in order if int(grouped[label]["remaining_cars"]) > 0]
    exhausted = [
        {
            "track_type": label,
            "track_codes": grouped[label]["track_codes"],
            "reason_code": "TRACK_TYPE_FULL",
            "reason": "no track of this type has remaining car capacity",
        }
        for label in order
        if int(grouped[label]["remaining_cars"]) == 0
    ]
    available.sort(key=lambda item: str(item["track_type"]))
    exhausted.sort(key=lambda item: str(item["track_type"]))
    return available, exhausted


def build_arrival_forecast(
    workspace: object,
    prospective: list[ProspectiveTrain],
    shift_opened_at: str,
    horizon_at: str | None,
) -> dict[str, object]:
    """Replay classification for every upcoming train without persisting it.

    Persisted open/partial intakes and prospective trains share one timeline
    ordered by arrival time (code breaks ties); cars already standing from a
    partial intake count as current occupancy and are never re-spotted.
    """

    shadow = copy.deepcopy(workspace)
    shadow_cars: dict[str, FreightCar] = shadow.cars
    shadow_tracks: dict[str, StandingTrack] = shadow.tracks

    # Snapshot physical occupancy from the real workspace before replaying any
    # plan; this is what keeps "current" and "planned" apart in the report.
    baseline_occupancy = {
        code: stack_occupancy(track, workspace.cars)
        for code, track in workspace.tracks.items()
    }

    timeline = _persisted_open_intakes(shadow)
    for train in prospective:
        for item in train.cars:
            shadow_cars[item.code] = _car_from_input(item)
        timeline.append(
            _TimelineEntry(
                train_code=train.code,
                route=train.route,
                arrival_at=train.arrival_at,
                source=SOURCE_FORECAST,
                consist=train.consist,
            )
        )
    timeline.sort(key=lambda entry: (entry.arrival_at, entry.train_code))

    planned_spots: list[PlannedSpot] = []
    train_reports: list[TrainForecast] = []
    for entry in timeline:
        within_shift = entry.arrival_at >= shift_opened_at and (
            horizon_at is None or entry.arrival_at <= horizon_at
        )
        window_reason: str | None = None
        if entry.arrival_at < shift_opened_at:
            window_reason = REASON_BEFORE_OPEN
        elif horizon_at is not None and entry.arrival_at > horizon_at:
            window_reason = REASON_AFTER_HORIZON

        spots: list[PlannedSpot] = []
        blocks: list[BlockRecord] = []
        already_standing = 0
        if within_shift:
            for car_code in entry.consist:
                car = shadow_cars.get(car_code)
                if car is None:
                    blocks.append(
                        BlockRecord(
                            car_code,
                            "CAR_MISSING",
                            "consist references an unknown car",
                            entry.source,
                            entry.train_code,
                        )
                    )
                    continue
                if car.state != CarState.RECEIVED:
                    already_standing += 1
                    continue
                ranked = ranked_candidate_tracks(car, shadow_cars, shadow_tracks.values())
                if not ranked:
                    reason_code = car_block_reason(car, shadow_cars, shadow_tracks.values())
                    blocks.append(
                        BlockRecord(
                            car_code,
                            reason_code,
                            REASON_MESSAGES.get(reason_code, reason_code),
                            entry.source,
                            entry.train_code,
                        )
                    )
                    continue
                target = ranked[0]
                target.stack.append(car.code)
                car.state = CarState.STANDING
                car.location = target.code
                spot = PlannedSpot(car_code, target.code, entry.source, entry.train_code)
                spots.append(spot)
                planned_spots.append(spot)
        else:
            assert window_reason is not None
            for car_code in entry.consist:
                blocks.append(
                    BlockRecord(
                        car_code,
                        window_reason,
                        REASON_MESSAGES[window_reason],
                        entry.source,
                        entry.train_code,
                    )
                )

        train_reports.append(
            TrainForecast(
                code=entry.train_code,
                route=entry.route,
                arrival_at=entry.arrival_at,
                source=entry.source,
                within_shift=within_shift,
                window_reason_code=window_reason,
                total_cars=len(entry.consist),
                placeable_cars=len(spots),
                blocked_cars=len(blocks),
                already_standing=already_standing,
                spots=spots,
                blocks=blocks,
            )
        )

    track_rows = [
        _track_dict(track, shadow_cars, planned_spots, baseline_occupancy)
        for _code, track in sorted(shadow_tracks.items())
    ]
    available, exhausted = _remaining_track_types(track_rows)
    unavailable = [
        {
            "code": row["code"],
            "track_type": row["track_type"],
            "reason_code": "TRACK_IN_MAINTENANCE"
            if row["state"] == str(TrackState.MAINTENANCE)
            else "TRACK_RESTRICTED"
            if row["state"] == str(TrackState.RESTRICTED)
            else "TRACK_TRANSFER_DUTY",
            "reason": "track is in maintenance"
            if row["state"] == str(TrackState.MAINTENANCE)
            else "track is restricted"
            if row["state"] == str(TrackState.RESTRICTED)
            else "track is assigned to transfer duty",
        }
        for row in track_rows
        if not row["receives_cars"]
    ]

    totals = {
        "trains_forecast": len(train_reports),
        "trains_fully_placeable": sum(1 for report in train_reports if report.fully_placeable),
        "trains_blocked": sum(1 for report in train_reports if report.blocked_cars > 0),
        "cars_requested": sum(report.total_cars for report in train_reports),
        "cars_placeable": sum(report.placeable_cars for report in train_reports),
        "cars_blocked": sum(report.blocked_cars for report in train_reports),
        "cars_already_standing": sum(report.already_standing for report in train_reports),
    }
    return {
        "shift_opened_at": shift_opened_at,
        "shift_horizon_at": horizon_at,
        "trains": [report.to_dict() for report in train_reports],
        "tracks": track_rows,
        "remaining_track_types": available,
        "exhausted_track_types": exhausted,
        "unavailable_tracks": unavailable,
        "totals": totals,
    }


__all__ = [
    "PlannedSpot",
    "ProspectiveTrain",
    "SOURCE_CURRENT",
    "SOURCE_FORECAST",
    "SOURCE_PERSISTED",
    "TrainForecast",
    "build_arrival_forecast",
    "track_type_label",
]
