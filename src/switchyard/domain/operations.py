"""Workflow behaviors that own yard state changes.

Each function enforces the domain rules for one command, moves the
affected aggregates through the transition tables, and returns the
action result. Service commands load the workspace, validate the
request context, call exactly one behavior, and commit once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .allocator import SpotRecord, classify_intake
from .car import CarInput, FreightCar
from .enums import CarKind, CarState, IntakeState, OutboundState, RunState, ShiftState
from .errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from .executor import execute_step
from .intake import IntakeTrain
from .outbound import OutboundTrain
from .pull import PullRun
from .sequencer import plan_pull_run
from .shift import YardShift
from .timeutil import now_iso
from .transitions import transition_car, transition_outbound, transition_run, transition_shift


def receive_intake(workspace: Any, train: IntakeTrain, car_inputs: list[CarInput]) -> list[FreightCar]:
    """Register a new intake train and its received cars."""
    if train.code in workspace.intakes:
        raise ConflictError("intake train already exists", code=train.code)
    for item in car_inputs:
        if item.code in workspace.cars:
            raise ConflictError("car code already exists", code=item.code)
    cars = [_car_from_input(item) for item in car_inputs]
    for car in cars:
        workspace.cars[car.code] = car
    train.state = IntakeState.OPEN
    train.unplaced = []
    workspace.intakes[train.code] = train
    return cars


def classify_consist(workspace: Any, train: IntakeTrain) -> list[SpotRecord]:
    """Place every receivable car of an intake onto standing tracks."""
    if train.state == IntakeState.CANCELLED:
        raise ValidationError("cancelled intake cannot be classified", **{"intake_code": ["cancelled"]})
    if train.state == IntakeState.CLASSIFIED:
        raise ValidationError("intake is already classified", **{"intake_code": ["already classified"]})
    missing = [code for code in train.consist if code not in workspace.cars]
    if missing:
        raise ValidationError("consist references missing cars", **{"consist": missing})
    return classify_intake(train, workspace.cars, workspace.tracks)


def draft_outbound(workspace: Any, code: str, destination: str, car_codes: list[str]) -> OutboundTrain:
    """Create a draft outbound train from standing, unclaimed cars."""
    if code in workspace.outbounds:
        raise ConflictError("outbound train already exists", code=code)
    claimed = _claimed_car_codes(workspace)
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
        if car_code in claimed:
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
    return train


def plan_outbound(workspace: Any, outbound: OutboundTrain, transfer_code: str) -> PullRun:
    """Derive a pull run for a draft outbound and reserve its cars."""
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
    return run


@dataclass(slots=True)
class AdvanceResult:
    """Outcome of advancing a pull run."""

    run: PullRun
    outbound: OutboundTrain
    started: bool
    executed: int
    completed: bool


def advance_pull_run(workspace: Any, run: PullRun, requested_steps: int) -> AdvanceResult:
    """Execute up to ``requested_steps`` moves and settle the run state."""
    if run.state == RunState.COMPLETED:
        raise ConflictError("pull run is already complete", code=run.code)
    if run.state == RunState.FAILED:
        raise ConflictError("pull run has failed", code=run.code)
    started = False
    if run.state == RunState.QUEUED:
        transition_run(run, RunState.RUNNING)
        run.started_at = now_iso()
        started = True
    executed = 0
    while executed < requested_steps and run.current_step < len(run.steps):
        step = run.steps[run.current_step]
        execute_step(workspace, run, step)
        run.current_step += 1
        executed += 1
    outbound = workspace.outbounds.get(run.outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", run.outbound_code)
    completed = run.current_step >= len(run.steps)
    if completed:
        if not outbound.assembly_complete():
            raise ValidationError(
                "pull run finished without matching the planned consist",
                **{"assembled": outbound.assembled_car_codes},
            )
        transition_run(run, RunState.COMPLETED)
        run.completed_at = now_iso()
        transition_outbound(outbound, OutboundState.READY)
    return AdvanceResult(run=run, outbound=outbound, started=started, executed=executed, completed=completed)


def depart_outbound(workspace: Any, outbound: OutboundTrain) -> None:
    """Mark a ready outbound departed along with its assembled cars."""
    if outbound.state != OutboundState.READY:
        raise ValidationError(
            "outbound train is not ready",
            **{"outbound_code": [f"current state is {outbound.state.value}"]},
        )
    if not outbound.assembly_complete():
        raise ValidationError("outbound assembly is incomplete", **{"assembled": outbound.assembled_car_codes})
    for code in outbound.assembled_car_codes:
        car = workspace.cars.get(code)
        if car is None or car.state != CarState.ASSEMBLED:
            raise ValidationError(
                f"assembled car {code} is not in assembled state",
                **{"assembled": [code]},
            )
    departed_at = now_iso()
    transition_outbound(outbound, OutboundState.DEPARTED)
    outbound.departed_at = departed_at
    for code in outbound.assembled_car_codes:
        transition_car(workspace.cars[code], CarState.DEPARTED)


def open_shift(workspace: Any, shift: YardShift) -> None:
    """Register a new shift while no other shift is open."""
    if shift.code in workspace.shifts:
        raise ConflictError("shift already exists", code=shift.code)
    for existing in workspace.shifts.values():
        if existing.state == ShiftState.OPEN:
            raise ResourceBusyError(
                "another shift is still open",
                open_shift=existing.code,
            )
    workspace.shifts[shift.code] = shift


def ensure_shift_closable(shift: YardShift) -> None:
    """Reject closure for a shift that already closed."""
    if shift.state == ShiftState.CLOSED:
        raise ResourceBusyError("shift is already closed", shift_code=shift.code)


def close_shift(workspace: Any, shift: YardShift, snapshot: dict[str, Any], closed_at: str) -> None:
    """Close the shift and attach its closure snapshot."""
    workspace.closure_snapshots.append(snapshot)
    transition_shift(shift, ShiftState.CLOSED)
    shift.closed_at = closed_at
    shift.closure_snapshot_code = str(snapshot["code"])


def _claimed_car_codes(workspace: Any) -> set[str]:
    claimed: set[str] = set()
    for outbound in workspace.outbounds.values():
        if outbound.state in {OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.READY}:
            claimed.update(outbound.planned_car_codes)
    return claimed


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


__all__ = [
    "AdvanceResult",
    "advance_pull_run",
    "classify_consist",
    "close_shift",
    "depart_outbound",
    "draft_outbound",
    "ensure_shift_closable",
    "open_shift",
    "plan_outbound",
    "receive_intake",
]
