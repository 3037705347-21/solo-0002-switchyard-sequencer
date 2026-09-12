"""Pull run advancement and outbound departure commands."""

from __future__ import annotations

from typing import Any

from ..domain import operations
from ..domain.enums import EventKind
from ..domain.errors import NotFoundError
from ..domain.validators import parse_advance_steps
from .context import YardApplication, require_open_shift


def advance_run(app: YardApplication, run_code: str, payload: Any) -> dict[str, Any]:
    requested_steps = parse_advance_steps(payload)
    workspace = app.load()
    shift_code = require_open_shift(workspace, "open a shift before moving cars")
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    result = operations.advance_pull_run(workspace, run, requested_steps)
    outbound = result.outbound
    events: list[Any] = []
    if result.started:
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_STARTED,
                f"pull run {run_code} started",
                {"total_steps": len(run.steps)},
            )
        )
    if result.completed:
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_COMPLETED,
                f"pull run {run_code} completed",
                {
                    "assembled_car_codes": list(outbound.assembled_car_codes),
                    "steps": len(run.steps),
                },
            )
        )
    else:
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_ADVANCED,
                f"pull run {run_code} advanced {result.executed} steps",
                {
                    "current_step": run.current_step,
                    "remaining": run.remaining(),
                },
            )
        )
    app.commit(workspace, events)
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "executed_steps": result.executed,
        "completed": result.completed,
    }


def depart_outbound(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = require_open_shift(workspace, "open a shift before moving cars")
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    operations.depart_outbound(workspace, outbound)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_DEPARTED,
        f"outbound {outbound.code} departed for {outbound.destination}",
        {"car_count": len(outbound.assembled_car_codes), "departed_at": outbound.departed_at},
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "departed_car_count": len(outbound.assembled_car_codes),
    }


__all__ = ["advance_run", "depart_outbound"]
