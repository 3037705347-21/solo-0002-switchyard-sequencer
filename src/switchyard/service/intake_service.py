"""Inbound train intake and classification commands."""

from __future__ import annotations

from typing import Any

from ..domain.allocator import classify_intake
from ..domain.car import CarInput, FreightCar
from ..domain.enums import CarKind, CarState, EventKind, IntakeState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.rules import MAX_TRAIN_CONSIST
from ..domain.validators import build_intake_append_payload, build_intake_payload
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before yard work")


def create_intake(app: YardApplication, payload: Any) -> dict[str, Any]:
    train, car_inputs = build_intake_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    if train.code in workspace.intakes:
        raise ConflictError("intake train already exists", code=train.code)
    for item in car_inputs:
        if item.code in workspace.cars:
            raise ConflictError("car code already exists", code=item.code)
    cars: list[FreightCar] = []
    for item in car_inputs:
        car = _car_from_input(item)
        workspace.cars[car.code] = car
        cars.append(car)
    train.state = IntakeState.OPEN
    train.unplaced = []
    workspace.intakes[train.code] = train
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_RECEIVED,
        f"intake {train.code} received {len(cars)} cars",
        {"route": train.route, "car_count": len(cars), "arrival_at": train.arrival_at},
    )
    app.commit(workspace, event)
    return {
        "intake": train.to_dict(),
        "cars": [car.to_dict() for car in cars],
    }


def append_intake_cars(app: YardApplication, intake_code: str, payload: Any) -> dict[str, Any]:
    car_inputs = build_intake_append_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    train = workspace.intakes.get(intake_code)
    if train is None:
        raise NotFoundError("intake train", intake_code)
    if train.state != IntakeState.OPEN:
        raise ConflictError(
            f"intake {intake_code} is {train.state.value}; consist changes are only allowed while OPEN",
            code=intake_code,
            state=train.state.value,
        )
    if len(train.consist) + len(car_inputs) > MAX_TRAIN_CONSIST:
        raise ValidationError(
            "too many cars",
            **{
                "cars": [
                    f"consist would exceed {MAX_TRAIN_CONSIST} cars "
                    f"({len(train.consist)} on hand, {len(car_inputs)} appended)"
                ]
            },
        )
    for item in car_inputs:
        if item.code in workspace.cars:
            raise ConflictError("car code already exists", code=item.code)
    cars: list[FreightCar] = []
    for item in car_inputs:
        car = _car_from_input(item)
        workspace.cars[car.code] = car
        train.consist.append(car.code)
        cars.append(car)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_APPENDED,
        f"intake {train.code} appended {len(cars)} cars",
        {
            "route": train.route,
            "appended_count": len(cars),
            "car_codes": [car.code for car in cars],
            "car_count": len(train.consist),
        },
    )
    app.commit(workspace, event)
    return {
        "intake": train.to_dict(),
        "cars": [car.to_dict() for car in cars],
    }


def classify_intake_command(app: YardApplication, intake_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    train = workspace.intakes.get(intake_code)
    if train is None:
        raise NotFoundError("intake train", intake_code)
    if train.state == IntakeState.CANCELLED:
        raise ValidationError("cancelled intake cannot be classified", **{"intake_code": ["cancelled"]})
    if train.state == IntakeState.CLASSIFIED:
        raise ValidationError("intake is already classified", **{"intake_code": ["already classified"]})
    missing = [code for code in train.consist if code not in workspace.cars]
    if missing:
        raise ValidationError("consist references missing cars", **{"consist": missing})
    spots = classify_intake(train, workspace.cars, workspace.tracks)
    if train.unplaced:
        message = f"intake {train.code} partially classified with {len(train.unplaced)} unplaced cars"
    else:
        message = f"intake {train.code} fully classified"
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_CLASSIFIED,
        message,
        {
            "spotted": len(spots),
            "unplaced": list(train.unplaced),
            "spots": [item.to_dict() for item in spots],
        },
    )
    app.commit(workspace, event)
    return {
        "intake": train.to_dict(),
        "spots": [item.to_dict() for item in spots],
        "unplaced": list(train.unplaced),
    }


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


__all__ = ["append_intake_cars", "classify_intake_command", "create_intake"]
