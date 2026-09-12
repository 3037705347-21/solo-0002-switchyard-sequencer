"""Outbound train creation and pull planning commands."""

from __future__ import annotations

from typing import Any

from ..domain import operations
from ..domain.enums import EventKind
from ..domain.errors import NotFoundError
from ..domain.validators import build_outbound_payload, parse_transfer_code
from .context import YardApplication, require_open_shift


def create_outbound(app: YardApplication, payload: Any) -> dict[str, Any]:
    code, destination, car_codes = build_outbound_payload(payload)
    workspace = app.load()
    shift_code = require_open_shift(workspace, "open a shift before planning")
    train = operations.draft_outbound(workspace, code, destination, car_codes)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_CREATED,
        f"outbound {code} drafted for {destination}",
        {"destination": destination, "planned_count": len(car_codes)},
    )
    app.commit(workspace, event)
    return train.to_dict()


def sequence_outbound(app: YardApplication, outbound_code: str, payload: Any) -> dict[str, Any]:
    transfer_code = parse_transfer_code(payload)
    workspace = app.load()
    shift_code = require_open_shift(workspace, "open a shift before planning")
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    run = operations.plan_outbound(workspace, outbound, transfer_code)
    event = workspace.record_event(
        shift_code,
        EventKind.PULL_PLANNED,
        f"pull run {run.code} planned for {outbound.code}",
        {"steps": len(run.steps), "transfer_code": transfer_code},
    )
    app.commit(workspace, event)
    return {"pull_run": run.to_dict(), "outbound": outbound.to_dict()}


__all__ = ["create_outbound", "sequence_outbound"]
