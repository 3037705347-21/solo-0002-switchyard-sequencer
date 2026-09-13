"""Safe replacement of one unexecuted car in a planned outbound consist."""

from __future__ import annotations

from dataclasses import dataclass

from .car import FreightCar
from .enums import CarState, OutboundState, RunState
from .errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from .outbound import OutboundTrain
from .pull import MoveStep, PullRun
from .sequencer import derive_pull_steps
from .track import BufferBay, StandingTrack
from .transitions import transition_car


@dataclass(slots=True)
class ReplacementResult:
    outbound: OutboundTrain
    run: PullRun
    old_car: FreightCar
    new_car: FreightCar
    steps: list[MoveStep]


def replace_planned_car(
    outbound: OutboundTrain,
    run: PullRun,
    old_code: str,
    new_code: str,
    cars: dict[str, FreightCar],
    tracks: dict[str, StandingTrack],
    buffer_bays: dict[str, BufferBay],
    active_outbounds: dict[str, OutboundTrain],
) -> ReplacementResult:
    if outbound.state != OutboundState.PLANNED:
        raise ConflictError(
            "only PLANNED outbound trains can replace a car",
            current_state=outbound.state.value,
        )
    if run.outbound_code != outbound.code:
        raise ResourceBusyError("pull run does not belong to the outbound train", run_code=run.code)
    if run.state not in {RunState.QUEUED, RunState.RUNNING}:
        raise ConflictError("only queued or running pull plans can replace a car", current_state=run.state.value)
    if run.current_step > len(run.steps):
        raise ValidationError("pull run cursor is beyond its recorded steps", **{"current_step": ["out of range"]})
    if old_code == new_code:
        raise ValidationError("replacement car must differ from the current car", **{"new_car_code": ["must differ"]})

    old = cars.get(old_code)
    if old is None:
        raise NotFoundError("car", old_code)
    new = cars.get(new_code)
    if new is None:
        raise NotFoundError("car", new_code)
    if old_code not in outbound.planned_car_codes:
        raise ValidationError("old car is not part of the outbound plan", **{"old_car_code": ["not planned"]})
    if new_code in outbound.planned_car_codes:
        raise ValidationError("new car is already part of the outbound plan", **{"new_car_code": ["already planned"]})

    old_index = outbound.planned_car_codes.index(old_code)
    assembled_count = len(outbound.assembled_car_codes)
    if outbound.assembled_car_codes != outbound.planned_car_codes[:assembled_count]:
        raise ResourceBusyError("assembled consist does not match the planned prefix")
    if old_index < assembled_count:
        raise ConflictError("old car has already been pulled and cannot be replaced", car_code=old_code)

    transfer = buffer_bays.get(run.transfer_code)
    if transfer is None:
        raise ResourceBusyError("pull run references missing transfer bay", transfer_code=run.transfer_code)

    # Replay executed prefixes against a physical snapshot, while recorded
    # BUFFER/RETURN pairs establish this run's active buffers. The actual live
    # stack may contain foreign cars moved after this run's prefix, so do not
    # require every historical buffer to be removable from the current snapshot.
    replay_bay: list[str] = []
    pulled: list[str] = []
    own_buffer_sources: dict[str, str] = {}
    executed_steps = run.steps[: run.current_step]
    for offset, step in enumerate(executed_steps):
        car = cars.get(step.car_code)
        if car is None:
            raise ResourceBusyError(f"executed step {offset} references missing car {step.car_code}")
        if step.target_code == run.transfer_code:
            source = tracks.get(step.source_code)
            if source is None:
                raise ResourceBusyError(f"executed step {offset} references missing track {step.source_code}")
            if not source.can_operate():
                raise ResourceBusyError(f"buffered car {step.car_code} came from {source.state.value} track {source.code}")
            own_buffer_sources[step.car_code] = step.source_code
            replay_bay.append(step.car_code)
            if car.state != CarState.STANDING:
                raise ResourceBusyError(f"buffered car {step.car_code} must remain standing")
            if car.location != transfer.code:
                raise ResourceBusyError(f"buffered car {step.car_code} is no longer in {transfer.code}")
        elif step.source_code == run.transfer_code:
            if replay_bay[-1:] != [step.car_code]:
                raise ResourceBusyError(f"executed return history for {step.car_code} no longer matches the bay")
            replay_bay.pop()
            recorded_source = own_buffer_sources.pop(step.car_code, None)
            if recorded_source is not None and recorded_source != step.target_code:
                raise ResourceBusyError(
                    f"return step for {step.car_code} uses {step.target_code}, not its buffer source {recorded_source}"
                )
            target = tracks.get(step.target_code)
            if target is None:
                raise ResourceBusyError(f"executed return targets missing track {step.target_code}")
            if car.state != CarState.STANDING or car.location != target.code:
                raise ResourceBusyError(f"returned car {step.car_code} is not standing on {target.code}")
        elif step.target_code == outbound.code:
            source = tracks.get(step.source_code)
            if source is None:
                raise ResourceBusyError(f"executed step {offset} references missing track {step.source_code}")
            if step.car_code in pulled:
                raise ResourceBusyError(f"executed pull history repeats car {step.car_code}")
            pulled.append(step.car_code)
            if car.state != CarState.ASSEMBLED or car.location != outbound.code:
                raise ResourceBusyError(f"pulled car {step.car_code} is not assembled on {outbound.code}")
        else:
            raise ResourceBusyError(f"executed step {offset} has an unexpected transfer/outbound target")

    if pulled != outbound.assembled_car_codes:
        raise ResourceBusyError("pull history does not match the assembled consist")

    actual_bay = list(transfer.stack)
    own_active_bottom_up = list(replay_bay)
    if own_active_bottom_up and actual_bay[-len(own_active_bottom_up) :] != own_active_bottom_up:
        raise ResourceBusyError(
            f"transfer bay {transfer.code} has foreign cars above this run's buffered cars",
            transfer_code=transfer.code,
        )
    own_active_set = set(own_active_bottom_up)
    for code in actual_bay:
        car = cars.get(code)
        if car is None:
            raise ResourceBusyError(f"transfer bay contains missing car {code}")
        if car.state != CarState.STANDING or car.location != transfer.code:
            raise ResourceBusyError(f"buffered car {code} is not standing in {transfer.code}")
    retained_buffer_count = len(actual_bay) - len(own_active_bottom_up)
    active_buffers = [(code, own_buffer_sources[code]) for code in own_active_bottom_up]

    # Re-check the old reservation/release conditions against the live yard.
    # The transition is not called yet; all checks below must be mutation-free.
    if old.state != CarState.RESERVED:
        raise ConflictError(f"old car {old_code} is not reserved", car_code=old_code, state=old.state.value)
    if old.destination != outbound.destination:
        raise ValidationError(
            f"old car {old_code} is no longer for destination {outbound.destination}",
            **{"old_car_code": ["destination mismatch"]},
        )
    old_location = old.location
    if old_location is None or old_location not in tracks:
        raise ValidationError("old car no longer has a standing track", **{"old_car_code": ["not on a track"]})
    old_track = tracks[old_location]
    if not old_track.can_operate():
        raise ResourceBusyError(f"old car is on {old_track.state.value} track {old_location}")
    if old_code not in old_track.stack:
        raise ValidationError("old car is no longer in its source track stack", **{"old_car_code": ["missing from stack"]})
    for other_code, other in active_outbounds.items():
        if other is outbound or other.state not in {OutboundState.PLANNED, OutboundState.READY}:
            continue
        if old_code in other.planned_car_codes:
            raise ResourceBusyError(
                f"old car {old_code} is also referenced by outbound train {other_code}",
                car_code=old_code,
                outbound_code=other_code,
            )

    # Validate the replacement car's station position and destination.
    if new.state != CarState.STANDING:
        raise ConflictError(f"new car {new_code} is not standing", car_code=new_code, state=new.state.value)
    if new.destination != outbound.destination:
        raise ValidationError(
            f"new car {new_code} is for {new.destination}, not {outbound.destination}",
            **{"new_car_code": ["destination mismatch"]},
        )
    new_location = new.location
    if new_location is None or new_location not in tracks:
        raise ValidationError("new car is not positioned on a standing track", **{"new_car_code": ["not on a track"]})
    new_track = tracks[new_location]
    if not new_track.can_operate():
        raise ResourceBusyError(f"new car is on {new_track.state.value} track {new_location}")
    if new_code not in new_track.stack:
        raise ValidationError("new car is not in its standing track stack", **{"new_car_code": ["missing from stack"]})
    if new_code in actual_bay:
        raise ResourceBusyError(f"new car {new_code} is currently buffered")
    for other_code, other in active_outbounds.items():
        if other is outbound or other.state not in {OutboundState.PLANNED, OutboundState.READY}:
            continue
        if new_code in other.planned_car_codes:
            raise ResourceBusyError(
                f"new car {new_code} is already reserved by outbound train {other_code}",
                car_code=new_code,
                outbound_code=other_code,
            )

    remaining = list(outbound.planned_car_codes[assembled_count:])
    remaining[old_index - assembled_count] = new_code
    suffix_steps = derive_pull_steps(
        outbound,
        remaining,
        cars,
        tracks,
        transfer,
        pending_target_code=new_code,
        released_codes={old_code},
        active_buffers=active_buffers,
        retained_buffer_count=retained_buffer_count,
    )

    # Validation is complete. Apply only the planned list, reservations, and
    # unexecuted run steps. Executed history and the current cursor are retained.
    transition_car(old, CarState.STANDING, "release before planned-car replacement")
    transition_car(new, CarState.RESERVED, "reserve replacement planned car")
    outbound.planned_car_codes[old_index] = new_code
    run.steps = executed_steps + suffix_steps

    return ReplacementResult(
        outbound=outbound,
        run=run,
        old_car=old,
        new_car=new,
        steps=suffix_steps,
    )


__all__ = ["ReplacementResult", "replace_planned_car"]
