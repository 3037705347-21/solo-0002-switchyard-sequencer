"""Shift opening and lookup commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, ShiftState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError
from ..domain.shift import YardShift
from ..domain.validators import build_shift_payload
from ..report.event_query import annotate_events
from .context import YardApplication
from .event_paging import cursor_from_params, has_constraints, parse_event_filter, query_events


def open_shift(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, dispatcher, opened_at = build_shift_payload(payload)
    with app.update() as workspace:
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
        workspace.record_event(
            code,
            EventKind.SHIFT_OPENED,
            f"shift {code} opened by {dispatcher}",
            {"dispatcher": dispatcher, "opened_at": opened_at, "shift_code": code},
        )
    return shift.to_dict()


def get_shift(app: YardApplication, code: str, params: Any = None) -> dict[str, Any]:
    # Preserve the original "shift + recent events" response when no
    # filter/paging parameters are present.
    if not has_constraints(params):
        workspace = app.load()
        shift = workspace.shifts.get(code)
        if shift is None:
            raise NotFoundError("shift", code)
        events = [event.to_dict() for event in workspace.events if event.shift_code == code]
        return {"shift": shift.to_dict(), "events": events[-40:]}

    event_filter = parse_event_filter(params)
    cursor = cursor_from_params(params)

    workspace = app.load()
    shift = workspace.shifts.get(code)
    if shift is None:
        raise NotFoundError("shift", code)
    page = query_events(workspace, code, event_filter, cursor)
    annotated = annotate_events(workspace, page.events)
    return {
        "shift": shift.to_dict(),
        "events": annotated,
        "total": page.total,
        "live_total": page.live_total,
        "new_event_count": page.new_event_count,
        "limit": page.limit,
        "anchor": page.anchor,
        "has_more": page.next_cursor is not None,
        "next_cursor": page.next_cursor,
        "filter": page.filter_echo,
    }


__all__ = ["get_shift", "open_shift"]
