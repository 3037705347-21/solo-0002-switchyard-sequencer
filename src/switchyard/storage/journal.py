"""Append-only JSON event journal next to the workspace file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomicfile import append_text_line, read_json_if_present


class EventJournal:
    def __init__(self, path: Path):
        self.path = path

    def append(self, payload: dict[str, Any]) -> None:
        append_text_line(self.path, json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def read_all(self) -> list[dict[str, Any]]:
        value = read_json_if_present(self.path)
        if not isinstance(value, list):
            if not self.path.is_file():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines if line.strip()]
        return value


__all__ = ["EventJournal"]
