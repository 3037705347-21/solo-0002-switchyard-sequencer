"""Stable identifiers and canonical hashing for commit records."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Deterministic JSON text shared by state and journal hashing."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_content_hash(event_dict: dict[str, Any]) -> str:
    """Hash the business content of one event, excluding envelope fields."""
    content = {
        "sequence": int(event_dict["sequence"]),
        "at": event_dict.get("at"),
        "shift_code": event_dict.get("shift_code"),
        "kind": event_dict.get("kind"),
        "message": event_dict.get("message"),
        "payload": event_dict.get("payload", {}),
    }
    digest = hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def new_commit_id(sequence: int) -> str:
    """Commit ids are human-traceable and unique within one data directory."""
    randomness = hashlib.sha256(canonical_json({"seq": sequence, "r": _clock_seed()}).encode("utf-8")).hexdigest()[:16]
    return f"C-{sequence:08d}-{randomness}"


def _clock_seed() -> str:
    import os
    import time

    return f"{time.time_ns()}-{os.getpid()}-{id(object())}"


__all__ = ["canonical_json", "event_content_hash", "new_commit_id"]
