"""Hazardous goods compliance review query command."""

from __future__ import annotations

from typing import Any

from ..report.hazard_review import hazard_compliance_review
from .context import YardApplication


def hazard_review(app: YardApplication) -> dict[str, Any]:
    workspace = app.load()
    return hazard_compliance_review(workspace)


__all__ = ["hazard_review"]
