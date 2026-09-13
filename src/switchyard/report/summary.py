"""Summary shaping used by HTTP responses."""

from __future__ import annotations

from typing import Any

from ..storage.workspace import SCHEMA_VERSION
from .closure import closure_blockers
from .metrics import yard_metrics


def build_summary(workspace: Any, shift_code: str) -> dict[str, Any]:
    return {
        "shift_code": shift_code,
        "metrics": yard_metrics(workspace),
        "blockers": closure_blockers(workspace),
    }


def shift_event_range(workspace: Any, shift_code: str) -> dict[str, Any]:
    """First/last event sequences contributed by the shift being closed.

    The SHIFT_CLOSED event is recorded after document creation, so callers pass
    a workspace whose final event has already been appended.
    """
    sequences = [
        event.sequence
        for event in workspace.events
        if event.shift_code == shift_code
    ]
    if not sequences:
        return {"first_sequence": None, "last_sequence": None, "event_count": 0}
    return {
        "first_sequence": min(sequences),
        "last_sequence": max(sequences),
        "event_count": len(sequences),
    }


def snapshot_document(
    workspace: Any,
    shift_code: str,
    snapshot_code: str,
    closed_at: str,
) -> dict[str, Any]:
    event_range = shift_event_range(workspace, shift_code)
    return {
        "code": snapshot_code,
        "shift_code": shift_code,
        "closed_at": closed_at,
        "metrics": yard_metrics(workspace),
        "blockers": [],
        "version": workspace.version,
        "schema_version": SCHEMA_VERSION,
        "source_event_range": event_range,
        "source_event_count_after": len(workspace.events),
        "immutable": True,
    }


def event_payload(event: Any) -> dict[str, Any]:
    return event.to_dict()


__all__ = ["build_summary", "event_payload", "shift_event_range", "snapshot_document"]
