"""Explicit transition tables for domain state machines."""

from __future__ import annotations

from .car import FreightCar
from .enums import (
    CarState,
    IntakeState,
    OutboundState,
    RunState,
    ShiftState,
)
from .errors import StateTransitionError
from .intake import IntakeTrain
from .outbound import OutboundTrain
from .pull import PullRun
from .shift import YardShift


def _check(current: str, target: str, allowed: dict[str, set[str]], entity: str, reason: str | None = None) -> None:
    targets = allowed.get(current)
    if targets is None or target not in targets:
        raise StateTransitionError(entity, current, target, reason)


def transition_car(car: FreightCar, target: CarState, reason: str | None = None) -> None:
    allowed = {
        CarState.RECEIVED.value: {CarState.STANDING.value, CarState.REMOVED.value},
        CarState.STANDING.value: {
            CarState.RESERVED.value,
            CarState.REMOVED.value,
        },
        CarState.RESERVED.value: {
            CarState.ASSEMBLED.value,
            CarState.STANDING.value,
            CarState.REMOVED.value,
        },
        CarState.ASSEMBLED.value: {CarState.DEPARTED.value, CarState.REMOVED.value},
        CarState.DEPARTED.value: {CarState.REMOVED.value},
        CarState.REMOVED.value: set(),
    }
    _check(str(car.state), str(target), allowed, "car", reason)
    car.state = target


def transition_intake(train: IntakeTrain, target: IntakeState, reason: str | None = None) -> None:
    allowed = {
        IntakeState.OPEN.value: {IntakeState.PARTIAL.value, IntakeState.CLASSIFIED.value, IntakeState.CANCELLED.value},
        IntakeState.PARTIAL.value: {IntakeState.PARTIAL.value, IntakeState.CLASSIFIED.value, IntakeState.CANCELLED.value},
        IntakeState.CLASSIFIED.value: set(),
        IntakeState.CANCELLED.value: set(),
    }
    _check(str(train.state), str(target), allowed, "intake train", reason)
    train.state = target


def transition_outbound(train: OutboundTrain, target: OutboundState, reason: str | None = None) -> None:
    allowed = {
        OutboundState.DRAFT.value: {OutboundState.PLANNED.value, OutboundState.ABANDONED.value},
        OutboundState.PLANNED.value: {OutboundState.READY.value, OutboundState.DRAFT.value, OutboundState.ABANDONED.value},
        OutboundState.READY.value: {OutboundState.DEPARTED.value, OutboundState.ABANDONED.value},
        OutboundState.DEPARTED.value: set(),
        OutboundState.ABANDONED.value: set(),
    }
    _check(str(train.state), str(target), allowed, "outbound train", reason)
    train.state = target


def transition_run(run: PullRun, target: RunState, reason: str | None = None) -> None:
    allowed = {
        RunState.QUEUED.value: {RunState.RUNNING.value, RunState.FAILED.value},
        RunState.RUNNING.value: {RunState.COMPLETED.value, RunState.FAILED.value},
        RunState.COMPLETED.value: set(),
        RunState.FAILED.value: set(),
    }
    _check(str(run.state), str(target), allowed, "pull run", reason)
    run.state = target


def transition_shift(shift: YardShift, target: ShiftState, reason: str | None = None) -> None:
    allowed = {
        ShiftState.OPEN.value: {ShiftState.CLOSED.value},
        ShiftState.CLOSED.value: set(),
    }
    _check(str(shift.state), str(target), allowed, "shift", reason)
    shift.state = target


__all__ = [
    "transition_car",
    "transition_intake",
    "transition_outbound",
    "transition_run",
    "transition_shift",
]
