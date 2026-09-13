"""Shift closure command with blocker checks."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, ShiftState
from ..domain.errors import NotFoundError, ResourceBusyError
from ..domain.transitions import transition_shift
from ..report.closure import closure_blockers
from ..report.summary import snapshot_document
from .context import YardApplication


def close_shift(app: YardApplication, shift_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    if shift.state == ShiftState.CLOSED:
        raise ResourceBusyError("shift is already closed", shift_code=shift_code)
    blockers = closure_blockers(workspace)
    if blockers:
        event = workspace.record_event(
            shift_code,
            EventKind.CLOSURE_BLOCKED,
            f"closure for {shift_code} blocked by {len(blockers)} item(s)",
            {"shift_code": shift_code, "attempt": _block_attempt_count(workspace, shift_code) + 1,
             "blocker_count": len(blockers), "blockers": blockers},
        )
        app.commit(workspace, event)
        raise ResourceBusyError("shift closure is blocked", blockers=blockers)
    snapshot_code = f"SNAP-{shift_code}"
    closed_event = workspace.record_event(
        shift_code,
        EventKind.SHIFT_CLOSED,
        f"shift {shift_code} closed",
        {"shift_code": shift_code, "snapshot_code": snapshot_code},
    )
    closed_at = closed_event.at
    transition_shift(shift, ShiftState.CLOSED)
    shift.closed_at = closed_at
    shift.closure_snapshot_code = snapshot_code
    document = snapshot_document(workspace, shift_code, snapshot_code, closed_at)
    workspace.closure_snapshots.append(document)
    app.commit(workspace, closed_event)
    return {
        "shift": shift.to_dict(),
        "snapshot": document,
        "metrics": document["metrics"],
    }


def _block_attempt_count(workspace: Any, shift_code: str) -> int:
    return sum(
        1
        for event in workspace.events
        if event.shift_code == shift_code and str(event.kind) == "CLOSURE_BLOCKED"
    )


__all__ = ["close_shift"]
