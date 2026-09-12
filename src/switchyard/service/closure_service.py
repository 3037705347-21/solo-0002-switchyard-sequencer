"""Shift closure command with blocker checks."""

from __future__ import annotations

from typing import Any

from ..domain import operations
from ..domain.enums import EventKind
from ..domain.errors import NotFoundError, ResourceBusyError
from ..domain.timeutil import now_iso
from ..report.closure import closure_blockers
from ..report.summary import snapshot_document
from .context import YardApplication


def close_shift(app: YardApplication, shift_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    operations.ensure_shift_closable(shift)
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
    closed_at = now_iso()
    operations.close_shift(workspace, shift, document, closed_at)
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


__all__ = ["close_shift"]
