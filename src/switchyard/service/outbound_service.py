"""Outbound train creation and pull planning commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.outbound import OutboundTrain
from ..domain.replacer import replace_planned_car
from ..domain.sequencer import plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.validators import build_outbound_payload, parse_car_replacement, parse_transfer_code
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
    run = plan_pull_run(
        run_code,
        outbound,
        workspace.cars,
        workspace.tracks,
        workspace.buffer_bays,
        transfer_code,
    )
    workspace.runs[run.code] = run
    event = workspace.record_event(
        shift_code,
        EventKind.PULL_PLANNED,
        f"pull run {run.code} planned for {outbound.code}",
        {"steps": len(run.steps), "transfer_code": transfer_code},
    )
    app.commit(workspace, event)
    return {"pull_run": run.to_dict(), "outbound": outbound.to_dict()}


def replace_outbound_car(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    old_code, new_code = parse_car_replacement(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    run_code = f"RUN-{outbound.code}"
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    result = replace_planned_car(
        outbound=outbound,
        run=run,
        old_code=old_code,
        new_code=new_code,
        cars=workspace.cars,
        tracks=workspace.tracks,
        buffer_bays=workspace.buffer_bays,
        active_outbounds=workspace.outbounds,
    )
    event = workspace.record_event(
        shift_code,
        EventKind.PLANNED_CAR_REPLACED,
        f"replaced {old_code} with {new_code} in outbound {outbound.code}",
        {
            "outbound_code": outbound.code,
            "run_code": run.code,
            "old_car_code": old_code,
            "new_car_code": new_code,
            "current_step": run.current_step,
            "suffix_steps": len(result.steps),
        },
    )
    app.commit(workspace, event)
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "old_car": result.old_car.to_dict(),
        "new_car": result.new_car.to_dict(),
        "replaced_suffix_steps": len(result.steps),
    }


__all__ = ["create_outbound", "replace_outbound_car", "sequence_outbound"]
