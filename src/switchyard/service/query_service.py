"""Read-only query commands."""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError
from ..domain.validators import parse_metrics_query
from ..report.shift_metrics import shift_work_metrics
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
    return {"shift": shift.to_dict(), "events": events[-40:]}


def shift_metrics_view(
    app: YardApplication,
    shift_code: str,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only shift work metrics; never records events and never mutates state."""
    options = parse_metrics_query(query or {})
    workspace = app.load()
    return shift_work_metrics(
        workspace,
        shift_code,
        from_sequence=options["from_sequence"],
        to_sequence=options["to_sequence"],
        include_events=bool(options["include_events"]),
    )


__all__ = ["shift_metrics_view", "shift_view", "yard_view"]
