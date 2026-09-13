"""Outbound train creation and pull planning commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.outbound import OutboundTrain
from ..domain.sequencer import plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.validators import build_outbound_payload, parse_replan_payload, parse_transfer_code
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


def replan_outbound(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    """Replace the planned consist and derive a fresh pull run.

    Used to correct a plan-vs-actual conflict (e.g. a car pulled for a defect
    hold) before any shunting step is executed. The previous queued run is
    marked failed, its reservations are released back to standing, and a new
    run is sequenced. Published manifests are never rewritten; they keep
    pointing at the run codes captured at publication time.
    """

    transfer_code, car_codes = parse_replan_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state not in {OutboundState.PLANNED, OutboundState.DRAFT}:
        raise ValidationError(
            "only a confirmed but unexecuted plan can be replanned",
            **{"outbound_code": [f"current state is {outbound.state.value}"]},
        )
    if transfer_code not in workspace.buffer_bays:
        raise ValidationError("unknown transfer bay", **{"transfer_code": ["not found"]})

    old_run: Any = None
    if outbound.run_codes:
        old_run = workspace.runs.get(outbound.run_codes[-1])
    if old_run is not None and old_run.current_step > 0:
        raise ResourceBusyError(
            f"pull run {old_run.code} has already executed {old_run.current_step} step(s); cannot replan",
            pull_run=old_run.code,
        )
    if old_run is not None and old_run.state == RunState.COMPLETED:
        raise ResourceBusyError("pull run is already complete", pull_run=old_run.code)

    old_planned = list(outbound.planned_car_codes)
    new_set = set(car_codes)
    # No shunting step has run, so every previously planned car is still parked
    # in its source stack; release all reservations so the sequencer can take
    # them fresh. Cars dropped from the plan (including removed cars) stay as
    # they are unless they are reserved here.
    for code in old_planned:
        car = workspace.cars.get(code)
        if car is not None and car.state == CarState.RESERVED:
            car.state = CarState.STANDING
    # Cars newly added must be standing and destination-compatible.
    occupied = _occupied_car_codes(workspace) - set(old_planned)
    for code in car_codes:
        car = workspace.cars.get(code)
        if car is None:
            raise ValidationError("planned cars do not exist", **{"car_codes": [code]})
        if code not in old_planned:
            if car.state != CarState.STANDING:
                raise ValidationError(
                    f"car {code} is not standing",
                    **{"car_codes": [f"{code} is {car.state.value}"]},
                )
            if car.destination != outbound.destination:
                raise ValidationError(
                    f"car {code} is for {car.destination}, not {outbound.destination}",
                    **{"car_codes": [f"{code} destination mismatch"]},
                )
            if car.location not in workspace.tracks or code not in workspace.tracks[car.location].stack:
                raise ValidationError(
                    f"car {code} is not stacked on a standing track",
                    **{"car_codes": [f"{code} has no stack location"]},
                )
            if code in occupied:
                raise ResourceBusyError(
                    f"car {code} is already planned on another outbound train",
                    car_code=code,
                )

    if old_run is not None:
        old_run.state = RunState.FAILED
        old_run.error = "superseded by replan"
    # Roll the outbound back to draft so the sequencer can transition it.
    outbound.state = OutboundState.DRAFT
    outbound.assembled_car_codes = []
    outbound.planned_car_codes = list(car_codes)
    # Cars still in the plan are reserved again by the sequencer; any that
    # were released above stay standing.
    suffix = 2
    while True:
        run_code = f"RUN-{outbound.code}-{suffix}"
        if run_code not in workspace.runs:
            break
        suffix += 1
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
        EventKind.PULL_REPLANNED,
        f"outbound {outbound.code} replanned with run {run.code}",
        {
            "superseded_run": old_run.code if old_run is not None else None,
            "new_run": run.code,
            "old_planned_car_codes": old_planned,
            "planned_car_codes": list(car_codes),
            "steps": len(run.steps),
        },
    )
    app.commit(workspace, event)
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "superseded_run": old_run.code if old_run is not None else None,
    }


__all__ = ["create_outbound", "replan_outbound", "sequence_outbound"]
