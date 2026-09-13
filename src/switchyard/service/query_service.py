"""Read-only query commands."""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError
from ..report.shift_stats import shift_statistics_from_events
from ..report.summary import build_summary, frozen_snapshot_for, live_or_frozen_statistics
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
        "shift_statistics": live_or_frozen_statistics(workspace, active_shift) if shift_codes else None,
        "shifts": [shift.to_dict() for shift in workspace.shifts.values()],
    }


def _shift_events(workspace: Any, shift_code: str) -> list[Any]:
    return [event for event in workspace.events if event.shift_code == shift_code]


def shift_view(app: YardApplication, shift_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    events = [event.to_dict() for event in _shift_events(workspace, shift_code)]
    return {
        "shift": shift.to_dict(),
        "events": events[-50:],
        "shift_statistics": live_or_frozen_statistics(workspace, shift_code),
    }


def shift_statistics_view(app: YardApplication, shift_code: str) -> dict[str, Any]:
    """Return live statistics for an open shift or the frozen closure numbers."""
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    frozen = frozen_snapshot_for(workspace, shift_code)
    return {
        "shift": shift.to_dict(),
        "shift_statistics": live_or_frozen_statistics(workspace, shift_code),
        "snapshot_code": None if frozen is None else frozen.get("code"),
    }


def recompute_shift_statistics(app: YardApplication, shift_code: str) -> dict[str, Any]:
    """Rebuild statistics straight from the raw journal lines for one shift."""
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    raw_events = app.repository.journal.read_all()
    statistics = shift_statistics_from_events(raw_events, shift_code)
    return {"shift_code": shift_code, "source": "journal", "shift_statistics": statistics}


__all__ = [
    "recompute_shift_statistics",
    "shift_statistics_view",
    "shift_view",
    "yard_view",
]
