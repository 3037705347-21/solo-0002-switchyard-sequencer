"""Summary shaping used by HTTP responses."""

from __future__ import annotations

from typing import Any

from .closure import closure_blockers
from .metrics import yard_metrics
from .shift_stats import shift_statistics


def build_summary(workspace: Any, shift_code: str) -> dict[str, Any]:
    return {
        "shift_code": shift_code,
        "metrics": yard_metrics(workspace),
        "blockers": closure_blockers(workspace),
    }


def snapshot_document(workspace: Any, shift_code: str, snapshot_code: str) -> dict[str, Any]:
    return {
        "code": snapshot_code,
        "shift_code": shift_code,
        "metrics": yard_metrics(workspace),
        "shift_statistics": shift_statistics(workspace, shift_code),
        "blockers": [],
        "version": workspace.version,
    }


def frozen_snapshot_for(workspace: Any, shift_code: str) -> dict[str, Any] | None:
    """Return the closure snapshot frozen for a shift, if one exists."""
    for snapshot in workspace.closure_snapshots:
        if snapshot.get("shift_code") == shift_code:
            return dict(snapshot)
    return None


def live_or_frozen_statistics(workspace: Any, shift_code: str) -> dict[str, Any]:
    """Closed shifts serve frozen statistics; open shifts are recomputed live."""
    snapshot = frozen_snapshot_for(workspace, shift_code)
    if snapshot is not None and isinstance(snapshot.get("shift_statistics"), dict):
        stats = dict(snapshot["shift_statistics"])
        stats["frozen"] = True
        return stats
    stats = shift_statistics(workspace, shift_code)
    stats["frozen"] = False
    return stats


def event_payload(event: Any) -> dict[str, Any]:
    return event.to_dict()


__all__ = [
    "build_summary",
    "event_payload",
    "frozen_snapshot_for",
    "live_or_frozen_statistics",
    "snapshot_document",
]
