"""Read-only pull run views derived from a persisted workspace snapshot.

Every helper here computes from a loaded workspace and never mutates it, so
repeated queries (even from a different terminal or after a service restart)
return the same plan and execution state without retriggering any action.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, MoveVerb, RunState

STEP_PENDING = "PENDING"
STEP_DONE = "DONE"
STEP_CURRENT = "CURRENT"
STEP_SKIPPED = "SKIPPED"

RUN_STATES = tuple(item.value for item in RunState)


def resolve_shift_code(workspace: Any, run: Any) -> str | None:
    """Return the shift a run belongs to, recovering legacy runs without the field."""
    if run.shift_code:
        return run.shift_code
    for event in reversed(workspace.events):
        if event.kind != EventKind.PULL_PLANNED:
            continue
        if event.payload.get("run_code") == run.code or f"pull run {run.code} " in event.message:
            return event.shift_code
    return None


def _car_summary(workspace: Any, car_code: str) -> dict[str, Any] | None:
    car = workspace.cars.get(car_code)
    if car is None:
        return None
    return {
        "code": car.code,
        "kind": str(car.kind),
        "destination": car.destination,
        "state": str(car.state),
        "location": car.location,
        "loaded": car.loaded,
        "length_m": car.length_m,
        "danger_class": car.danger_class,
    }


def _step_blocked_reason(workspace: Any, run: Any, step: Any) -> str | None:
    """Pre-check the next action without moving any car."""
    if run.state != RunState.RUNNING:
        return None
    car = workspace.cars.get(step.car_code)
    if car is None:
        return f"car {step.car_code} no longer exists"
    if step.verb == MoveVerb.BUFFER:
        if str(car.state) != "STANDING":
            return f"car {step.car_code} is {car.state}, only a standing car can be buffered"
        track = workspace.tracks.get(step.source_code)
        if track is None:
            return f"source track {step.source_code} no longer exists"
        top = track.top_code()
        if top != step.car_code:
            return f"top of {step.source_code} is {top}, expected {step.car_code}"
        bay = workspace.buffer_bays.get(step.target_code)
        if bay is None:
            return f"transfer bay {step.target_code} no longer exists"
        if bay.remaining() <= 0:
            return f"transfer bay {bay.code} is full"
        return None
    if step.verb == MoveVerb.PULL:
        if str(car.state) != "RESERVED":
            return f"car {step.car_code} is {car.state}, it must stay reserved until pulled"
        track = workspace.tracks.get(step.source_code)
        if track is None:
            return f"source track {step.source_code} no longer exists"
        top = track.top_code()
        if top != step.car_code:
            return f"top of {step.source_code} is {top}, expected {step.car_code}"
        if step.target_code not in workspace.outbounds:
            return f"outbound train {step.target_code} no longer exists"
        return None
    if step.verb == MoveVerb.RETURN:
        if str(car.state) != "STANDING":
            return f"car {step.car_code} is {car.state}, only a buffered standing car can be returned"
        bay = workspace.buffer_bays.get(step.source_code)
        if bay is None:
            return f"transfer bay {step.source_code} no longer exists"
        top = bay.top_code()
        if top != step.car_code:
            return f"top of bay {bay.code} is {top}, expected {step.car_code}"
        target = workspace.tracks.get(step.target_code)
        if target is None:
            return f"return track {step.target_code} no longer exists"
        return None
    return f"unknown move verb {step.verb}"


def _step_status(index: int, run: Any) -> str:
    if index < run.current_step:
        return STEP_DONE
    if run.state == RunState.COMPLETED:
        return STEP_DONE if index < len(run.steps) else STEP_PENDING
    if run.state == RunState.FAILED:
        return STEP_CURRENT if index == run.current_step else (
            STEP_DONE if index < run.current_step else STEP_SKIPPED
        )
    if run.state == RunState.RUNNING and index == run.current_step:
        return STEP_CURRENT
    if run.state == RunState.QUEUED and index == run.current_step:
        return STEP_CURRENT
    return STEP_PENDING


def _blocked_reason(workspace: Any, run: Any, current: Any, readiness: str) -> str | None:
    if run.state == RunState.FAILED:
        return run.error or f"execution failed at step {run.current_step + 1}"
    if run.state == RunState.QUEUED:
        return "queued: waiting for the first advance"
    if run.state == RunState.COMPLETED:
        return None
    if run.state == RunState.RUNNING and readiness != "READY":
        return current["blocked_reason"] if current else "next action cannot be executed"
    return None


def _current_operation(workspace: Any, run: Any) -> dict[str, Any] | None:
    step = run.active_step()
    if step is None or run.state == RunState.COMPLETED:
        return None
    index = run.current_step
    blocked = _step_blocked_reason(workspace, run, step)
    readiness = "READY" if blocked is None else "BLOCKED"
    if run.state == RunState.FAILED:
        readiness = "FAILED"
    elif run.state == RunState.QUEUED:
        readiness = "WAITING"
    return {
        "step_number": index + 1,
        "verb": str(step.verb),
        "car_code": step.car_code,
        "car": _car_summary(workspace, step.car_code),
        "source_code": step.source_code,
        "target_code": step.target_code,
        "readiness": readiness,
        "blocked_reason": blocked,
    }


def build_step_views(workspace: Any, run: Any) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for index, step in enumerate(run.steps):
        status = _step_status(index, run)
        views.append(
            {
                "step_number": index + 1,
                "verb": str(step.verb),
                "car_code": step.car_code,
                "source_code": step.source_code,
                "target_code": step.target_code,
                "status": status,
                "car": _car_summary(workspace, step.car_code),
            }
        )
    return views


def build_run_view(workspace: Any, run: Any) -> dict[str, Any]:
    outbound = workspace.outbounds.get(run.outbound_code)
    current = _current_operation(workspace, run)
    readiness = current["readiness"] if current else "READY"
    blocked_reason = _blocked_reason(workspace, run, current, readiness)
    completed_steps = min(run.current_step, len(run.steps))
    view = {
        "code": run.code,
        "shift_code": resolve_shift_code(workspace, run),
        "state": str(run.state),
        "transfer_code": run.transfer_code,
        "outbound_code": run.outbound_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "failed_at": run.failed_at,
        "progress": {
            "total_steps": len(run.steps),
            "completed_steps": completed_steps,
            "remaining_steps": run.remaining(),
            "current_step": run.current_step + 1 if current else None,
        },
        "current_operation": current,
        "readiness": readiness,
        "blocked_reason": blocked_reason,
        "error": run.error,
        "steps": build_step_views(workspace, run),
        "outbound": None,
    }
    if outbound is not None:
        view["outbound"] = {
            "code": outbound.code,
            "destination": outbound.destination,
            "state": str(outbound.state),
            "planned_car_codes": list(outbound.planned_car_codes),
            "assembled_car_codes": list(outbound.assembled_car_codes),
            "planned_count": len(outbound.planned_car_codes),
            "assembled_count": len(outbound.assembled_car_codes),
            "remaining_car_codes": [
                code for code in outbound.planned_car_codes if code not in outbound.assembled_car_codes
            ],
            "departed_at": outbound.departed_at,
            "run_codes": list(outbound.run_codes),
            "assembled_cars": [
                car for code in outbound.assembled_car_codes if (car := _car_summary(workspace, code)) is not None
            ],
        }
    return view


def parse_state_filter(raw: str | None) -> list[str] | None:
    """Validate a comma-separated run-state filter; None means no filter."""
    if raw is None:
        return None
    requested = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not requested:
        return None
    invalid = [part for part in requested if part not in RUN_STATES]
    if invalid:
        from ..domain.errors import ValidationError

        raise ValidationError(
            "unknown pull run state filter",
            **{"state": [f"{part} is not one of {', '.join(RUN_STATES)}" for part in invalid]},
        )
    return requested


def build_run_listing(
    workspace: Any,
    shift_code: str | None = None,
    states: list[str] | None = None,
) -> dict[str, Any]:
    state_set = set(states) if states else None
    items: list[dict[str, Any]] = []
    for run in workspace.runs.values():
        view = build_run_view(workspace, run)
        if shift_code is not None and view["shift_code"] != shift_code:
            continue
        if state_set is not None and view["state"] not in state_set:
            continue
        items.append(view)
    items.sort(key=lambda item: (item["created_at"], item["code"]))
    counts = {state.value: 0 for state in RunState}
    for run in workspace.runs.values():
        counts[str(run.state)] += 1
    return {
        "filters": {
            "shift_code": shift_code,
            "states": sorted(state_set) if state_set else [],
        },
        "count": len(items),
        "state_counts": counts,
        "pull_runs": items,
    }


__all__ = [
    "STEP_CURRENT",
    "STEP_DONE",
    "STEP_PENDING",
    "STEP_SKIPPED",
    "build_run_listing",
    "build_run_view",
    "parse_state_filter",
    "resolve_shift_code",
]
