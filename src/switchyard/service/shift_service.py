"""Shift opening and lookup commands."""

from __future__ import annotations

from typing import Any

from ..domain import operations
from ..domain.enums import EventKind
from ..domain.errors import NotFoundError
from ..domain.shift import YardShift
from ..domain.validators import build_shift_payload
from .context import YardApplication


def open_shift(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, dispatcher, opened_at = build_shift_payload(payload)
    workspace = app.load()
    shift = YardShift(code=code, dispatcher=dispatcher, opened_at=opened_at)
    operations.open_shift(workspace, shift)
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
    return {"shift": shift.to_dict(), "events": events[-40:]}


__all__ = ["get_shift", "open_shift"]
