"""Inverse operations for pull run steps after a mid-run failure.

The executor applies steps by mutating track stacks, the transfer bay, car
state, and the outbound consist. When a later step in the same advance fails,
the already-applied steps must be undone so the yard returns to the state at
the start of the advance, leaving only the failed run record behind.
"""

from __future__ import annotations

from ..storage.workspace import YardWorkspace
from .enums import CarState, MoveVerb, OutboundState
from .errors import ResourceBusyError
from .pull import MoveStep, PullRun
from .transitions import transition_car, transition_outbound


def undo_step(workspace: YardWorkspace, step: MoveStep) -> None:
    """Reverse a single step that execute_step() applied successfully.

    The undone steps belong to one failed advance only. Work committed by
    earlier advances of the same run stays in place, so a pulled car returns
    to the assembled state it had before this advance started.
    """
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


def rollback_run_attempt(
    workspace: YardWorkspace,
    run: PullRun,
    executed_steps: list[MoveStep],
    started_at_step: int = 0,
) -> None:
    """Undo the steps of one failed advance.

    When the failure happens in the first advance (the run never left step 0),
    the run reservation is released completely: planned cars return to
    standing and the outbound train returns to draft so a fresh retry attempt
    can be planned. A failure after earlier, already-committed advances only
    unwinds the latest segment and leaves the run FAILED for inspection.
    """
    for step in reversed(executed_steps):
        undo_step(workspace, step)
    if started_at_step == 0:
        release_run_reservation(workspace, run)


def release_run_reservation(workspace: YardWorkspace, run: PullRun) -> None:
    """Release every car reserved for a run and drop any partial assembly.

    Used before re-planning a retry. Pulled cars that are already assembled
    are popped from the outbound consist in reverse pull order and put back on
    their source tracks; every planned car ends standing.
    """
    outbound = workspace.outbounds.get(run.outbound_code)
    for step in reversed(run.steps):
        if step.verb != MoveVerb.PULL:
            continue
        car = workspace.cars.get(step.car_code)
        if car is None:
            continue
        if outbound is not None and outbound.assembled_car_codes and outbound.assembled_car_codes[-1] == step.car_code:
            outbound.assembled_car_codes.pop()
            source = workspace.tracks.get(step.source_code)
            if source is not None:
                source.stack.append(step.car_code)
                car.location = source.code
        if car.state == CarState.ASSEMBLED:
            transition_car(car, CarState.RESERVED)
        if car.state == CarState.RESERVED:
            transition_car(car, CarState.STANDING)
    if outbound is not None and outbound.state == OutboundState.PLANNED:
        transition_outbound(outbound, OutboundState.DRAFT)


__all__ = ["release_run_reservation", "rollback_run_attempt", "undo_step"]
