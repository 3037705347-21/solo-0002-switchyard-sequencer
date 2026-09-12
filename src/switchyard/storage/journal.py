"""Append-only JSON event journal next to the workspace file.

The journal is the write-ahead log for state commits: events for a commit are
appended first (all together) and only then is the workspace state file
replaced. If the state write fails, the just-appended lines are rolled back,
so a caller never sees new trains/cars without their events and vice versa.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .atomicfile import append_text_line, ensure_parent


class JournalWriteError(RuntimeError):
    """Raised when a journal append cannot be completed or rolled back."""


class EventJournal:
    def __init__(self, path: Path):
        self.path = path

    def append(self, payload: dict[str, Any]) -> None:
        append_text_line(self.path, json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def append_many(self, payloads: list[dict[str, Any]]) -> None:
        """Append every payload as one write.

        If the write or flush fails, the journal is truncated back to its size
        before this call so no partial event lines survive.
        """
        ensure_parent(self.path)
        lines = [json.dumps(payload, ensure_ascii=False, sort_keys=True) for payload in payloads]
        content = "".join(
            line + ("" if line.endswith("\n") else "\n") for line in lines
        ).encode("utf-8")
        original_size = self.path.stat().st_size if self.path.is_file() else 0
        handle = None
        try:
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
        except OSError as exc:
            if handle is not None:
                handle.close()
            self._rollback(original_size)
            raise JournalWriteError(f"event journal append failed: {exc}") from exc

    def truncate_to(self, size: int) -> None:
        """Remove journal bytes past ``size`` (used to roll back a commit)."""
        if not self.path.is_file():
            return
        with self.path.open("a+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())

    def _rollback(self, size: int) -> None:
        try:
            self.truncate_to(size)
        except OSError:
            # Best-effort cleanup already failed; surface the original write
            # failure rather than masking it.
            pass

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return []
        lines = text.splitlines()
        if len(lines) == 1:
            # Tolerate the legacy shape where the journal held one JSON array.
            try:
                value = json.loads(lines[0])
            except json.JSONDecodeError:
                value = None
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [json.loads(line) for line in lines if line.strip()]


__all__ = ["EventJournal", "JournalWriteError"]
