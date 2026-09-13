"""Read-only car journey profile queries."""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError
from ..report.journey import build_car_journey, build_car_journey_index
from .context import YardApplication


def car_journey_view(app: YardApplication, car_code: str) -> dict[str, Any]:
    workspace = app.load()
    journey = build_car_journey(workspace, car_code)
    if journey is None:
        raise NotFoundError("car", car_code)
    return journey


def car_journey_index(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    return build_car_journey_index(workspace)


__all__ = ["car_journey_index", "car_journey_view"]
