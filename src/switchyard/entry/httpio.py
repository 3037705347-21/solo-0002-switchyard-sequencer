"""HTTP request body parsing helpers."""

from __future__ import annotations

import json
from typing import Any

from ..domain.errors import ValidationError


def parse_body(raw: bytes | None, content_type: str | None = None) -> Any:
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("request body is not valid UTF-8", **{"body": ["invalid encoding"]}) from exc
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("request body is not valid JSON", **{"body": [str(exc)]}) from exc
    return value


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


__all__ = ["json_bytes", "parse_body"]
