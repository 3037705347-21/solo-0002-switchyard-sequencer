"""Shift opening and lookup commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, ShiftState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError
from ..domain.shift import YardShift
from ..domain.validators import build_shift_payload
from ..report.summary import live_or_frozen_statistics
from .context import YardApplication


def open_shift(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, dispatcher, opened_at = build_shift_payload(payload)
    workspace = app.load()
    if code in workspace.shifts:
        raise ConflictError("shift already exists", code=code)
    for shift in workspace.shifts.values():
        if shift.state == ShiftState.OPEN:
            raise ResourceBusyError(
                "another shift is still open",
                open_shift=shift.code,
            )
    shift = YardShift(code=code, dispatcher=dispatcher, opened_at=opened_at)
    workspace.shifts[code] = shift
    event = workspace.record_event(
        code,
        EventKind.SHIFT_OPENED,
        f"shift {code} opened by {dispatcher}",
        {"dispatcher": dispatcher, "opened_at": opened_at},
    )
    app.commit(workspace, event)
    return shift.to_dict()


def get_shift(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    shift = workspace.shifts.get(code)
    if shift is None:
        raise NotFoundError("shift", code)
    events = [event.to_dict() for event in workspace.events if event.shift_code == code]
    return {
        "shift": shift.to_dict(),
        "events": events[-40:],
        "shift_statistics": live_or_frozen_statistics(workspace, code),
    }


__all__ = ["get_shift", "open_shift"]
