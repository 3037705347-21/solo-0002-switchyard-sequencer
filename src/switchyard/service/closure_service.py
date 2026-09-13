"""Shift closure command with blocker checks."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, ShiftState
from ..domain.errors import NotFoundError, ResourceBusyError
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_shift
from ..report.closure import closure_blockers
from ..report.precheck import closure_precheck
from ..report.summary import snapshot_document
from .context import YardApplication


def precheck_closure(app: YardApplication, shift_code: str) -> dict[str, Any]:
    """Preview closure blockers without mutating shifts, cars, runs, or events."""
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    if shift.state == ShiftState.CLOSED:
        raise ResourceBusyError("shift is already closed", shift_code=shift_code)
    blockers = closure_precheck(workspace)
    return {
        "shift": shift.to_dict(),
        "ready": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
    }


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
            {"blockers": blockers},
        )
        app.commit(workspace, event)
        raise ResourceBusyError("shift closure is blocked", blockers=blockers)
    snapshot_code = f"SNAP-{shift_code}"
    document = snapshot_document(workspace, shift_code, snapshot_code)
    workspace.closure_snapshots.append(document)
    closed_at = now_iso()
    transition_shift(shift, ShiftState.CLOSED)
    shift.closed_at = closed_at
    shift.closure_snapshot_code = snapshot_code
    event = workspace.record_event(
        shift_code,
        EventKind.SHIFT_CLOSED,
        f"shift {shift_code} closed",
        {"snapshot_code": snapshot_code, "closed_at": closed_at},
    )
    app.commit(workspace, event)
    return {
        "shift": shift.to_dict(),
        "snapshot": document,
        "metrics": document["metrics"],
    }


__all__ = ["close_shift", "precheck_closure"]
