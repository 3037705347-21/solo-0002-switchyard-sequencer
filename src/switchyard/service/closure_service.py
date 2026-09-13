"""Shift closure command with blocker checks."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, ShiftState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError
from ..domain.timeutil import now_iso
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
            {"blockers": blockers},
        )
        app.commit(workspace, event)
        raise ResourceBusyError("shift closure is blocked", blockers=blockers)
    snapshot_code = f"SNAP-{shift_code}"
    existing_codes = {str(doc.get("code")) for doc in workspace.closure_snapshots}
    if snapshot_code in existing_codes:
        # Snapshots are immutable: a new shift lifecycle must never overwrite
        # or duplicate an archived document.
        raise ConflictError(
            "an immutable closure snapshot already exists for this shift",
            snapshot_code=snapshot_code,
        )
    closed_at = now_iso()
    # Record the closure event before snapshotting so the snapshot's source
    # event range covers the SHIFT_CLOSED event itself.
    closed_event = workspace.record_event(
        shift_code,
        EventKind.SHIFT_CLOSED,
        f"shift {shift_code} closed",
        {"snapshot_code": snapshot_code, "closed_at": closed_at},
    )
    # The snapshot freezes the workspace version produced by the closure event.
    document = snapshot_document(workspace, shift_code, snapshot_code, closed_at)
    workspace.closure_snapshots.append(document)
    transition_shift(shift, ShiftState.CLOSED)
    shift.closed_at = closed_at
    shift.closure_snapshot_code = snapshot_code
    app.commit(workspace, closed_event)
    return {
        "shift": shift.to_dict(),
        "snapshot": document,
        "metrics": document["metrics"],
    }


__all__ = ["close_shift"]
