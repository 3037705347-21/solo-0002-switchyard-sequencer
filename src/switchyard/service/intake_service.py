"""Inbound train intake and classification commands."""

from __future__ import annotations

from typing import Any

from ..domain import operations
from ..domain.enums import EventKind
from ..domain.errors import NotFoundError
from ..domain.validators import build_intake_payload
from .context import YardApplication, require_open_shift


def create_intake(app: YardApplication, payload: Any) -> dict[str, Any]:
    train, car_inputs = build_intake_payload(payload)
    workspace = app.load()
    shift_code = require_open_shift(workspace, "open a shift before yard work")
    cars = operations.receive_intake(workspace, train, car_inputs)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_RECEIVED,
        f"intake {train.code} received {len(cars)} cars",
        {"route": train.route, "car_count": len(cars), "arrival_at": train.arrival_at},
    )
    app.commit(workspace, event)
    return {
        "intake": train.to_dict(),
        "cars": [car.to_dict() for car in cars],
    }


def classify_intake_command(app: YardApplication, intake_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = require_open_shift(workspace, "open a shift before yard work")
    train = workspace.intakes.get(intake_code)
    if train is None:
        raise NotFoundError("intake train", intake_code)
    spots = operations.classify_consist(workspace, train)
    if train.unplaced:
        message = f"intake {train.code} partially classified with {len(train.unplaced)} unplaced cars"
    else:
        message = f"intake {train.code} fully classified"
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_CLASSIFIED,
        message,
        {
            "spotted": len(spots),
            "unplaced": list(train.unplaced),
            "spots": [item.to_dict() for item in spots],
        },
    )
    app.commit(workspace, event)
    return {
        "intake": train.to_dict(),
        "spots": [item.to_dict() for item in spots],
        "unplaced": list(train.unplaced),
    }


__all__ = ["classify_intake_command", "create_intake"]
