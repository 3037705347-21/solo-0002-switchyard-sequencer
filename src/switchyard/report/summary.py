"""Summary shaping used by HTTP responses."""

from __future__ import annotations

from typing import Any

from .closure import closure_blockers
from .metrics import yard_metrics


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
    remark: str = "",
    responsible: str = "",
) -> dict[str, Any]:
    return {
        "code": snapshot_code,
        "shift_code": shift_code,
        "metrics": yard_metrics(workspace),
        "blockers": [],
        "version": workspace.version,
        "remark": remark,
        "responsible": responsible,
    }


def event_payload(event: Any) -> dict[str, Any]:
    return event.to_dict()


__all__ = ["build_summary", "event_payload", "snapshot_document"]
