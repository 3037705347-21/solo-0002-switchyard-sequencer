"""Read-only shift handover briefing queries.

These commands only load the persisted workspace and project a briefing; they
never commit, never bump the workspace version, and never journal an event.
"""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError, ResourceBusyError
from ..domain.enums import ShiftState
from ..report.handoff import build_handoff_briefing
from .context import YardApplication


def current_handoff_briefing(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    open_codes = sorted(code for code, shift in workspace.shifts.items() if shift.state == ShiftState.OPEN)
    if not open_codes:
        raise ResourceBusyError("no open shift", message_hint="open a shift or request a briefing by shift code")
    return shift_handoff_briefing(app, open_codes[0])


def shift_handoff_briefing(app: YardApplication, shift_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    return build_handoff_briefing(workspace, shift)


__all__ = ["current_handoff_briefing", "shift_handoff_briefing"]
