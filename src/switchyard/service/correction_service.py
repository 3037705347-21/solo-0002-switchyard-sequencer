"""Controlled correction commands for archived closure snapshots."""

from __future__ import annotations

from typing import Any

from ..domain.correction import build_correction, derive_idempotency_key
from ..domain.enums import EventKind
from ..domain.errors import ConflictError, NotFoundError
from ..domain.validators import build_correction_payload
from ..report.corrections import corrections_for, snapshot_detail, snapshot_export, snapshot_listing
from .context import YardApplication


def _find_snapshot(workspace: Any, snapshot_code: str) -> dict[str, Any] | None:
    return next(
        (item for item in workspace.closure_snapshots if item.get("code") == snapshot_code),
        None,
    )


def _effective_before(workspace: Any, snapshot: dict[str, Any], chain: list[dict[str, Any]], name: str) -> str:
    """Value of an explanatory field immediately before a new correction."""
    value = str(snapshot.get(name, ""))
    for earlier in chain:
        if name in earlier.get("changes", {}):
            value = str(earlier["changes"][name])
    return value


def correct_snapshot(app: YardApplication, snapshot_code: str, payload: Any) -> dict[str, Any]:
    changes, reason, revised_by, supplied_key = build_correction_payload(payload)
    workspace = app.load()
    snapshot = _find_snapshot(workspace, snapshot_code)
    if snapshot is None:
        raise NotFoundError("closure snapshot", snapshot_code)
    chain = corrections_for(workspace, snapshot_code)
    idempotency_key = supplied_key or derive_idempotency_key(snapshot_code, changes)

    # Stable replay for a repeated submission: an existing record with the same
    # idempotency key and identical body returns the same revision, writes no
    # new event, and leaves prior comparisons against that revision intact.
    for existing in chain:
        if existing.get("idempotency_key") != idempotency_key:
            continue
        same_body = (
            existing.get("changes") == changes
            and existing.get("reason") == reason
            and existing.get("revised_by") == revised_by
        )
        if not same_body:
            raise ConflictError(
                "idempotency key was already used with different content",
                idempotency_key=idempotency_key,
                existing_correction=existing.get("code"),
            )
        if existing is not chain[-1]:
            raise ConflictError(
                "that revision already exists and is superseded by a later correction",
                idempotency_key=idempotency_key,
                existing_correction=existing.get("code"),
                latest_correction=chain[-1].get("code"),
            )
        return {"correction": existing, "replayed": True, "snapshot": snapshot_detail(workspace, snapshot_code)}

    previous_values = {
        name: _effective_before(workspace, snapshot, chain, name) for name in changes
    }
    if all(previous_values[name] == value for name, value in changes.items()):
        raise ConflictError(
            "correction changes nothing: explanatory values already match",
            snapshot_code=snapshot_code,
        )

    sequence = workspace.next_correction_sequence
    revision = len(chain) + 1
    correction = build_correction(
        sequence=sequence,
        revision=revision,
        snapshot_code=snapshot_code,
        shift_code=str(snapshot.get("shift_code", "")),
        changes=changes,
        previous_values=previous_values,
        reason=reason,
        revised_by=revised_by,
        idempotency_key=idempotency_key,
    )
    workspace.snapshot_corrections.append(correction)
    workspace.next_correction_sequence = sequence + 1
    event = workspace.record_event(
        str(snapshot.get("shift_code", "")),
        EventKind.SNAPSHOT_CORRECTED,
        f"snapshot {snapshot_code} corrected by {revised_by}",
        {
            "snapshot_code": snapshot_code,
            "correction_code": correction["code"],
            "revision": revision,
            "changed_fields": correction["changed_fields"],
            "idempotency_key": idempotency_key,
        },
    )
    app.commit(workspace, event)
    return {"correction": correction, "replayed": False, "snapshot": snapshot_detail(workspace, snapshot_code)}


def list_snapshots(app: YardApplication) -> dict[str, Any]:
    return snapshot_listing(app.load())


def get_snapshot(app: YardApplication, snapshot_code: str) -> dict[str, Any]:
    workspace = app.load()
    detail = snapshot_detail(workspace, snapshot_code)
    if not detail:
        raise NotFoundError("closure snapshot", snapshot_code)
    return detail


def export_snapshot(app: YardApplication, snapshot_code: str) -> dict[str, Any]:
    workspace = app.load()
    document = snapshot_export(workspace, snapshot_code)
    if not document:
        raise NotFoundError("closure snapshot", snapshot_code)
    return document


__all__ = ["correct_snapshot", "export_snapshot", "get_snapshot", "list_snapshots"]
