"""Inverse operations for pull run steps after a failed attempt.

A failed pull run is rolled back to the state before the attempt started,
even when earlier advances of the same attempt already committed buffer,
pull, and return actions. Steps are undone in reverse application order so
LIFO stacks are restored exactly. After the undo pass every planned car is
standing on its source track, the transfer bay is empty of this run's cars,
the outbound consist is cleared, and the outbound train returns to draft,
which lets a fresh retry attempt be planned without any manual repair.
"""

from __future__ import annotations

from ..storage.workspace import YardWorkspace
from .enums import CarState, MoveVerb, OutboundState
from .errors import ResourceBusyError
from .pull import MoveStep, PullRun
from .transitions import transition_car, transition_outbound


def undo_step(workspace: YardWorkspace, step: MoveStep) -> None:
    """Reverse a single step that execute_step() applied successfully."""
    if step.verb == MoveVerb.BUFFER:
        bay = workspace.buffer_bays.get(step.target_code)
        if bay is None:
            raise ResourceBusyError(f"undo references missing transfer bay {step.target_code}")
        if bay.top_code() != step.car_code:
            raise ResourceBusyError(
                f"undo buffer requires {step.car_code} on top of bay {bay.code}, found {bay.top_code()}"
            )
        bay.stack.pop()
        source = workspace.tracks.get(step.source_code)
        if source is None:
            raise ResourceBusyError(f"undo references missing track {step.source_code}")
        source.stack.append(step.car_code)
        car = workspace.cars[step.car_code]
        car.location = source.code
        return
    if step.verb == MoveVerb.PULL:
        outbound = workspace.outbounds.get(step.target_code)
        if outbound is None:
            raise ResourceBusyError(f"undo references missing outbound train {step.target_code}")
        if not outbound.assembled_car_codes or outbound.assembled_car_codes[-1] != step.car_code:
            raise ResourceBusyError(
                f"undo pull requires {step.car_code} last assembled on {outbound.code}"
            )
        outbound.assembled_car_codes.pop()
        source = workspace.tracks.get(step.source_code)
        if source is None:
            raise ResourceBusyError(f"undo references missing track {step.source_code}")
        source.stack.append(step.car_code)
        car = workspace.cars[step.car_code]
        transition_car(car, CarState.RESERVED)
        car.location = source.code
        return
    if step.verb == MoveVerb.RETURN:
        target = workspace.tracks.get(step.target_code)
        if target is None:
            raise ResourceBusyError(f"undo references missing track {step.target_code}")
        if target.top_code() != step.car_code:
            raise ResourceBusyError(
                f"undo return requires {step.car_code} on top of track {target.code}, found {target.top_code()}"
            )
        target.stack.pop()
        bay = workspace.buffer_bays.get(step.source_code)
        if bay is None:
            raise ResourceBusyError(f"undo references missing transfer bay {step.source_code}")
        bay.stack.append(step.car_code)
        car = workspace.cars[step.car_code]
        car.location = bay.code
        return
    raise ResourceBusyError(f"unknown move verb {step.verb}")


def rollback_attempt(
    workspace: YardWorkspace,
    run: PullRun,
    applied_steps: list[MoveStep],
) -> None:
    """Undo every applied step of an attempt and release the reservation.

    ``applied_steps`` is the full sequence of steps already applied across
    all advances of this attempt (``run.steps[:run.current_step]``). They are
    undone in reverse order, which restores every track stack and the transfer
    bay to its pre-attempt state. Planned cars return to standing and the
    outbound train returns to draft so a fresh retry attempt can be planned.
    """
    for step in reversed(applied_steps):
        undo_step(workspace, step)

    outbound = workspace.outbounds.get(run.outbound_code)
    for step in run.steps:
        if step.verb != MoveVerb.PULL:
            continue
        car = workspace.cars.get(step.car_code)
        if car is None:
            continue
        if car.state == CarState.ASSEMBLED:
            transition_car(car, CarState.RESERVED)
        if car.state == CarState.RESERVED:
            transition_car(car, CarState.STANDING)
        if isinstance(step.source_code, str) and step.source_code in workspace.tracks:
            car.location = step.source_code
    if outbound is not None:
        outbound.assembled_car_codes.clear()
        if outbound.state == OutboundState.PLANNED:
            transition_outbound(outbound, OutboundState.DRAFT)


__all__ = ["rollback_attempt", "undo_step"]
