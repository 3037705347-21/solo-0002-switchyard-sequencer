"""Maintenance window entity and lifecycle operations for standing tracks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import CarState, IntakeState, OutboundState, RunState, TrackState, WindowState
from .errors import NotFoundError, ResourceBusyError, StateTransitionError
from .rules import destination_allowed, hazard_allowed, kind_allowed
from .timeutil import now_iso
from .transitions import transition_window

OPEN_WINDOW_STATES = {WindowState.SCHEDULED, WindowState.FROZEN, WindowState.ACTIVE}
ACTIVE_RUN_STATES = {RunState.QUEUED, RunState.RUNNING}
OPEN_OUTBOUND_STATES = {OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.READY}


@dataclass(slots=True)
class AffectedPlan:
    """One unfinished plan touched by a maintenance freeze."""

    kind: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> "AffectedPlan":
        return cls(
            kind=str(raw["kind"]),
            code=str(raw["code"]),
            message=str(raw["message"]),
        )


@dataclass(slots=True)
class MaintenanceWindow:
    code: str
    track_code: str
    planned_start: str
    planned_end: str
    reason: str
    owner: str
    state: WindowState = WindowState.SCHEDULED
    prior_track_state: str | None = None
    affected_plans: list[AffectedPlan] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    frozen_at: str | None = None
    confirmed_at: str | None = None
    restored_at: str | None = None
    cancelled_at: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "track_code": self.track_code,
            "planned_start": self.planned_start,
            "planned_end": self.planned_end,
            "reason": self.reason,
            "owner": self.owner,
            "state": str(self.state),
            "prior_track_state": self.prior_track_state,
            "affected_plans": [plan.to_dict() for plan in self.affected_plans],
            "created_at": self.created_at,
            "frozen_at": self.frozen_at,
            "confirmed_at": self.confirmed_at,
            "restored_at": self.restored_at,
            "cancelled_at": self.cancelled_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "MaintenanceWindow":
        return cls(
            code=str(raw["code"]),
            track_code=str(raw["track_code"]),
            planned_start=str(raw["planned_start"]),
            planned_end=str(raw["planned_end"]),
            reason=str(raw.get("reason", "")),
            owner=str(raw.get("owner", "")),
            state=WindowState.parse(str(raw.get("state", WindowState.SCHEDULED.value))),
            prior_track_state=None if raw.get("prior_track_state") is None else str(raw["prior_track_state"]),
            affected_plans=[AffectedPlan.from_dict(dict(item)) for item in raw.get("affected_plans", [])],
            created_at=str(raw.get("created_at", "")),
            frozen_at=None if raw.get("frozen_at") is None else str(raw["frozen_at"]),
            confirmed_at=None if raw.get("confirmed_at") is None else str(raw["confirmed_at"]),
            restored_at=None if raw.get("restored_at") is None else str(raw["restored_at"]),
            cancelled_at=None if raw.get("cancelled_at") is None else str(raw["cancelled_at"]),
            note=str(raw.get("note", "")),
        )

    def is_open(self) -> bool:
        return self.state in OPEN_WINDOW_STATES


def _run_touches_track(run: Any, track_code: str) -> bool:
    for step in run.steps[run.current_step :]:
        if step.source_code == track_code or step.target_code == track_code:
            return True
    return False


def _active_runs_touching(workspace: Any, track_code: str) -> list[str]:
    codes: list[str] = []
    for code in sorted(workspace.runs):
        run = workspace.runs[code]
        if run.state in ACTIVE_RUN_STATES and _run_touches_track(run, track_code):
            codes.append(code)
    return codes


def _car_matches_track(track: Any, car: Any) -> bool:
    """True when the track is a compatible allocation target for the car."""
    return (
        destination_allowed(track, car)
        and kind_allowed(track, car)
        and hazard_allowed(track, car)
    )


def collect_affected_plans(workspace: Any, track_code: str) -> list[AffectedPlan]:
    """List every unfinished classification or pull plan touching a track."""
    track = workspace.tracks.get(track_code)
    if track is None:
        return []
    plans: list[AffectedPlan] = []
    for code in sorted(workspace.intakes):
        intake = workspace.intakes[code]
        if intake.state not in {IntakeState.OPEN, IntakeState.PARTIAL}:
            continue
        related = [
            car_code
            for car_code in intake.consist
            if workspace.cars.get(car_code) is not None
            and workspace.cars[car_code].state == CarState.RECEIVED
            and _car_matches_track(track, workspace.cars[car_code])
        ]
        if not related:
            continue
        plans.append(
            AffectedPlan(
                "intake",
                code,
                f"intake {code} is {intake.state.value} with {len(related)} unplaced car(s) compatible with {track_code}",
            )
        )
    for code in sorted(workspace.outbounds):
        outbound = workspace.outbounds[code]
        if outbound.state not in OPEN_OUTBOUND_STATES:
            continue
        on_track = [car_code for car_code in outbound.planned_car_codes if car_code in track.stack]
        if on_track:
            plans.append(
                AffectedPlan(
                    "outbound",
                    code,
                    f"outbound {code} is {outbound.state.value} with {len(on_track)} planned car(s) still on {track_code}",
                )
            )
    for code in _active_runs_touching(workspace, track_code):
        run = workspace.runs[code]
        plans.append(
            AffectedPlan(
                "pull_run",
                code,
                f"pull run {code} is {run.state.value} with remaining moves involving {track_code}",
            )
        )
    for car_code in track.stack:
        plans.append(AffectedPlan("standing_car", car_code, f"car {car_code} still stands on {track_code}"))
    return plans


def freeze_window(workspace: Any, window: MaintenanceWindow) -> None:
    """Freeze new intake allocation to the window track ahead of the window."""
    track = workspace.tracks.get(window.track_code)
    if track is None:
        raise NotFoundError("standing track", window.track_code)
    if track.state == TrackState.MAINTENANCE:
        raise ResourceBusyError(f"track {track.code} is already in maintenance", track_code=track.code)
    transition_window(window, WindowState.FROZEN)
    window.prior_track_state = str(track.state)
    track.state = TrackState.RESTRICTED
    window.affected_plans = collect_affected_plans(workspace, window.track_code)
    window.frozen_at = now_iso()


def confirm_window(workspace: Any, window: MaintenanceWindow) -> None:
    """Switch a cleared, frozen track into maintenance."""
    track = workspace.tracks.get(window.track_code)
    if track is None:
        raise NotFoundError("standing track", window.track_code)
    if window.state != WindowState.FROZEN:
        raise StateTransitionError(
            "maintenance window",
            str(window.state),
            WindowState.ACTIVE.value,
            "window must be frozen before it can be confirmed",
        )
    if track.stack:
        raise ResourceBusyError(
            f"track {track.code} still holds {len(track.stack)} car(s)",
            track_code=track.code,
            cars=list(track.stack),
        )
    blocking = _active_runs_touching(workspace, window.track_code)
    if blocking:
        raise ResourceBusyError(
            f"active pull run(s) still reference {track.code}",
            track_code=track.code,
            runs=blocking,
        )
    transition_window(window, WindowState.ACTIVE)
    track.state = TrackState.MAINTENANCE
    window.confirmed_at = now_iso()


def restore_window(workspace: Any, window: MaintenanceWindow) -> None:
    """Reopen a maintained track and return it to an operable state."""
    track = workspace.tracks.get(window.track_code)
    if track is None:
        raise NotFoundError("standing track", window.track_code)
    if window.state != WindowState.ACTIVE:
        raise StateTransitionError(
            "maintenance window",
            str(window.state),
            WindowState.RESTORED.value,
            "only an active window can be restored",
        )
    transition_window(window, WindowState.RESTORED)
    track.state = TrackState.OPERATIONAL
    window.restored_at = now_iso()


def cancel_window(workspace: Any, window: MaintenanceWindow) -> None:
    """Cancel a scheduled or frozen window and undo its freeze."""
    track = workspace.tracks.get(window.track_code)
    if track is None:
        raise NotFoundError("standing track", window.track_code)
    if window.state not in {WindowState.SCHEDULED, WindowState.FROZEN}:
        raise StateTransitionError(
            "maintenance window",
            str(window.state),
            WindowState.CANCELLED.value,
            "only a scheduled or frozen window can be cancelled",
        )
    if window.state == WindowState.FROZEN:
        track.state = TrackState.parse(window.prior_track_state or TrackState.OPERATIONAL.value)
    transition_window(window, WindowState.CANCELLED)
    window.cancelled_at = now_iso()


__all__ = [
    "AffectedPlan",
    "MaintenanceWindow",
    "cancel_window",
    "collect_affected_plans",
    "confirm_window",
    "freeze_window",
    "restore_window",
]
