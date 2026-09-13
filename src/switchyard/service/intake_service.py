"""Inbound train intake and classification commands."""

from __future__ import annotations

from typing import Any

from ..domain.allocator import classify_intake
from ..domain.car import CarInput, FreightCar
from ..domain.enums import CarKind, CarState, EventKind, IntakeState, OutboundState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.manifest import (
    CHANGE_ADDED,
    CHANGE_REMOVED,
    ManifestChange,
    ManifestVersion,
    car_spec_dict,
    diff_manifests,
)
from ..domain.timeutil import now_iso
from ..domain.validators import build_correction_payload, build_intake_payload
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
    train.manifest_version = 1
    workspace.intakes[train.code] = train
    _record_manifest_version(
        workspace,
        train,
        operator=workspace.shifts[shift_code].dispatcher,
        reason="initial manifest",
        car_inputs=car_inputs,
        changes=[
            ManifestChange(CHANGE_ADDED, item.code, after=car_spec_dict(item)) for item in car_inputs
        ],
    )
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


def correct_intake_manifest(app: YardApplication, intake_code: str, payload: Any) -> dict[str, Any]:
    operator, reason, inputs = build_correction_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    train = workspace.intakes.get(intake_code)
    if train is None:
        raise NotFoundError("intake train", intake_code)
    if train.state == IntakeState.CANCELLED:
        raise ValidationError("cancelled intake cannot be corrected", **{"intake_code": ["cancelled"]})
    missing = [code for code in train.consist if code not in workspace.cars]
    if missing:
        raise ValidationError("consist references missing cars", **{"consist": missing})
    current = {code: workspace.cars[code] for code in train.consist}
    before_specs = [car_spec_dict(current[code]) for code in train.consist]
    after_specs = [car_spec_dict(item) for item in inputs]
    changes = diff_manifests(before_specs, after_specs)
    if not changes:
        raise ValidationError(
            "manifest correction introduces no changes",
            **{"cars": ["identical to the current manifest"]},
        )
    outbound_refs = _outbound_references(workspace)
    conflicts = _correction_conflicts(changes, current, outbound_refs)
    if conflicts:
        raise ConflictError(
            "manifest correction touches classified or planned cars",
            conflicts=conflicts,
        )
    if train.state == IntakeState.CLASSIFIED:
        # Removals and updates of classified cars were rejected above; pure
        # additions would strand cars that can no longer be classified.
        raise ValidationError(
            "classified intake cannot be corrected",
            **{"intake_code": ["already classified"]},
        )
    for item in inputs:
        if item.code not in current and item.code in workspace.cars:
            raise ConflictError("car code already exists", code=item.code)
    for change in changes:
        if change.kind == CHANGE_REMOVED:
            del workspace.cars[change.car_code]
    for item in inputs:
        existing = workspace.cars.get(item.code)
        if existing is None:
            workspace.cars[item.code] = _car_from_input(item)
        else:
            _apply_car_input(existing, item)
    train.consist = [item.code for item in inputs]
    train.manifest_version += 1
    if train.state == IntakeState.PARTIAL:
        train.unplaced = [
            code for code in train.consist if workspace.cars[code].state == CarState.RECEIVED
        ]
    record = _record_manifest_version(workspace, train, operator, reason, inputs, changes)
    event = workspace.record_event(
        shift_code,
        EventKind.MANIFEST_CORRECTED,
        f"intake {train.code} manifest corrected to version {record.version}",
        {
            "version": record.version,
            "operator": operator,
            "reason": reason,
            "changes": [change.to_dict() for change in changes],
        },
    )
    app.commit(workspace, event)
    return {
        "intake": train.to_dict(),
        "manifest_version": record.to_dict(),
        "cars": [workspace.cars[code].to_dict() for code in train.consist],
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


def _record_manifest_version(
    workspace: Any,
    train: Any,
    operator: str,
    reason: str,
    car_inputs: list[CarInput],
    changes: list[ManifestChange],
) -> ManifestVersion:
    record = ManifestVersion(
        intake_code=train.code,
        version=train.manifest_version,
        recorded_at=now_iso(),
        operator=operator,
        reason=reason,
        car_codes=[item.code for item in car_inputs],
        cars=[car_spec_dict(item) for item in car_inputs],
        changes=changes,
    )
    workspace.manifest_versions.setdefault(train.code, []).append(record)
    return record


def _outbound_references(workspace: Any) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    for outbound in workspace.outbounds.values():
        if outbound.state in {OutboundState.DEPARTED, OutboundState.ABANDONED}:
            continue
        for car_code in list(outbound.planned_car_codes) + list(outbound.assembled_car_codes):
            references.setdefault(car_code, []).append(outbound.code)
    return references


def _correction_conflicts(
    changes: list[ManifestChange],
    current: dict[str, FreightCar],
    outbound_refs: dict[str, list[str]],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for change in changes:
        if change.kind == CHANGE_ADDED:
            continue
        car = current[change.car_code]
        refs = outbound_refs.get(change.car_code, [])
        if car.state == CarState.RECEIVED and not refs:
            continue
        reasons: list[str] = []
        if car.state != CarState.RECEIVED:
            reasons.append(f"car is {car.state.value}")
        if refs:
            reasons.append(f"referenced by outbound {', '.join(sorted(refs))}")
        conflicts.append(
            {
                "car_code": car.code,
                "change": change.kind,
                "state": str(car.state),
                "location": car.location,
                "outbound_codes": sorted(refs),
                "message": f"car {car.code} cannot be {change.kind}: {'; '.join(reasons)}",
            }
        )
    return conflicts


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


def _apply_car_input(car: FreightCar, item: CarInput) -> None:
    car.kind = CarKind.parse(item.kind)
    car.destination = item.destination
    car.loaded = item.loaded
    car.length_m = item.length_m
    car.danger_class = item.danger_class
    car.note = item.note


__all__ = ["classify_intake_command", "correct_intake_manifest", "create_intake"]
