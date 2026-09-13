"""State-changing execution of pull run and reorder order steps."""

from __future__ import annotations

from ..storage.workspace import YardWorkspace
from .car import FreightCar
from .enums import CarState, MoveVerb, RunState
from .errors import ResourceBusyError, StateTransitionError
from .pull import MoveStep, PullRun
from .reorder import ReorderOrder
from .rules import track_receives_car

StepRunner = PullRun | ReorderOrder


def _source_top_matches(workspace: YardWorkspace, source_code: str, car_code: str, step_kind: str) -> None:
    track = workspace.tracks.get(source_code)
    if track is None:
        raise ResourceBusyError(f"{step_kind} references missing track {source_code}")
    if track.top_code() != car_code:
        raise ResourceBusyError(
            f"{step_kind} requires {car_code} on top of {source_code}, found {track.top_code()}"
        )


def _bay_top_matches(workspace: YardWorkspace, bay_code: str, car_code: str, step_kind: str) -> None:
    bay = workspace.buffer_bays.get(bay_code)
    if bay is None:
        raise ResourceBusyError(f"{step_kind} references missing transfer bay {bay_code}")
    if bay.top_code() != car_code:
        raise ResourceBusyError(f"{step_kind} requires {car_code} on top of bay {bay_code}")


def execute_step(workspace: YardWorkspace, run: StepRunner, step: MoveStep) -> str:
    if run.state not in {RunState.QUEUED, RunState.RUNNING}:
        raise StateTransitionError("run", str(run.state), "RUNNING", "cannot execute finished run")
    car = workspace.cars.get(step.car_code)
    if car is None:
        raise ResourceBusyError(f"step references missing car {step.car_code}")
    if step.verb == MoveVerb.BUFFER:
        return _execute_buffer(workspace, run, step, car)
    if step.verb == MoveVerb.EXTRACT:
        return _execute_extract(workspace, run, step, car)
    if step.verb == MoveVerb.PULL:
        return _execute_pull(workspace, run, step, car)
    if step.verb == MoveVerb.RETURN:
        return _execute_return(workspace, run, step, car)
    raise ResourceBusyError(f"unknown move verb {step.verb}")


def _park_on_bay(workspace: YardWorkspace, step: MoveStep, car: FreightCar, step_kind: str) -> str:
    _source_top_matches(workspace, step.source_code, step.car_code, step_kind)
    bay = workspace.buffer_bays[step.target_code]
    if bay.remaining() <= 0:
        raise ResourceBusyError(f"transfer bay {bay.code} has no free capacity")
    source = workspace.tracks[step.source_code]
    popped = source.stack.pop()
    if popped != step.car_code:
        raise ResourceBusyError(f"unexpected top car {popped} while moving {step.car_code}")
    bay.stack.append(step.car_code)
    car.location = bay.code
    return bay.code


def _execute_buffer(workspace: YardWorkspace, run: StepRunner, step: MoveStep, car: FreightCar) -> str:
    if car.state != CarState.STANDING:
        raise StateTransitionError("car", str(car.state), "BUFFERED", "only standing cars can be buffered")
    bay_code = _park_on_bay(workspace, step, car, "buffer")
    return f"buffered {car.code} to {bay_code}"


def _execute_extract(workspace: YardWorkspace, run: StepRunner, step: MoveStep, car: FreightCar) -> str:
    if car.state != CarState.STANDING:
        raise StateTransitionError("car", str(car.state), "EXTRACTED", "only standing cars can be extracted")
    bay_code = _park_on_bay(workspace, step, car, "extract")
    return f"extracted {car.code} to {bay_code}"


def _execute_pull(workspace: YardWorkspace, run: StepRunner, step: MoveStep, car: FreightCar) -> str:
    if car.state != CarState.RESERVED:
        raise StateTransitionError("car", str(car.state), "ASSEMBLED", "planned car is not reserved")
    _source_top_matches(workspace, step.source_code, step.car_code, "pull")
    outbound = workspace.outbounds.get(step.target_code)
    if outbound is None:
        raise ResourceBusyError(f"pull targets missing outbound train {step.target_code}")
    source = workspace.tracks[step.source_code]
    popped = source.stack.pop()
    if popped != step.car_code:
        raise ResourceBusyError(f"unexpected top car {popped} while pulling {step.car_code}")
    outbound.assembled_car_codes.append(step.car_code)
    car.state = CarState.ASSEMBLED
    car.location = outbound.code
    return f"pulled {car.code} onto {outbound.code}"


def _execute_return(workspace: YardWorkspace, run: StepRunner, step: MoveStep, car: FreightCar) -> str:
    if car.state != CarState.STANDING:
        raise StateTransitionError("car", str(car.state), "RETURNED", "only standing cars can be returned")
    _bay_top_matches(workspace, step.source_code, step.car_code, "return")
    target = workspace.tracks.get(step.target_code)
    if target is None:
        raise ResourceBusyError(f"return targets missing standing track {step.target_code}")
    reason = track_receives_car(target, car, workspace.cars)
    if reason is not None:
        raise ResourceBusyError(f"return cannot place {car.code} on {target.code}: {reason}")
    bay = workspace.buffer_bays[step.source_code]
    popped = bay.stack.pop()
    if popped != step.car_code:
        raise ResourceBusyError(f"unexpected bay top {popped} while returning {step.car_code}")
    target.stack.append(step.car_code)
    car.location = target.code
    return f"returned {car.code} to {target.code}"


__all__ = ["execute_step"]
