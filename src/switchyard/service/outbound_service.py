"""Outbound train creation, pull planning, and plan lifecycle commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.outbound import OutboundTrain
from ..domain.reservation import (
    ReservationRecord,
    active_for_car,
    release_active,
)
from ..domain.sequencer import plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_car, transition_outbound
from ..domain.validators import build_outbound_payload, parse_transfer_code
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before planning")


def _holder_for_car(workspace: Any, car_code: str) -> dict[str, Any] | None:
    record = active_for_car(workspace.reservations, car_code)
    if record is not None:
        return {
            "outbound_code": record.outbound_code,
            "run_code": record.run_code,
            "reservation_code": record.code,
            "destination": record.destination,
            "track_code": record.track_code,
            "frozen_at": record.frozen_at,
            "state": "PLANNED",
        }
    for outbound in workspace.outbounds.values():
        if outbound.state in {OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.READY}:
            if car_code in outbound.planned_car_codes:
                return {
                    "outbound_code": outbound.code,
                    "run_code": None,
                    "reservation_code": None,
                    "destination": outbound.destination,
                    "track_code": None,
                    "frozen_at": None,
                    "state": str(outbound.state),
                }
    return None


def create_outbound(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, destination, car_codes = build_outbound_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    if code in workspace.outbounds:
        raise ConflictError("outbound train already exists", code=code)
    missing: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for car_code in car_codes:
        car = workspace.cars.get(car_code)
        if car is None:
            missing.append(car_code)
            continue
        holder = _holder_for_car(workspace, car_code)
        if holder is not None:
            conflicts.append(
                {
                    "car_code": car_code,
                    "held_by": holder,
                    "requested_by": {"outbound_code": code, "destination": destination},
                }
            )
            continue
        if car.state != CarState.STANDING:
            raise ValidationError(
                f"car {car_code} is not standing",
                **{"car_codes": [f"{car_code} is {car.state.value}"]},
            )
        if car.destination != destination:
            raise ValidationError(
                f"car {car_code} is for {car.destination}, not {destination}",
                **{"car_codes": [f"{car_code} destination mismatch"]},
            )
        if car.location not in workspace.tracks or car_code not in workspace.tracks[car.location].stack:
            raise ValidationError(
                f"car {car_code} is not stacked on a standing track",
                **{"car_codes": [f"{car_code} has no stack location"]},
            )
    if missing:
        raise ValidationError("planned cars do not exist", **{"car_codes": missing})
    if conflicts:
        cars_text = ", ".join(item["car_code"] for item in conflicts)
        raise ResourceBusyError(
            f"cars already reserved or planned by another outbound train: {cars_text}",
            conflicts=conflicts,
        )
    train = OutboundTrain(
        code=code,
        destination=destination,
        planned_car_codes=car_codes,
        state=OutboundState.DRAFT,
        created_at=now_iso(),
    )
    workspace.outbounds[code] = train
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_CREATED,
        f"outbound {code} drafted for {destination}",
        {"destination": destination, "planned_count": len(car_codes)},
    )
    app.commit(workspace, event)
    return train.to_dict()


def sequence_outbound(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    transfer_code = parse_transfer_code(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state != OutboundState.DRAFT:
        raise ValidationError(
            "outbound train already has a plan",
            **{"outbound_code": [f"current state is {outbound.state.value}"]},
        )
    if transfer_code not in workspace.buffer_bays:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})
    run_code = f"RUN-{outbound.code}"
    if run_code in workspace.runs:
        raise ConflictError("pull run already exists", code=run_code)
    plan = plan_pull_run(
        run_code,
        outbound,
        workspace.cars,
        workspace.tracks,
        workspace.buffer_bays,
        transfer_code,
    )
    workspace.runs[plan.run.code] = plan.run
    frozen_at = now_iso()
    records: list[ReservationRecord] = []
    for car_code in outbound.planned_car_codes:
        car = workspace.cars[car_code]
        record = ReservationRecord(
            code=workspace.allocate_reservation_code(),
            car_code=car_code,
            outbound_code=outbound.code,
            run_code=plan.run.code,
            destination=outbound.destination,
            track_code=str(car.location),
            frozen_at=frozen_at,
            actions=list(plan.actions_by_car.get(car_code, [])),
        )
        workspace.reservations[record.code] = record
        records.append(record)
    planned_event = workspace.record_event(
        shift_code,
        EventKind.PULL_PLANNED,
        f"pull run {plan.run.code} planned for {outbound.code}",
        {"steps": len(plan.run.steps), "transfer_code": transfer_code},
    )
    frozen_event = workspace.record_event(
        shift_code,
        EventKind.RESERVATION_FROZEN,
        f"{len(records)} reservation(s) frozen for {outbound.code}",
        {
            "reservation_codes": [record.code for record in records],
            "car_codes": [record.car_code for record in records],
            "frozen_at": frozen_at,
        },
    )
    app.commit(workspace, [planned_event, frozen_event])
    return {
        "pull_run": plan.run.to_dict(),
        "outbound": outbound.to_dict(),
        "reservations": [record.to_dict() for record in records],
    }


def _release_plan(workspace: Any, outbound: Any, reason: str) -> list[ReservationRecord]:
    run_code = f"RUN-{outbound.code}"
    run = workspace.runs.get(run_code)
    if run is not None and run.state != RunState.QUEUED:
        raise ResourceBusyError(
            f"pull run {run.code} is {run.state.value} and cannot be unwound",
            run_code=run.code,
        )
    released_at = now_iso()
    released = release_active(workspace.reservations, outbound.code, reason, released_at)
    for record in released:
        car = workspace.cars.get(record.car_code)
        if car is not None and car.state == CarState.RESERVED:
            transition_car(car, CarState.STANDING, reason=f"reservation released: {reason}")
    if run is not None:
        del workspace.runs[run_code]
        if run_code in outbound.run_codes:
            outbound.run_codes.remove(run_code)
    return released


def replan_outbound(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state != OutboundState.PLANNED:
        raise ValidationError(
            "only a planned outbound train can be replanned",
            **{"outbound_code": [f"current state is {outbound.state.value}"]},
        )
    released = _release_plan(workspace, outbound, "replanned")
    transition_outbound(outbound, OutboundState.DRAFT, reason="replanned")
    event = workspace.record_event(
        shift_code,
        EventKind.RESERVATION_RELEASED,
        f"{len(released)} reservation(s) released for replan of {outbound.code}",
        {
            "reservation_codes": [record.code for record in released],
            "car_codes": [record.car_code for record in released],
            "reason": "replanned",
        },
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "released_reservations": [record.to_dict() for record in released],
    }


def cancel_outbound(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state not in {OutboundState.DRAFT, OutboundState.PLANNED}:
        raise ValidationError(
            "only a draft or planned outbound train can be cancelled",
            **{"outbound_code": [f"current state is {outbound.state.value}"]},
        )
    released: list[ReservationRecord] = []
    if outbound.state == OutboundState.PLANNED:
        released = _release_plan(workspace, outbound, "cancelled")
    transition_outbound(outbound, OutboundState.ABANDONED, reason="cancelled")
    event = workspace.record_event(
        shift_code,
        EventKind.RESERVATION_RELEASED,
        f"outbound {outbound.code} cancelled; {len(released)} reservation(s) released",
        {
            "reservation_codes": [record.code for record in released],
            "car_codes": [record.car_code for record in released],
            "reason": "cancelled",
        },
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "released_reservations": [record.to_dict() for record in released],
    }


__all__ = ["cancel_outbound", "create_outbound", "replan_outbound", "sequence_outbound"]
