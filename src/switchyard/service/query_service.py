"""Read-only query commands."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from ..domain.enums import EventKind
from ..domain.errors import NotFoundError
from ..report.run_view import build_run_listing, build_run_view, parse_state_filter
from ..report.summary import build_summary
from .context import YardApplication


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


def shift_view(app: YardApplication, shift_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    events = [event.to_dict() for event in workspace.events if event.shift_code == shift_code]
    return {"shift": shift.to_dict(), "events": events[-50:]}


def _query_value(query: dict[str, list[str]] | None, name: str) -> str | None:
    if not query:
        return None
    values = query.get(name)
    if not values:
        return None
    value = values[-1].strip()
    return value or None


def pull_run_view(app: YardApplication, run_code: str) -> dict[str, Any]:
    workspace = app.load()
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    return build_run_view(workspace, run)


def pull_run_listing(
    app: YardApplication,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    workspace = app.load()
    raw_shift = _query_value(query, "shift")
    shift_code = raw_shift.upper() if raw_shift is not None else None
    states = parse_state_filter(_query_value(query, "state"))
    return build_run_listing(workspace, shift_code=shift_code, states=states)


def parse_query_string(query_string: str) -> dict[str, list[str]]:
    return parse_qs(query_string, keep_blank_values=True)


__all__ = [
    "parse_query_string",
    "pull_run_listing",
    "pull_run_view",
    "shift_view",
    "yard_view",
]
