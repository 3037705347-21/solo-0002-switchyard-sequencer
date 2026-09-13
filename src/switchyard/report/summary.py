"""Summary shaping used by HTTP responses."""

from __future__ import annotations

from typing import Any

from .closure import closure_blockers
from .metrics import yard_metrics
from .shift_metrics import shift_work_metrics


def build_summary(workspace: Any, shift_code: str) -> dict[str, Any]:
    return {
        "shift_code": shift_code,
        "metrics": yard_metrics(workspace),
        "blockers": closure_blockers(workspace),
    }


def snapshot_document(
    workspace: Any,
    shift_code: str,
    snapshot_code: str,
    closed_at: str | None = None,
) -> dict[str, Any]:
    work = shift_work_metrics(workspace, shift_code)
    return {
        "code": snapshot_code,
        "shift_code": shift_code,
        "metrics": yard_metrics(workspace),
        "work_metrics": work["metrics"],
        "blockers": [],
        "closed_at": closed_at,
        "version": workspace.version,
    }


def event_payload(event: Any) -> dict[str, Any]:
    return event.to_dict()


__all__ = ["build_summary", "event_payload", "snapshot_document"]
