"""Shift snapshot archive: read-only listing, detail, and field-level diffs.

Every command here is a pure read: it loads the persisted workspace, selects
from the append-only ``closure_snapshots`` archive, and returns deep copies.
Nothing in this module mutates the live yard and nothing is ever committed.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from ..domain.errors import NotFoundError, ValidationError
from ..domain.timeutil import parse_iso
from ..domain.validators import require_object, require_text
from ..report.diff import snapshot_diff
from .context import YardApplication
ARCHIVE_HEADER_KEYS = (
    "code",
    "shift_code",
    "closed_at",
    "version",
    "schema_version",
    "source_event_range",
    "immutable",
)


def _archive(workspace: Any) -> list[dict[str, Any]]:
    return [dict(doc) for doc in workspace.closure_snapshots]


def _sort_key(doc: dict[str, Any]) -> tuple[int, str, str]:
    closed_at = doc.get("closed_at")
    # Older or damaged documents without closed_at sort last.
    return (1, "", str(doc.get("code", ""))) if not isinstance(closed_at, str) else (0, closed_at, str(doc.get("code", "")))


def _parse_boundary(raw: Any, field_name: str) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{field_name} must be an ISO-8601 timestamp", **{field_name: ["invalid timestamp"]})
    try:
        return parse_iso(raw)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be an ISO-8601 timestamp", **{field_name: [str(exc)]}) from exc


def _header(doc: dict[str, Any]) -> dict[str, Any]:
    header = {key: deepcopy(doc.get(key)) for key in ARCHIVE_HEADER_KEYS}
    metrics = doc.get("metrics") if isinstance(doc.get("metrics"), dict) else None
    car_counts = metrics.get("car_state_counts") if metrics else None
    header["car_state_counts"] = deepcopy(car_counts)
    header["open_outbound_count"] = len(metrics.get("open_outbounds", [])) if metrics and isinstance(metrics.get("open_outbounds"), list) else None
    header["completed_outbound_count"] = (
        len(metrics.get("completed_outbounds", []))
        if metrics and isinstance(metrics.get("completed_outbounds"), list)
        else None
    )
    header["unfinished_run_count"] = (
        len(metrics.get("unfinished_runs", []))
        if metrics and isinstance(metrics.get("unfinished_runs"), list)
        else None
    )
    header["track_count"] = (
        len(metrics.get("track_occupancy", {}))
        if metrics and isinstance(metrics.get("track_occupancy"), dict)
        else None
    )
    header["blocker_count"] = len(doc.get("blockers", [])) if isinstance(doc.get("blockers"), list) else None
    return header


def list_snapshots(app: YardApplication, query: dict[str, Any] | None = None) -> dict[str, Any]:
    query = query or {}
    shift_code = query.get("shift_code")
    if shift_code is not None:
        if not isinstance(shift_code, str) or not shift_code.strip():
            raise ValidationError("shift_code must be a non-empty string", **{"shift_code": ["invalid"]})
        shift_code = shift_code.strip().upper()
    closed_from = _parse_boundary(query.get("closed_from"), "closed_from")
    closed_to = _parse_boundary(query.get("closed_to"), "closed_to")
    if closed_from and closed_to and closed_from > closed_to:
        raise ValidationError(
            "closed_from must not be later than closed_to",
            **{"closed_from": ["range is inverted"]},
        )
    workspace = app.load()
    selected: list[dict[str, Any]] = []
    for doc in sorted(_archive(workspace), key=_sort_key):
        if shift_code is not None and str(doc.get("shift_code", "")).upper() != shift_code:
            continue
        closed_at = doc.get("closed_at")
        # Documents without a parseable closed_at cannot pass a time window.
        if closed_from is not None or closed_to is not None:
            if not isinstance(closed_at, str):
                continue
            try:
                moment = parse_iso(closed_at)
            except ValueError:
                continue
            if closed_from is not None and moment < closed_from:
                continue
            if closed_to is not None and moment > closed_to:
                continue
        selected.append(_header(doc))
    return {
        "count": len(selected),
        "filters": {
            "shift_code": shift_code,
            "closed_from": None if closed_from is None else query.get("closed_from"),
            "closed_to": None if closed_to is None else query.get("closed_to"),
        },
        "snapshots": selected,
    }


def _find_document(workspace: Any, snapshot_code: str) -> dict[str, Any]:
    for doc in workspace.closure_snapshots:
        if str(doc.get("code")) == snapshot_code:
            return dict(doc)
    raise NotFoundError("closure snapshot", snapshot_code)


def get_snapshot(app: YardApplication, snapshot_code: str) -> dict[str, Any]:
    workspace = app.load()
    document = _find_document(workspace, snapshot_code)
    # Deep copy so callers can never mutate the in-memory archive.
    return {"snapshot": deepcopy(document)}


def diff_snapshots(app: YardApplication, payload: Any) -> dict[str, Any]:
    body = require_object(payload, "payload")
    base_code = require_text(body.get("base"), "base", 64).upper()
    target_code = require_text(body.get("target"), "target", 64).upper()
    workspace = app.load()
    base = _find_document(workspace, base_code)
    target = _find_document(workspace, target_code)
    return snapshot_diff(base, target)


__all__ = ["diff_snapshots", "get_snapshot", "list_snapshots"]
