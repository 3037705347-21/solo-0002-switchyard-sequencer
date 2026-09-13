"""Read-only revision views over archived closure snapshots.

These helpers never mutate a snapshot. They take the immutable snapshot
documents plus the append-only correction list and produce list, detail, and
export payloads that keep original values and revised values side by side.
Metrics and blockers always come from the original snapshot document.
"""

from __future__ import annotations

from typing import Any

ORIGINAL_RECORD = "ORIGINAL"
CORRECTION_RECORD = "CORRECTION"


def corrections_for(workspace: Any, snapshot_code: str) -> list[dict[str, Any]]:
    """Return a snapshot's corrections in application order."""
    return [
        dict(item)
        for item in workspace.snapshot_corrections
        if item.get("snapshot_code") == snapshot_code
    ]


def _effective_values(snapshot: dict[str, Any], corrections: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold corrections over a snapshot to obtain the current explanatory values."""
    effective = {"remark": snapshot.get("remark", ""), "responsible": snapshot.get("responsible", "")}
    for correction in corrections:
        for name, value in correction.get("changes", {}).items():
            effective[name] = value
    return effective


def _entry_flags(snapshot: dict[str, Any], corrections: list[dict[str, Any]]) -> dict[str, Any]:
    effective = _effective_values(snapshot, corrections)
    return {
        "correction_count": len(corrections),
        "revised": bool(corrections),
        "latest_revision": corrections[-1]["revision"] if corrections else 0,
        "latest_correction_code": corrections[-1]["code"] if corrections else None,
        "effective": effective,
    }


def snapshot_listing(workspace: Any) -> dict[str, Any]:
    """Flatten snapshots and corrections into one ordered, typed record list."""
    entries: list[dict[str, Any]] = []
    for snapshot in workspace.closure_snapshots:
        code = str(snapshot["code"])
        corrections = corrections_for(workspace, code)
        entry = {
            "record_type": ORIGINAL_RECORD,
            "code": code,
            "shift_code": snapshot.get("shift_code"),
            "version": snapshot.get("version"),
            "metrics": snapshot.get("metrics", {}),
            "remark": snapshot.get("remark", ""),
            "responsible": snapshot.get("responsible", ""),
        }
        entry.update(_entry_flags(snapshot, corrections))
        entries.append(entry)
        for correction in corrections:
            entries.append(
                {
                    "record_type": CORRECTION_RECORD,
                    "code": correction["code"],
                    "snapshot_code": code,
                    "shift_code": correction.get("shift_code", snapshot.get("shift_code")),
                    "revision": correction["revision"],
                    "changed_fields": list(correction.get("changed_fields", [])),
                    "changes": dict(correction.get("changes", {})),
                    "previous_values": dict(correction.get("previous_values", {})),
                    "reason": correction.get("reason", ""),
                    "revised_by": correction.get("revised_by", ""),
                    "created_at": correction.get("created_at", ""),
                }
            )
    return {
        "snapshot_count": len(workspace.closure_snapshots),
        "correction_count": len(workspace.snapshot_corrections),
        "entries": entries,
    }


def snapshot_detail(workspace: Any, snapshot_code: str) -> dict[str, Any]:
    """Return one archived snapshot with its full correction chain."""
    snapshot = next(
        (item for item in workspace.closure_snapshots if item.get("code") == snapshot_code),
        None,
    )
    if snapshot is None:
        return {}
    corrections = corrections_for(workspace, snapshot_code)
    detail = {
        "record_type": ORIGINAL_RECORD,
        "code": snapshot_code,
        "shift_code": snapshot.get("shift_code"),
        "metrics": snapshot.get("metrics", {}),
        "blockers": snapshot.get("blockers", []),
        "version": snapshot.get("version"),
        "original": {
            "remark": snapshot.get("remark", ""),
            "responsible": snapshot.get("responsible", ""),
        },
        "corrections": corrections,
    }
    detail.update(_entry_flags(snapshot, corrections))
    return detail


def snapshot_export(workspace: Any, snapshot_code: str) -> dict[str, Any]:
    """Shape a snapshot for export, separating archived data from revisions."""
    detail = snapshot_detail(workspace, snapshot_code)
    if not detail:
        return {}
    return {
        "code": snapshot_code,
        "shift_code": detail["shift_code"],
        "archived": {
            "metrics": detail["metrics"],
            "blockers": detail["blockers"],
            "snapshot_version": detail["version"],
            "remark": detail["original"]["remark"],
            "responsible": detail["original"]["responsible"],
        },
        "revised": {
            "remark": detail["effective"]["remark"],
            "responsible": detail["effective"]["responsible"],
        },
        "revision_count": detail["correction_count"],
        "latest_revision": detail["latest_revision"],
        "corrections": [
            {
                "code": item["code"],
                "revision": item["revision"],
                "changed_fields": item["changed_fields"],
                "previous_values": item["previous_values"],
                "changes": item["changes"],
                "reason": item["reason"],
                "revised_by": item["revised_by"],
                "created_at": item["created_at"],
            }
            for item in detail["corrections"]
        ],
    }


__all__ = [
    "CORRECTION_RECORD",
    "ORIGINAL_RECORD",
    "corrections_for",
    "snapshot_detail",
    "snapshot_export",
    "snapshot_listing",
]
