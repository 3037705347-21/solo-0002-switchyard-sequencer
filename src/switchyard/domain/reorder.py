"""Track reorganization work orders and LIFO-aware tidy planning.

A reorder work order relocates standing cars between tracks (or restacks one
track into a requested order) without creating an outbound train and without
reserving any car. Every move physically passes through the transfer bay:
blockers are buffered, the target car is extracted, the target is returned to
its destination track, and the blockers are returned to their source track.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .car import FreightCar
from .enums import CarState, MoveVerb, ReorderMode, RunState, TrackState
from .errors import ValidationError
from .pull import MoveStep
from .rules import destination_allowed, hazard_allowed, kind_allowed
from .timeutil import now_iso
from .track import BufferBay, StandingTrack


@dataclass(slots=True)
class ReorderMove:
    """One requested relocation of a car to the top of a destination track."""

    car_code: str
    source_code: str
    target_code: str

    def to_dict(self) -> dict[str, str]:
        return {
            "car_code": self.car_code,
            "source_code": self.source_code,
            "target_code": self.target_code,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> "ReorderMove":
        return cls(
            car_code=str(raw["car_code"]),
            source_code=str(raw["source_code"]),
            target_code=str(raw["target_code"]),
        )


@dataclass(slots=True)
class StepRecord:
    """Persisted position of a car right after one executed step."""

    step_index: int
    verb: MoveVerb
    car_code: str
    source_code: str
    target_code: str
    location_after: str | None
    at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "step_index": self.step_index,
            "verb": str(self.verb),
            "car_code": self.car_code,
            "source_code": self.source_code,
            "target_code": self.target_code,
            "location_after": self.location_after,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "StepRecord":
        location_after = raw.get("location_after")
        return cls(
            step_index=int(raw["step_index"]),
            verb=MoveVerb.parse(str(raw["verb"])),
            car_code=str(raw["car_code"]),
            source_code=str(raw["source_code"]),
            target_code=str(raw["target_code"]),
            location_after=None if location_after is None else str(location_after),
            at=str(raw.get("at", "")),
        )


@dataclass(slots=True)
class ReorderRequest:
    """Validated operator input before a reorder order is planned."""

    code: str
    mode: ReorderMode
    transfer_code: str
    moves: list[tuple[str, str]] = field(default_factory=list)
    track_code: str | None = None
    target_order: list[str] = field(default_factory=list)
    staging_track_code: str | None = None


@dataclass(slots=True)
class ReorderOrder:
    code: str
    mode: ReorderMode
    transfer_code: str
    track_code: str | None = None
    moves: list[ReorderMove] = field(default_factory=list)
    steps: list[MoveStep] = field(default_factory=list)
    state: RunState = RunState.QUEUED
    current_step: int = 0
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    step_records: list[StepRecord] = field(default_factory=list)
    expected_locations: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "mode": str(self.mode),
            "transfer_code": self.transfer_code,
            "track_code": self.track_code,
            "moves": [move.to_dict() for move in self.moves],
            "steps": [step.to_dict() for step in self.steps],
            "state": str(self.state),
            "current_step": self.current_step,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "step_records": [record.to_dict() for record in self.step_records],
            "expected_locations": dict(self.expected_locations),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ReorderOrder":
        track_code = raw.get("track_code")
        return cls(
            code=str(raw["code"]),
            mode=ReorderMode.parse(str(raw.get("mode", ReorderMode.MOVES.value))),
            transfer_code=str(raw["transfer_code"]),
            track_code=None if track_code is None else str(track_code),
            moves=[ReorderMove.from_dict(dict(item)) for item in raw.get("moves", [])],
            steps=[MoveStep.from_dict(dict(item)) for item in raw.get("steps", [])],
            state=RunState.parse(str(raw.get("state", RunState.QUEUED.value))),
            current_step=int(raw.get("current_step", 0)),
            created_at=str(raw.get("created_at", "")),
            started_at=None if raw.get("started_at") is None else str(raw["started_at"]),
            completed_at=None if raw.get("completed_at") is None else str(raw["completed_at"]),
            error=None if raw.get("error") is None else str(raw["error"]),
            step_records=[StepRecord.from_dict(dict(item)) for item in raw.get("step_records", [])],
            expected_locations={str(key): str(value) for key, value in dict(raw.get("expected_locations", {})).items()},
        )

    def remaining(self) -> int:
        return max(0, len(self.steps) - self.current_step)

    def active_step(self) -> MoveStep | None:
        if self.state == RunState.COMPLETED or self.current_step >= len(self.steps):
            return None
        return self.steps[self.current_step]


def _fail(code: str, message: str) -> None:
    raise ValidationError(message, **{"reorder": [code]})


def _receive_reason(
    track: StandingTrack,
    car: FreightCar,
    stack: list[str],
    cars: dict[str, FreightCar],
) -> str | None:
    """Capacity and compatibility check against a simulated track stack."""
    if track.state != TrackState.OPERATIONAL:
        return f"track {track.code} is {track.state.value}"
    if not destination_allowed(track, car):
        return f"track {track.code} rejects destination {car.destination}"
    if not kind_allowed(track, car):
        return f"track {track.code} rejects kind {car.kind.value}"
    if not hazard_allowed(track, car):
        return f"track {track.code} is not hazard rated"
    if len(stack) + 1 > track.capacity_cars:
        return f"track {track.code} is at car capacity"
    length = sum(cars[item].length_m for item in stack if item in cars) + car.length_m
    if length > track.capacity_length_m:
        return f"track {track.code} is at length capacity"
    return None


def _staging_capacity_reason(
    track: StandingTrack,
    spill: list[str],
    cars: dict[str, FreightCar],
    working: dict[str, list[str]],
) -> str | None:
    stack = list(working[track.code])
    for car_code in spill:
        reason = _receive_reason(track, cars[car_code], stack, cars)
        if reason is not None:
            return f"staging track {track.code} cannot hold {car_code}: {reason}"
        stack.append(car_code)
    return None


def _select_staging_track(
    requested: str | None,
    source_code: str,
    spill: list[str],
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    working: dict[str, list[str]],
) -> str:
    if requested is not None:
        track = tracks.get(requested)
        if track is None:
            _fail("staging-missing", f"staging track {requested} does not exist")
        if requested == source_code:
            _fail("staging-is-source", "staging track must differ from the reordered track")
        reason = _staging_capacity_reason(track, spill, cars, working)
        if reason is not None:
            _fail("staging-rejects", reason)
        return requested
    for code in sorted(tracks):
        if code == source_code:
            continue
        if _staging_capacity_reason(tracks[code], spill, cars, working) is None:
            return code
    _fail("no-staging-track", "no standing track can stage the reordered cars")
    return ""


def _derive_target_order_moves(
    request: ReorderRequest,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    working: dict[str, list[str]],
) -> list[tuple[str, str]]:
    track_code = str(request.track_code)
    if track_code not in tracks:
        _fail("track-missing", f"standing track {track_code} does not exist")
    current = list(working[track_code])
    desired = list(request.target_order)
    if sorted(current) != sorted(desired):
        _fail(
            "order-mismatch",
            f"target order must list exactly the {len(current)} cars currently on {track_code}",
        )
    if current == desired:
        _fail("already-in-order", f"track {track_code} already matches the target order")
    prefix = 0
    while prefix < len(current) and current[prefix] == desired[prefix]:
        prefix += 1
    spill = current[prefix:]
    staging_code = _select_staging_track(request.staging_track_code, track_code, spill, cars, tracks, working)
    moves = [(car_code, staging_code) for car_code in reversed(spill)]
    moves.extend((car_code, track_code) for car_code in desired[prefix:])
    return moves


def plan_reorder_order(
    request: ReorderRequest,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    transfer_bays: dict[str, BufferBay],
) -> ReorderOrder:
    """Derive buffer, extract, and return steps without mutating yard state."""
    transfer = transfer_bays.get(request.transfer_code)
    if transfer is None:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})
    working = {code: list(track.stack) for code, track in tracks.items()}
    location_of = {code: car.location for code, car in cars.items()}
    raw_moves = list(request.moves)
    if request.mode == ReorderMode.TARGET_ORDER:
        raw_moves = _derive_target_order_moves(request, cars, tracks, working)
    if not raw_moves:
        _fail("empty-plan", "reorder request produced no moves")
    order = ReorderOrder(
        code=request.code,
        mode=request.mode,
        transfer_code=transfer.code,
        track_code=request.track_code,
    )
    for car_code, target_code in raw_moves:
        car = cars.get(car_code)
        if car is None:
            _fail("car-missing", f"car {car_code} does not exist")
        if car.state != CarState.STANDING:
            _fail("car-not-standing", f"car {car_code} is not standing")
        source_code = location_of.get(car_code)
        if source_code not in tracks:
            _fail("car-not-on-track", f"car {car_code} has no standing location")
        if target_code not in tracks:
            _fail("track-missing", f"standing track {target_code} does not exist")
        if target_code == source_code:
            _fail("same-track", f"car {car_code} already stands on {source_code}")
        stack = working[source_code]
        if car_code not in stack:
            _fail("car-not-in-stack", f"car {car_code} is not in track {source_code}")
        index = stack.index(car_code)
        above = stack[index + 1 :]
        for blocker in above:
            blocker_car = cars.get(blocker)
            if blocker_car is None or blocker_car.state != CarState.STANDING:
                _fail(
                    "blocker-reserved",
                    f"car {blocker} is reserved elsewhere and cannot be buffered",
                )
        needed = len(above) + 1
        if needed > transfer.capacity_cars:
            _fail(
                "buffer-overflow",
                f"transfer bay {transfer.code} needs {needed} slots but has {transfer.capacity_cars}",
            )
        reason = _receive_reason(tracks[target_code], car, working[target_code], cars)
        if reason is not None:
            _fail("track-rejects", f"car {car_code} cannot be placed: {reason}")
        for blocker in reversed(above):
            order.steps.append(MoveStep(MoveVerb.BUFFER, blocker, source_code, transfer.code))
        order.steps.append(MoveStep(MoveVerb.EXTRACT, car_code, source_code, transfer.code))
        order.steps.append(MoveStep(MoveVerb.RETURN, car_code, transfer.code, target_code))
        for blocker in above:
            order.steps.append(MoveStep(MoveVerb.RETURN, blocker, transfer.code, source_code))
        del stack[index:]
        stack.extend(above)
        working[target_code].append(car_code)
        location_of[car_code] = target_code
        order.moves.append(ReorderMove(car_code, source_code, target_code))
        order.expected_locations[car_code] = target_code
    return order


__all__ = [
    "ReorderMove",
    "ReorderOrder",
    "ReorderRequest",
    "StepRecord",
    "plan_reorder_order",
]
