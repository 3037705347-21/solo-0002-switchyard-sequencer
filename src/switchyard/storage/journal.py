"""Append-only JSON event journal next to the workspace file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomicfile import append_text_line, read_json_if_present


@dataclass(slots=True)
class JournalLine:
    """One physical journal line, parsed tolerantly for read-only audits."""

    line_no: int
    raw: str
    data: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None and self.error is None


def read_journal_lines(path: Path) -> list[JournalLine]:
    """Parse every journal record without raising on malformed lines.

    Supports both the standard JSON-lines layout and a legacy single JSON
    array. Unparseable lines are returned as review items instead of aborting
    the read, so a damaged journal never blocks callers. This function is
    strictly read-only.
    """

    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped[0] == "[":
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return [JournalLine(line_no=1, raw=stripped, error=f"invalid JSON array: {exc}")]
        if not isinstance(parsed, list):
            return [JournalLine(line_no=1, raw=stripped, error="journal root is not a JSON array")]
        lines: list[JournalLine] = []
        for index, item in enumerate(parsed, start=1):
            if isinstance(item, dict):
                lines.append(JournalLine(line_no=index, raw=json.dumps(item, ensure_ascii=False), data=dict(item)))
            else:
                lines.append(
                    JournalLine(line_no=index, raw=json.dumps(item, ensure_ascii=False), error="record is not a JSON object")
                )
        return lines
    lines = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        raw = raw_line.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            lines.append(JournalLine(line_no=line_no, raw=raw, error=f"invalid JSON: {exc}"))
            continue
        if not isinstance(item, dict):
            lines.append(JournalLine(line_no=line_no, raw=raw, error="record is not a JSON object"))
            continue
        lines.append(JournalLine(line_no=line_no, raw=raw, data=dict(item)))
    return lines


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


__all__ = ["EventJournal", "JournalLine", "read_journal_lines"]
