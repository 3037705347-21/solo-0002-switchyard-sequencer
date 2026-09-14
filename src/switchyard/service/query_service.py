"""Read-only query commands."""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError
from ..domain.rules import is_car_code
from ..domain.validators import require_text
from ..report.car_view import build_car_view
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


def car_view_command(app: YardApplication, raw_code: str) -> dict[str, Any]:
    """Reconciled location and ownership answer for one car code.

    Purely read-only: the workspace is loaded but never committed, so the
    query cannot change car state.
    """
    code = require_text(raw_code, "code", 24).upper()
    if not is_car_code(code):
        raise NotFoundError("car", raw_code)
    workspace = app.load()
    view = build_car_view(workspace, code)
    if not view.get("found"):
        raise NotFoundError("car", raw_code)
    return view


__all__ = ["car_view_command", "shift_view", "yard_view"]
