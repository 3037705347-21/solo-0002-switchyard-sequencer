"""Controlled correction records for archived closure snapshots.

A correction never edits the original snapshot document. It is a separate,
append-only record that references the snapshot by code and carries revised
values for explanatory fields only. Metrics, blockers, and the historical
event trail are intentionally outside :data:`CORRECTABLE_FIELDS` and any
attempt to touch them is rejected at the service boundary.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .timeutil import now_iso

# Snapshot fields that may be revised after closure. Everything else in a
# snapshot document is archived history and stays immutable.
CORRECTABLE_FIELDS = ("remark", "responsible")

CORRECTION_CODE_PREFIX = "CORR-"
IDEMPOTENCY_KEY_LENGTH = 40
MAX_IDEMPOTENCY_KEY_LENGTH = 80


def derive_idempotency_key(snapshot_code: str, changes: dict[str, str]) -> str:
    """Build a stable key for a correction that did not supply one.

    The key is a hash of the snapshot code and the exact corrected values,
    so resubmitting the identical revision collapses onto the same record
    while a different revision derives a different key.
    """
    material = snapshot_code + "|"
    for name in CORRECTABLE_FIELDS:
        if name in changes:
            material += f"{name}={changes[name]};"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return digest[:IDEMPOTENCY_KEY_LENGTH]


def build_correction(
    sequence: int,
    revision: int,
    snapshot_code: str,
    shift_code: str,
    changes: dict[str, str],
    previous_values: dict[str, str],
    reason: str,
    revised_by: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a correction document.

    ``sequence`` is the global correction number used for the stable record
    code; ``revision`` is this correction's position in its snapshot's own
    revision chain. ``previous_values`` captures the effective values being
    replaced so consumers can diff the revision without recomputing history.
    """
    changed_fields = sorted(changes)
    return {
        "code": f"{CORRECTION_CODE_PREFIX}{sequence:03d}-{snapshot_code}",
        "sequence": sequence,
        "revision": revision,
        "snapshot_code": snapshot_code,
        "shift_code": shift_code,
        "changed_fields": changed_fields,
        "changes": {name: changes[name] for name in changed_fields},
        "previous_values": {name: previous_values[name] for name in changed_fields},
        "reason": reason,
        "revised_by": revised_by,
        "idempotency_key": idempotency_key,
        "created_at": now_iso(),
    }


__all__ = [
    "CORRECTABLE_FIELDS",
    "CORRECTION_CODE_PREFIX",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "build_correction",
    "derive_idempotency_key",
]
