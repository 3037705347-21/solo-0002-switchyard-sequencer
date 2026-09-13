"""Arrival capacity forecast and track arrangement commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, ShiftState, TrackPurpose, TrackState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.forecast import build_arrival_forecast
from ..domain.timeutil import is_after_or_equal
from ..domain.validators import build_forecast_payload, build_track_arrangement_payload
from .context import YardApplication


def _open_shift(workspace: Any) -> Any:
    open_shifts = [shift for shift in workspace.shifts.values() if shift.state == ShiftState.OPEN]
    if not open_shifts:
        raise ResourceBusyError("no open shift", message_hint="open a shift before forecasting")
    return open_shifts[0]


def arrival_forecast(app: YardApplication, payload: Any) -> dict[str, Any]:
    prospective, horizon = build_forecast_payload(payload)
    workspace = app.load()
    shift = _open_shift(workspace)

    existing_intakes = set(workspace.intakes)
    for index, train in enumerate(prospective):
        if train.code in existing_intakes:
            raise ConflictError(
                "prospective train code already belongs to a persisted intake",
                code=train.code,
            )
        for item in train.cars:
            if item.code in workspace.cars:
                raise ConflictError("forecast car code already exists in the yard", code=item.code)
    if horizon is not None and not is_after_or_equal(horizon, shift.opened_at):
        raise ValidationError(
            "shift horizon must be at or after shift opening",
            **{"shift_horizon_at": ["must be >= opened_at"]},
        )

    # Pure read: the domain replays classification on a deep copy and returns
    # the report; nothing is committed and no plan/intake state changes.
    return build_arrival_forecast(workspace, prospective, shift.opened_at, horizon)


def arrange_track(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, state_text, purpose_text, note = build_track_arrangement_payload(payload)
    workspace = app.load()
    shift = _open_shift(workspace)
    track = workspace.tracks.get(code)
    if track is None:
        raise NotFoundError("standing track", code)

    changes: dict[str, str] = {}
    new_state = track.state
    if state_text is not None:
        new_state = TrackState.parse(state_text)
    new_purpose = track.purpose
    if purpose_text is not None:
        new_purpose = TrackPurpose.parse(purpose_text)

    if new_purpose != track.purpose and track.purpose == TrackPurpose.DESTINATION:
        raise ValidationError(
            "destination tracks cannot be reassigned",
            **{"purpose": ["only general tracks may move to transfer duty"]},
        )
    if new_purpose != track.purpose and new_purpose == TrackPurpose.TRANSFER and track.stack:
        raise ResourceBusyError(
            "track must be empty before transfer duty",
            track_code=code,
            cars=len(track.stack),
        )
    if new_state == TrackState.MAINTENANCE and track.stack:
        raise ResourceBusyError(
            "track still holds cars and cannot enter maintenance",
            track_code=code,
            cars=len(track.stack),
        )

    # Reserved cars still physically occupy the stack; pulling a track out from
    # under an active outbound plan would strand the run.
    reserved_on_track = []
    for car_code in track.stack:
        car = workspace.cars.get(car_code)
        if car is not None and str(car.state) in {"RESERVED", "ASSEMBLED"}:
            reserved_on_track.append(car_code)
    if reserved_on_track and (
        new_state != TrackState.OPERATIONAL or new_purpose == TrackPurpose.TRANSFER
    ):
        raise ResourceBusyError(
            "track serves an active pull plan",
            track_code=code,
            reserved_cars=reserved_on_track,
        )

    if new_state == track.state and new_purpose == track.purpose:
        return {
            "track": track.to_dict(),
            "changed": False,
            "changes": {},
            "note": note,
        }

    if new_state != track.state:
        changes["state"] = f"{track.state.value} -> {new_state.value}"
    if new_purpose != track.purpose:
        changes["purpose"] = f"{track.purpose.value} -> {new_purpose.value}"
    track.state = new_state
    track.purpose = new_purpose
    if note:
        track.note = note

    event = workspace.record_event(
        shift.code,
        EventKind.TRACK_ARRANGED,
        f"track {code} arrangement updated",
        {"changes": changes, "note": note},
    )
    app.commit(workspace, event)
    return {
        "track": track.to_dict(),
        "changed": True,
        "changes": changes,
        "note": note,
    }


__all__ = ["arrival_forecast", "arrange_track"]
