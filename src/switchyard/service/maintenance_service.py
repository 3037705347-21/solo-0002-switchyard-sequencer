"""Maintenance window scheduling and lifecycle commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, TrackState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError
from ..domain.maintenance import (
    MaintenanceWindow,
    cancel_window,
    confirm_window,
    freeze_window,
    restore_window,
)
from ..domain.validators import build_maintenance_payload
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before maintenance work")


def _get_window(workspace: Any, code: str) -> MaintenanceWindow:
    window = workspace.maintenance_windows.get(code)
    if window is None:
        raise NotFoundError("maintenance window", code)
    return window


def schedule_window(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, track_code, planned_start, planned_end, reason, owner = build_maintenance_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    if code in workspace.maintenance_windows:
        raise ConflictError("maintenance window already exists", code=code)
    track = workspace.tracks.get(track_code)
    if track is None:
        raise NotFoundError("standing track", track_code)
    if track.state == TrackState.MAINTENANCE:
        raise ResourceBusyError(f"track {track_code} is already in maintenance", track_code=track_code)
    for existing in workspace.maintenance_windows.values():
        if existing.track_code == track_code and existing.is_open():
            raise ConflictError(
                f"track {track_code} already has an open maintenance window",
                code=existing.code,
            )
    window = MaintenanceWindow(
        code=code,
        track_code=track_code,
        planned_start=planned_start,
        planned_end=planned_end,
        reason=reason,
        owner=owner,
    )
    workspace.maintenance_windows[code] = window
    event = workspace.record_event(
        shift_code,
        EventKind.MAINTENANCE_SCHEDULED,
        f"maintenance window {code} scheduled for {track_code}",
        {
            "track_code": track_code,
            "planned_start": planned_start,
            "planned_end": planned_end,
            "reason": reason,
            "owner": owner,
        },
    )
    app.commit(workspace, event)
    return {"window": window.to_dict()}


def freeze_window_command(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    window = _get_window(workspace, code)
    freeze_window(workspace, window)
    affected = [plan.to_dict() for plan in window.affected_plans]
    event = workspace.record_event(
        shift_code,
        EventKind.MAINTENANCE_FROZEN,
        f"maintenance window {code} froze intake allocation to {window.track_code}",
        {"track_code": window.track_code, "affected_plans": affected},
    )
    app.commit(workspace, event)
    return {"window": window.to_dict(), "affected_plans": affected}


def confirm_window_command(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    window = _get_window(workspace, code)
    confirm_window(workspace, window)
    event = workspace.record_event(
        shift_code,
        EventKind.MAINTENANCE_CONFIRMED,
        f"maintenance window {code} confirmed; {window.track_code} entered maintenance",
        {"track_code": window.track_code, "confirmed_at": window.confirmed_at},
    )
    app.commit(workspace, event)
    return {"window": window.to_dict()}


def restore_window_command(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    window = _get_window(workspace, code)
    restore_window(workspace, window)
    event = workspace.record_event(
        shift_code,
        EventKind.MAINTENANCE_RESTORED,
        f"maintenance window {code} restored; {window.track_code} is operational again",
        {"track_code": window.track_code, "restored_at": window.restored_at},
    )
    app.commit(workspace, event)
    return {"window": window.to_dict()}


def cancel_window_command(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    window = _get_window(workspace, code)
    cancel_window(workspace, window)
    track_state = str(workspace.tracks[window.track_code].state)
    event = workspace.record_event(
        shift_code,
        EventKind.MAINTENANCE_CANCELLED,
        f"maintenance window {code} cancelled; {window.track_code} is {track_state}",
        {"track_code": window.track_code, "track_state": track_state},
    )
    app.commit(workspace, event)
    return {"window": window.to_dict()}


def list_windows(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    windows = sorted(workspace.maintenance_windows.values(), key=lambda item: (item.created_at, item.code))
    return {"windows": [window.to_dict() for window in windows]}


def get_window(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    return {"window": _get_window(workspace, code).to_dict()}


__all__ = [
    "cancel_window_command",
    "confirm_window_command",
    "freeze_window_command",
    "get_window",
    "list_windows",
    "restore_window_command",
    "schedule_window",
]
