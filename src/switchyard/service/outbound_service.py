"""Outbound train creation and pull planning commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.outbound import OutboundTrain
from ..domain.sequencer import plan_pull_run
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_car, transition_outbound, transition_run
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
        if workspace.active_deactivation(car_code) is not None:
            raise ResourceBusyError(
                f"car {car_code} is out of service and cannot be selected for outbound planning",
                car_code=car_code,
            )
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
    withdrawn = [code for code in outbound.planned_car_codes if workspace.active_deactivation(code) is not None]
    if withdrawn:
        raise ResourceBusyError(
            "planned car(s) are out of service",
            car_codes=withdrawn,
        )
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


def _fail_runs_for_outbound(workspace: Any, outbound: OutboundTrain) -> list[str]:
    failed: list[str] = []
    for run_code in outbound.run_codes:
        run = workspace.runs.get(run_code)
        if run is None:
            continue
        if run.state in {RunState.QUEUED, RunState.RUNNING}:
            transition_run(run, RunState.FAILED, reason=f"outbound {outbound.code} abandoned")
            run.error = f"outbound {outbound.code} abandoned"
            failed.append(run_code)
    return failed


def _source_track_for_assembled(workspace: Any, outbound: OutboundTrain) -> dict[str, str]:
    """Recover each assembled car's source standing track from completed runs."""

    source_of: dict[str, str] = {}
    for run_code in outbound.run_codes:
        run = workspace.runs.get(run_code)
        if run is None:
            continue
        for step in run.steps:
            verb = str(step.verb)
            if verb == "PULL" and step.car_code in outbound.assembled_car_codes:
                source_of[step.car_code] = step.source_code
    return source_of


def abandon_outbound(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    """Release every car reference held by an outbound plan.

    DRAFT plans hold no car state. PLANNED plans release reservations back to
    their tracks (and fail any queued pull run). READY trains return the
    assembled cars to their source tracks; an in-flight run is rejected rather
    than leaving cars half-moved.
    """

    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state == OutboundState.DEPARTED:
        raise ValidationError(
            "departed outbound cannot be abandoned",
            **{"outbound_code": ["already departed"]},
        )
    if outbound.state == OutboundState.ABANDONED:
        raise ConflictError("outbound is already abandoned", code=outbound_code)
    active_run = next(
        (
            workspace.runs[code]
            for code in outbound.run_codes
            if code in workspace.runs and workspace.runs[code].state in {RunState.QUEUED, RunState.RUNNING}
        ),
        None,
    )
    if outbound.state == OutboundState.READY and active_run is not None:
        raise ResourceBusyError(
            f"pull run {active_run.code} is still {active_run.state.value}; complete it before abandoning",
            pull_run=active_run.code,
        )

    released_to_track: list[str] = []
    returned_to_track: list[dict[str, str]] = []
    failed_runs: list[str] = []

    if outbound.state in {OutboundState.PLANNED, OutboundState.READY}:
        # A queued or running run may have left blocker cars in a transfer bay;
        # abandoning cannot rewind their stack positions, so the caller must
        # advance the run to a point where every bay is empty instead.
        for bay in workspace.buffer_bays.values():
            stranded = [code for code in bay.stack if code in workspace.cars]
            if stranded:
                raise ResourceBusyError(
                    f"transfer bay {bay.code} holds {len(stranded)} buffered car(s); finish the run instead",
                    bay_code=bay.code,
                    car_codes=stranded,
                )

        # Assembled cars (a partially or fully executed run) go back onto the
        # source tracks recorded by the run's PULL steps before reservations
        # are released.
        source_of = _source_track_for_assembled(workspace, outbound)
        missing_source = [code for code in outbound.assembled_car_codes if code not in source_of]
        if missing_source:
            raise ResourceBusyError(
                "assembled cars have no recoverable source track",
                car_codes=missing_source,
            )
        for code in outbound.assembled_car_codes:
            car = workspace.cars.get(code)
            track = workspace.tracks.get(source_of[code])
            if car is None or track is None:
                raise ResourceBusyError(f"cannot return car {code} to its source track")
            if len(track.stack) + 1 > track.capacity_cars:
                raise ResourceBusyError(f"track {track.code} is at car capacity", track_code=track.code)
            used_length = sum(
                workspace.cars[item].length_m for item in track.stack if item in workspace.cars
            )
            if used_length + car.length_m > track.capacity_length_m:
                raise ResourceBusyError(f"track {track.code} is at length capacity", track_code=track.code)

        if outbound.state == OutboundState.PLANNED:
            failed_runs = _fail_runs_for_outbound(workspace, outbound)

        for code in outbound.planned_car_codes:
            car = workspace.cars.get(code)
            if car is None:
                continue
            if car.state == CarState.ASSEMBLED:
                track_code = source_of[code]
                track = workspace.tracks[track_code]
                track.stack.append(code)
                car.location = track_code
                transition_car(car, CarState.STANDING, reason=f"outbound {outbound_code} abandoned")
                returned_to_track.append({"car_code": code, "track_code": track_code})
            elif car.state == CarState.RESERVED:
                transition_car(car, CarState.STANDING, reason=f"outbound {outbound_code} abandoned")
                released_to_track.append(code)

    transition_outbound(outbound, OutboundState.ABANDONED)
    event = workspace.record_event(
        shift_code,
        EventKind.OUTBOUND_ABANDONED,
        f"outbound {outbound_code} abandoned; {len(released_to_track)} reservation(s) released, "
        f"{len(returned_to_track)} assembled car(s) returned",
        {
            "outbound_code": outbound_code,
            "released_reserved": released_to_track,
            "returned_assembled": returned_to_track,
            "failed_runs": failed_runs,
        },
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "released_reserved": released_to_track,
        "returned_assembled": returned_to_track,
        "failed_runs": failed_runs,
    }


__all__ = ["abandon_outbound", "create_outbound", "sequence_outbound"]
