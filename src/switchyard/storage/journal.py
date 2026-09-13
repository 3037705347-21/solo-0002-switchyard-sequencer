"""Append-only JSON event journal next to the workspace file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomicfile import append_text_line


class EventJournal:
    def __init__(self, path: Path):
        self.path = path

    def append(self, payload: dict[str, Any]) -> None:
        append_text_line(self.path, json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(value, list):
            return [dict(item) for item in value]
        return [dict(value)]


__all__ = ["EventJournal"]
