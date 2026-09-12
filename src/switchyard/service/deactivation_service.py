"""Vehicle deactivation (withdrawal/hold) and recovery workflow commands."""

from __future__ import annotations

from typing import Any

from ..domain.deactivation import CarDeactivation
from ..domain.enums import (
    CarState,
    DeactivationKind,
    DeactivationStatus,
    EventKind,
)
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError
from ..domain.locator import collect_conflicts, locate_car
from ..domain.rules import track_receives_car
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_car
from ..domain.validators import build_deactivation_payload, build_recovery_payload
from .context import YardApplication

OUT_OF_SERVICE_LOCATION = "OUT_OF_SERVICE"


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before deactivation work")


def _deactivation_view(record: CarDeactivation) -> dict[str, Any]:
    return record.to_dict()


def deactivate_car(app: YardApplication, payload: Any) -> dict[str, Any]:
    car_code, kind, reason, operator = build_deactivation_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    car = workspace.cars.get(car_code)
    if car is None:
        raise NotFoundError("car", car_code)
    active = workspace.active_deactivation(car_code)
    if active is not None:
        raise ConflictError(
            f"car {car_code} is already out of service ({active.code})",
            deactivation_code=active.code,
        )
    sightings = locate_car(workspace, car_code)
    conflicts = collect_conflicts(workspace, car_code)
    if conflicts:
        # Reject while keeping every reference intact; still leave an audit
        # trail of the attempt on the next successful deactivation record.
        event = workspace.record_event(
            shift_code,
            EventKind.CAR_DEACTIVATION_BLOCKED,
            f"deactivation of {car_code} blocked by {len(conflicts)} reference(s)",
            {
                "car_code": car_code,
                "kind": str(kind),
                "reason": reason,
                "operator": operator,
                "conflicts": conflicts,
            },
        )
        app.commit(workspace, event)
        raise ResourceBusyError(
            f"car {car_code} has {len(conflicts)} active reference(s); resolve them before deactivation",
            car_code=car_code,
            conflicts=conflicts,
            attribution=sightings,
        )
    if car.state == CarState.REMOVED:
        raise ConflictError(f"car {car_code} is already removed", car_code=car_code)
    if car.state != CarState.STANDING:
        # Defensive: locator conflicts should have covered every other state.
        raise ValidationError(
            f"car {car_code} cannot be deactivated from state {car.state.value}",
            **{"car_code": [f"current state is {car.state.value}"]},
        )
    standing = sightings["standing_track"]
    if standing is None:
        raise ConflictError(f"car {car_code} is present on no standing track", car_code=car_code)

    record_code = f"DEA-{workspace.next_deactivation_sequence:04d}"
    workspace.next_deactivation_sequence += 1
    record = CarDeactivation(
        code=record_code,
        car_code=car_code,
        kind=kind,
        reason=reason,
        operator=operator,
        status=DeactivationStatus.ACTIVE,
        requested_at=now_iso(),
        prior_state=car.state.value,
        prior_location=standing["track_code"],
        restore_index=int(standing["stack_index"]),
    )
    workspace.deactivations[record_code] = record
    record.blocked_attempts = sum(
        1
        for event in workspace.events
        if str(event.kind) == "CAR_DEACTIVATION_BLOCKED"
        and event.payload.get("car_code") == car_code
    )

    track = workspace.tracks[standing["track_code"]]
    popped = track.stack.pop(standing["stack_index"])
    if popped != car_code:
        raise ResourceBusyError(f"unexpected stack change while deactivating {car_code}")
    transition_car(car, CarState.REMOVED, reason=f"deactivation {record_code}")
    car.location = OUT_OF_SERVICE_LOCATION
    event = workspace.record_event(
        shift_code,
        EventKind.CAR_DEACTIVATED,
        f"car {car_code} deactivated ({kind.value}) from {track.code} by {operator}",
        {
            "deactivation_code": record_code,
            "car_code": car_code,
            "kind": str(kind),
            "reason": reason,
            "operator": operator,
            "prior_state": record.prior_state,
            "prior_location": record.prior_location,
            "restore_index": record.restore_index,
            "attribution": sightings,
        },
    )
    app.commit(workspace, event)
    return {
        "deactivation": _deactivation_view(record),
        "car": car.to_dict(),
        "attribution": sightings,
        "conflicts": [],
    }


def recover_car(app: YardApplication, payload: Any) -> dict[str, Any]:
    car_code, reason, operator, requested_track = build_recovery_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    car = workspace.cars.get(car_code)
    if car is None:
        raise NotFoundError("car", car_code)
    record = workspace.active_deactivation(car_code)
    if record is None:
        raise ConflictError(
            f"car {car_code} has no active deactivation record",
            car_code=car_code,
        )

    def _block(message: str, reason_code: str) -> ResourceBusyError:
        event = workspace.record_event(
            shift_code,
            EventKind.CAR_RECOVERY_BLOCKED,
            f"recovery of {car_code} blocked: {reason_code}",
            {
                "car_code": car_code,
                "deactivation_code": record.code,
                "reason": reason,
                "operator": operator,
                "blocker": reason_code,
            },
        )
        app.commit(workspace, event)
        return ResourceBusyError(message, car_code=car_code, blocker=reason_code)

    if record.kind == DeactivationKind.RETIRE:
        raise _block(
            f"car {car_code} was permanently withdrawn (RETIRE) and cannot be recovered",
            "permanent_retirement",
        )
    if car.state != CarState.REMOVED:
        raise ConflictError(
            f"car {car_code} is not out of service",
            car_code=car_code,
            current_state=car.state.value,
        )

    original_code = record.prior_location
    track_code = requested_track or original_code
    track = workspace.tracks.get(track_code) if track_code else None
    if track is None:
        raise _block(
            f"recovery track {track_code} does not exist",
            "recovery_track_missing",
        )
    if car_code in track.stack:
        raise ConflictError(f"car {car_code} is already back on {track_code}", car_code=car_code)
    refusal = track_receives_car(track, car, workspace.cars)
    if refusal is not None:
        raise _block(refusal, "track_unavailable")

    use_original = requested_track is None and track_code == original_code and record.restore_index is not None
    if use_original:
        insert_at = min(max(0, int(record.restore_index)), len(track.stack))
    else:
        insert_at = len(track.stack)
    track.stack.insert(insert_at, car_code)
    car.location = track.code
    transition_car(car, CarState.STANDING, reason=f"recovery of deactivation {record.code}")
    record.status = DeactivationStatus.RECOVERED
    record.recovered_at = now_iso()
    record.recovery_reason = reason
    record.recovery_operator = operator
    event = workspace.record_event(
        shift_code,
        EventKind.CAR_RECOVERED,
        f"car {car_code} recovered to {track.code} by {operator}",
        {
            "deactivation_code": record.code,
            "car_code": car_code,
            "reason": reason,
            "operator": operator,
            "restored_track": track.code,
            "requested_track": requested_track,
            "original_track": original_code,
            "stack_index": insert_at,
        },
    )
    app.commit(workspace, event)
    return {
        "deactivation": _deactivation_view(record),
        "car": car.to_dict(),
        "restored": {"track_code": track.code, "stack_index": insert_at},
    }


def get_deactivation(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    record = workspace.deactivations.get(code)
    if record is None:
        raise NotFoundError("deactivation", code)
    car = workspace.cars.get(record.car_code)
    return {
        "deactivation": record.to_dict(),
        "car": None if car is None else car.to_dict(),
        "attribution": locate_car(workspace, record.car_code),
    }


def list_deactivations(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    records = sorted(workspace.deactivations.values(), key=lambda item: item.code)
    return {
        "deactivations": [record.to_dict() for record in records],
        "active_car_codes": sorted(
            record.car_code
            for record in records
            if str(record.status) == "ACTIVE"
        ),
    }


__all__ = [
    "deactivate_car",
    "get_deactivation",
    "list_deactivations",
    "recover_car",
]
