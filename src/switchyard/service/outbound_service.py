"""Outbound train creation and pull planning commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.outbound import OutboundTrain
from ..domain.sequencer import derive_plan_shape, plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.transfer import (
    TransferCapacityError,
    reconcile_transfer_occupancy,
    record_reservation,
    select_transfer_line,
)
from ..domain.validators import build_outbound_payload, parse_transfer_code
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before planning")


def _occupied_car_codes(workspace: Any) -> set[str]:
    occupied: set[str] = set()
    for outbound in workspace.outbounds.values():
        if outbound.state in {OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.READY}:
            occupied.update(outbound.planned_car_codes)
    return occupied


def create_outbound(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, destination, car_codes = build_outbound_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    if code in workspace.outbounds:
        raise ConflictError("outbound train already exists", code=code)
    occupied = _occupied_car_codes(workspace)
    missing: list[str] = []
    for car_code in car_codes:
        car = workspace.cars.get(car_code)
        if car is None:
            missing.append(car_code)
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
        if car_code in occupied:
            raise ResourceBusyError(
                f"car {car_code} is already planned on another outbound train",
                car_code=car_code,
            )
    if missing:
        raise ValidationError("planned cars do not exist", **{"car_codes": missing})
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


def _next_run_code(workspace: Any, outbound_code: str) -> str:
    base = f"RUN-{outbound_code}"
    if base not in workspace.runs:
        return base
    attempt = 2
    while f"{base}-{attempt}" in workspace.runs:
        attempt += 1
    return f"{base}-{attempt}"


def sequence_outbound(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    preferred_code = parse_transfer_code(payload)
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
    if preferred_code is not None and preferred_code not in workspace.buffer_bays:
        raise NotFoundError("transfer line", preferred_code)
    # Recompute committed holds from persisted runs first so planning always
    # works against positions and plans already on record (covers restart).
    reconcile_transfer_occupancy(workspace)
    # Car-level validation first (missing car, blocker reserved elsewhere);
    # those failures are independent of transfer-line capacity.
    shape = derive_plan_shape(outbound, workspace.cars, workspace.tracks)
    required = shape.peak_bay_occupancy
    chosen_code, chosen, evaluations = select_transfer_line(
        required,
        workspace.buffer_bays,
        workspace.runs,
        preferred_code=preferred_code,
        reservations=workspace.transfer_reservations,
    )
    if chosen_code is None:
        raise TransferCapacityError(required, evaluations, preferred_code)
    run_code = _next_run_code(workspace, outbound.code)
    run = plan_pull_run(
        run_code,
        outbound,
        workspace.cars,
        workspace.tracks,
        workspace.buffer_bays,
        chosen_code,
    )
    workspace.runs[run.code] = run
    record_reservation(workspace, run, required, chosen.slack)
    event = workspace.record_event(
        shift_code,
        EventKind.PULL_PLANNED,
        f"pull run {run.code} planned for {outbound.code}",
        {
            "steps": len(run.steps),
            "transfer_code": chosen_code,
            "required_slots": required,
            "slack_slots": chosen.slack,
            "selection": "requested" if preferred_code is not None else "best_fit",
            "evaluated": [
                {
                    "code": item.code,
                    "feasible": item.feasible,
                    "reason": item.reason,
                    "available_cars": item.available_cars,
                }
                for item in evaluations
            ],
        },
    )
    app.commit(workspace, event)
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "transfer": {
            "code": chosen_code,
            "required_slots": required,
            "slack_slots": chosen.slack,
            "available_cars": chosen.available_cars,
            "capacity_cars": chosen.capacity_cars,
            "selection": "requested" if preferred_code is not None else "best_fit",
            "evaluations": [
                {
                    "code": item.code,
                    "feasible": item.feasible,
                    "reason": item.reason,
                    "capacity_cars": item.capacity_cars,
                    "physical_cars": item.physical_cars,
                    "committed_cars": item.committed_cars,
                    "available_cars": item.available_cars,
                }
                for item in evaluations
            ],
        },
    }


__all__ = ["create_outbound", "sequence_outbound"]
