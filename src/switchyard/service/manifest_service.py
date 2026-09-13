"""Departure manifest publication and read commands.

Publishing a manifest snapshots the yard but never mutates cars, tracks,
runs, or the outbound train. It appends one frozen document plus an event.
Every later state change therefore leaves previously published versions
byte-identical and retrievable.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, OutboundState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.manifest import (
    build_manifest_document,
    canonical_payload,
    departure_readiness,
    verify_manifest,
)
from ..domain.timeutil import now_iso
from .context import YardApplication

PUBLISHABLE_STATES = {OutboundState.PLANNED, OutboundState.READY}


def _open_shift(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before publishing a manifest")


def _next_version(workspace: Any, outbound_code: str) -> int:
    versions = [
        int(document["version"])
        for document in workspace.manifests.values()
        if document.get("outbound_code") == outbound_code
    ]
    return (max(versions) + 1) if versions else 1


def publish_manifest(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _open_shift(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state not in PUBLISHABLE_STATES:
        raise ValidationError(
            "outbound plan is not confirmed",
            **{"outbound_code": [f"current state is {outbound.state.value}; confirm the plan first"]},
        )
    version = _next_version(workspace, outbound_code)
    manifest_code = f"MAN-{outbound.code}-V{version}"
    if manifest_code in workspace.manifests:
        raise ConflictError("manifest version already exists", code=manifest_code)
    document = build_manifest_document(
        manifest_code,
        version,
        workspace,
        outbound,
        now_iso(),
    )
    workspace.manifests[manifest_code] = document
    outbound.manifest_codes.append(manifest_code)
    summary = document["discrepancy_summary"]
    event = workspace.record_event(
        shift_code,
        EventKind.MANIFEST_PUBLISHED,
        f"manifest {manifest_code} published for {outbound_code}",
        {
            "manifest_code": manifest_code,
            "version": version,
            "outbound_code": outbound_code,
            "pending_count": summary["pending_count"],
            "conflict_count": summary["conflict_count"],
            "content_digest": document["content_digest"],
        },
    )
    app.commit(workspace, event)
    return {"manifest": document}


def list_manifests(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    documents = [
        workspace.manifests[code]
        for code in outbound.manifest_codes
        if code in workspace.manifests
    ]
    documents.sort(key=lambda item: int(item["version"]))
    versions = [
        {
            "code": item["code"],
            "version": item["version"],
            "published_at": item["published_at"],
            "outbound_state": item["outbound_state"],
            "content_digest": item["content_digest"],
            "discrepancy_summary": item["discrepancy_summary"],
        }
        for item in documents
    ]
    latest = documents[-1] if documents else None
    return {
        "outbound_code": outbound_code,
        "latest_version": latest["version"] if latest else 0,
        "latest_manifest_code": latest["code"] if latest else None,
        "versions": versions,
    }


def _load_manifest(workspace: Any, outbound_code: str, version: int | None) -> dict[str, Any]:
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if version is None:
        if not outbound.manifest_codes:
            raise NotFoundError("manifest for outbound train", outbound_code)
        code = outbound.manifest_codes[-1]
    else:
        code = f"MAN-{outbound_code}-V{version}"
    document = workspace.manifests.get(code)
    if document is None:
        raise NotFoundError("manifest", code)
    return document


def get_manifest(app: YardApplication, outbound_code: str, version: int | None = None) -> dict[str, Any]:
    workspace = app.load()
    document = _load_manifest(workspace, outbound_code, version)
    return {"manifest": document}


def export_manifest(app: YardApplication, outbound_code: str, version: int | None = None) -> dict[str, Any]:
    """Return the frozen export payload plus a live yard verification."""

    workspace = app.load()
    document = _load_manifest(workspace, outbound_code, version)
    verification = verify_manifest(document, workspace)
    readiness = departure_readiness(
        workspace,
        workspace.outbounds[outbound_code],
    )
    return {
        "export": {
            "format": "switchyard-departure-manifest",
            "canonical_content": canonical_payload(document),
            "document": document,
        },
        "verification": verification,
        "departure_readiness": readiness,
    }


def manifest_readiness(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    return departure_readiness(workspace, outbound)


def verify_manifest_version(
    app: YardApplication,
    outbound_code: str,
    version: int | None = None,
) -> dict[str, Any]:
    workspace = app.load()
    document = _load_manifest(workspace, outbound_code, version)
    return {"verification": verify_manifest(document, workspace)}


__all__ = [
    "export_manifest",
    "get_manifest",
    "list_manifests",
    "manifest_readiness",
    "publish_manifest",
    "verify_manifest_version",
]
