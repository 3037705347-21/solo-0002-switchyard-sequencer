"""Closure certificates: tamper-evident proof of a shift handoff.

A certificate captures the shift record, the closure snapshot metrics and
blocker result, and the shift event range at the moment of closure. Every
digest is computed over canonical JSON, so the same persisted history always
produces the same certificate content. Verification is a pure comparison
between a stored certificate and a workspace snapshot; it reports each
inconsistent field or event range entry and never mutates the workspace.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain.enums import ShiftState
from ..domain.errors import NotFoundError, ResourceBusyError
from ..domain.timeutil import now_iso

CERTIFICATE_VERSION = 1
SHIFT_FIELDS = ("code", "dispatcher", "opened_at", "closed_at", "state", "closure_snapshot_code")
CONTENT_FIELDS = ("shift", "snapshot_code", "snapshot_version", "metrics", "blockers", "event_range")
ABSENT = "<absent>"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def event_range_document(events: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [{"sequence": int(event["sequence"]), "digest": stable_digest(event)} for event in events]
    sequences = [entry["sequence"] for entry in entries]
    return {
        "first_sequence": sequences[0] if sequences else 0,
        "last_sequence": sequences[-1] if sequences else 0,
        "count": len(entries),
        "events": entries,
        "event_digest": stable_digest(entries),
    }


def certificate_content(certificate: dict[str, Any]) -> dict[str, Any]:
    return {key: certificate.get(key) for key in CONTENT_FIELDS}


def build_closure_certificate(workspace: Any, shift_code: str, generated_at: str | None = None) -> dict[str, Any]:
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        raise NotFoundError("shift", shift_code)
    if shift.state != ShiftState.CLOSED or not shift.closure_snapshot_code:
        raise ResourceBusyError("shift is not closed yet", shift_code=shift_code)
    snapshot_code = shift.closure_snapshot_code
    snapshot = _find_snapshot(workspace, shift_code, snapshot_code)
    if snapshot is None:
        raise NotFoundError("closure snapshot", snapshot_code)
    shift_record = shift.to_dict()
    events = [event.to_dict() for event in workspace.events if event.shift_code == shift_code]
    certificate: dict[str, Any] = {
        "code": f"CERT-{shift_code}",
        "certificate_version": CERTIFICATE_VERSION,
        "shift_code": shift_code,
        "generated_at": generated_at or now_iso(),
        "shift": {key: shift_record[key] for key in SHIFT_FIELDS},
        "snapshot_code": snapshot_code,
        "snapshot_version": snapshot.get("version"),
        "metrics": snapshot.get("metrics"),
        "blockers": snapshot.get("blockers", []),
        "event_range": event_range_document(events),
    }
    certificate["content_digest"] = stable_digest(certificate_content(certificate))
    return certificate


def verify_certificate_against_workspace(
    certificate: dict[str, Any],
    workspace: Any,
    checked_at: str | None = None,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    shift_code = str(certificate.get("shift_code", ""))

    certified_digest = certificate.get("content_digest")
    recomputed_digest = stable_digest(certificate_content(certificate))
    if certified_digest != recomputed_digest:
        mismatches.append(
            _mismatch(
                "certificate",
                "content_digest",
                certified_digest,
                recomputed_digest,
                "certificate content does not match its own digest; the certificate file may have been altered",
            )
        )

    _verify_shift(certificate, workspace, shift_code, mismatches)
    _verify_snapshot(certificate, workspace, shift_code, mismatches)
    _verify_events(certificate, workspace, shift_code, mismatches)

    return {
        "shift_code": shift_code,
        "certificate_code": certificate.get("code"),
        "verified": not mismatches,
        "checked_at": checked_at or now_iso(),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def _verify_shift(
    certificate: dict[str, Any],
    workspace: Any,
    shift_code: str,
    mismatches: list[dict[str, Any]],
) -> None:
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        mismatches.append(
            _mismatch(
                "shift",
                "shift",
                "present",
                "missing",
                f"shift {shift_code} is missing from the stored state",
            )
        )
        return
    certified_shift = certificate.get("shift") or {}
    actual_record = shift.to_dict()
    actual_subset = {key: actual_record.get(key, ABSENT) for key in SHIFT_FIELDS}
    _diff_values("shift", certified_shift, actual_subset, mismatches, "shift")


def _verify_snapshot(
    certificate: dict[str, Any],
    workspace: Any,
    shift_code: str,
    mismatches: list[dict[str, Any]],
) -> None:
    snapshot = _find_snapshot(workspace, shift_code, certificate.get("snapshot_code"))
    if snapshot is None:
        mismatches.append(
            _mismatch(
                "snapshot",
                "snapshot",
                certificate.get("snapshot_code"),
                "missing",
                "the certified closure snapshot is missing from the stored state",
            )
        )
        return
    if certificate.get("snapshot_version") != snapshot.get("version"):
        mismatches.append(
            _mismatch("snapshot", "snapshot_version", certificate.get("snapshot_version"), snapshot.get("version"))
        )
    _diff_values("metrics", certificate.get("metrics"), snapshot.get("metrics"), mismatches, "metrics")
    _diff_values("blockers", certificate.get("blockers", []), snapshot.get("blockers", []), mismatches, "blockers")


def _verify_events(
    certificate: dict[str, Any],
    workspace: Any,
    shift_code: str,
    mismatches: list[dict[str, Any]],
) -> None:
    actual_events = [event.to_dict() for event in workspace.events if event.shift_code == shift_code]
    actual_range = event_range_document(actual_events)
    certified_range = certificate.get("event_range") or {}
    for key in ("first_sequence", "last_sequence", "count"):
        if certified_range.get(key) != actual_range[key]:
            mismatches.append(_mismatch("events", f"event_range.{key}", certified_range.get(key), actual_range[key]))

    certified_entries = {
        int(entry["sequence"]): entry["digest"] for entry in certified_range.get("events", [])
    }
    actual_order = [entry["sequence"] for entry in actual_range["events"]]
    actual_entries = {entry["sequence"]: entry["digest"] for entry in actual_range["events"]}

    missing = sorted(set(certified_entries) - set(actual_entries))
    if missing:
        mismatches.append(
            _mismatch(
                "events",
                "event_range.missing_sequences",
                missing,
                [],
                f"certified event sequence(s) {_join(missing)} are missing from the stored history",
            )
        )
    unexpected = sorted(set(actual_entries) - set(certified_entries))
    if unexpected:
        mismatches.append(
            _mismatch(
                "events",
                "event_range.unexpected_sequences",
                [],
                unexpected,
                f"stored history contains event sequence(s) {_join(unexpected)} outside the certified range",
            )
        )
    if actual_order != sorted(actual_order):
        mismatches.append(
            _mismatch(
                "events",
                "event_range.order",
                sorted(actual_order),
                actual_order,
                "stored events are not in ascending sequence order",
            )
        )
    for sequence in sorted(set(certified_entries) & set(actual_entries)):
        if certified_entries[sequence] != actual_entries[sequence]:
            mismatches.append(
                _mismatch(
                    "events",
                    f"event_range.events[{sequence}].digest",
                    certified_entries[sequence],
                    actual_entries[sequence],
                    f"stored event {sequence} content differs from the certified digest",
                )
            )
    if certified_range.get("event_digest") != actual_range["event_digest"]:
        mismatches.append(
            _mismatch(
                "events",
                "event_range.event_digest",
                certified_range.get("event_digest"),
                actual_range["event_digest"],
                "event range digest mismatch; stored events were edited, reordered, removed, or extended",
            )
        )


def _diff_values(
    path: str,
    expected: Any,
    actual: Any,
    mismatches: list[dict[str, Any]],
    scope: str,
) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            sub_path = f"{path}.{key}"
            if key not in expected:
                mismatches.append(_mismatch(scope, sub_path, ABSENT, actual[key]))
            elif key not in actual:
                mismatches.append(_mismatch(scope, sub_path, expected[key], ABSENT))
            else:
                _diff_values(sub_path, expected[key], actual[key], mismatches, scope)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            mismatches.append(_mismatch(scope, f"{path}.length", len(expected), len(actual)))
        for index in range(min(len(expected), len(actual))):
            _diff_values(f"{path}[{index}]", expected[index], actual[index], mismatches, scope)
        return
    if expected != actual:
        mismatches.append(_mismatch(scope, path, expected, actual))


def _mismatch(scope: str, field: str, expected: Any, actual: Any, message: str | None = None) -> dict[str, Any]:
    return {
        "scope": scope,
        "field": field,
        "expected": expected,
        "actual": actual,
        "message": message or f"{field}: expected {_preview(expected)}, found {_preview(actual)}",
    }


def _preview(value: Any, limit: int = 120) -> str:
    if isinstance(value, (dict, list)):
        text = canonical_json(value)
    else:
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _join(sequences: list[int]) -> str:
    return ", ".join(str(item) for item in sequences)


def _find_snapshot(workspace: Any, shift_code: str, snapshot_code: Any) -> dict[str, Any] | None:
    for document in workspace.closure_snapshots:
        if document.get("code") == snapshot_code and document.get("shift_code") == shift_code:
            return document
    return None


__all__ = [
    "CERTIFICATE_VERSION",
    "build_closure_certificate",
    "canonical_json",
    "certificate_content",
    "event_range_document",
    "stable_digest",
    "verify_certificate_against_workspace",
]
