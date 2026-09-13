"""Read-only query commands."""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError
from ..report.event_query import annotate_events
from ..report.summary import build_summary
from .context import YardApplication
from .event_paging import cursor_from_params, has_constraints, parse_event_filter, query_events


def yard_view(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    shift_codes = [code for code, shift in workspace.shifts.items() if str(shift.state) == "OPEN"]
    active_shift = shift_codes[0] if shift_codes else "NONE"
    summary = build_summary(workspace, active_shift)
    return {
        "active_shift": active_shift,
        "metrics": summary["metrics"],
        "blockers": summary["blockers"],
        "shifts": [shift.to_dict() for shift in workspace.shifts.values()],
    }


def shift_view(app: YardApplication, shift_code: str, params: Any = None) -> dict[str, Any]:
    if not has_constraints(params):
        workspace = app.load()
        shift = workspace.shifts.get(shift_code)
        if shift is None:
            raise NotFoundError("shift", shift_code)
        events = [event.to_dict() for event in workspace.events if event.shift_code == shift_code]
        return {"shift": shift.to_dict(), "events": events[-50:]}

    event_filter = parse_event_filter(params)
    cursor = cursor_from_params(params)
    workspace = app.load()
    if shift_code not in workspace.shifts:
        raise NotFoundError("shift", shift_code)
    page = query_events(workspace, shift_code, event_filter, cursor)
    return {
        "shift": workspace.shifts[shift_code].to_dict(),
        "events": annotate_events(workspace, page.events),
        "total": page.total,
        "live_total": page.live_total,
        "new_event_count": page.new_event_count,
        "limit": page.limit,
        "anchor": page.anchor,
        "has_more": page.next_cursor is not None,
        "next_cursor": page.next_cursor,
        "filter": page.filter_echo,
    }


__all__ = ["shift_view", "yard_view"]
