"""Outbound train creation and pull planning commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, StateTransitionError, ValidationError
from ..domain.outbound import OutboundTrain
from ..domain.plan_revision import validate_draft_consist
from ..domain.sequencer import plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.validators import build_outbound_payload, build_outbound_revision_payload, parse_transfer_code
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


def revise_outbound_plan(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    """Replace the planned car sequence of a DRAFT outbound train.

    Every validation runs against the in-memory workspace before the list is
    touched, so a rejected revision neither reserves a car nor leaves the train
    half edited. PLANNED trains and later states are immutable here.
    """
    new_car_codes = build_outbound_revision_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state != OutboundState.DRAFT:
        raise StateTransitionError(
            "outbound train",
            outbound.state.value,
            OutboundState.DRAFT.value,
            "only DRAFT trains can revise planned cars",
        )
    occupied = _occupied_car_codes(workspace)
    occupied.difference_update(outbound.planned_car_codes)
    # Pure read-only check: raises before any mutation happens.
    validate_draft_consist(
        outbound.destination,
        new_car_codes,
        workspace.cars,
        workspace.tracks,
        occupied,
    )
    previous_codes = list(outbound.planned_car_codes)
    added = [code for code in new_car_codes if code not in previous_codes]
    removed = [code for code in previous_codes if code not in new_car_codes]
    reordered = added == [] and removed == [] and previous_codes != new_car_codes
    outbound.planned_car_codes = list(new_car_codes)
    event = workspace.record_event(
        shift_code,
        EventKind.PLAN_REVISED,
        f"outbound {outbound.code} draft plan revised",
        {
            "previous_car_codes": previous_codes,
            "planned_car_codes": list(new_car_codes),
            "added": added,
            "removed": removed,
            "reordered": reordered,
        },
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "changes": {
            "added": added,
            "removed": removed,
            "reordered": reordered,
            "previous_count": len(previous_codes),
            "planned_count": len(new_car_codes),
        },
        "planned_sequence": [
            {
                "position": index,
                "car_code": code,
                "track_code": workspace.cars[code].location,
                "destination": workspace.cars[code].destination,
            }
            for index, code in enumerate(new_car_codes, start=1)
        ],
    }


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


__all__ = ["create_outbound", "revise_outbound_plan", "sequence_outbound"]
