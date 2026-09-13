"""Append-only JSON event journal next to the workspace file.

Journal lines are one JSON object per line. Modern files use two framed
record types:

* ``{"journal": "switchyard-events", "record": "event", "event": {...},
   "content_hash": "sha256:...", "commit_id": "..."}``
* ``{"journal": "switchyard-events", "record": "commit", "commit_id": "...",
   "commit_number": N, "state_version": V, "event_sequences": [...],
   "event_hashes": [...]}``

A commit only becomes *detectably complete* once its ``commit`` marker is
present. Event lines of a commit are written first as a single batch, then
the marker, so an interrupted commit is recognizable: events without the
matching marker are a roll-forward candidate (the state already contains
them), and a marker without matching events is corruption.

Legacy journals (bare event objects, or a single JSON array document) keep
loading; recovery migrates them in place without user intervention.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomicfile import append_text_lines, atomic_write_text, read_text_if_present

JOURNAL_FORMAT = "switchyard-events"
JOURNAL_VERSION = 2
RECORD_EVENT = "event"
RECORD_COMMIT = "commit"


class JournalParseError(ValueError):
    """Raised when a journal line cannot be parsed at all."""


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def event_line(event_dict: dict[str, Any], content_hash: str, commit_id: str) -> str:
    return _dump(
        {
            "journal": JOURNAL_FORMAT,
            "record": RECORD_EVENT,
            "event": event_dict,
            "content_hash": content_hash,
            "commit_id": commit_id,
        }
    )


def commit_marker(
    commit_id: str,
    commit_number: int,
    state_version: int,
    event_sequences: list[int],
    event_hashes: list[str],
) -> str:
    return _dump(
        {
            "journal": JOURNAL_FORMAT,
            "record": RECORD_COMMIT,
            "commit_id": commit_id,
            "commit_number": commit_number,
            "state_version": state_version,
            "event_sequences": list(event_sequences),
            "event_hashes": list(event_hashes),
        }
    )


def canonical_commit_lines(
    entries: list[dict[str, Any]], events_by_sequence: dict[int, dict[str, Any]], hasher: Any
) -> list[str]:
    """Render the canonical journal text implied by the state ledger."""
    lines: list[str] = []
    for entry in entries:
        commit_id = str(entry["commit_id"])
        sequences = [int(value) for value in entry.get("event_sequences", [])]
        for sequence in sequences:
            event_dict = events_by_sequence[sequence]
            lines.append(event_line(event_dict, hasher(event_dict), commit_id))
        lines.append(
            commit_marker(
                commit_id=commit_id,
                commit_number=int(entry["commit_number"]),
                state_version=int(entry["state_version"]),
                event_sequences=sequences,
                event_hashes=[hasher(events_by_sequence[sequence]) for sequence in sequences],
            )
        )
    return lines


class EventJournal:
    def __init__(self, path: Path):
        self.path = path

    def append_commit(
        self,
        event_items: list[tuple[dict[str, Any], str]],
        commit_id: str,
        commit_number: int,
        state_version: int,
    ) -> None:
        lines = [event_line(event_dict, content_hash, commit_id) for event_dict, content_hash in event_items]
        lines.append(
            commit_marker(
                commit_id=commit_id,
                commit_number=commit_number,
                state_version=state_version,
                event_sequences=[int(item[0]["sequence"]) for item in event_items],
                event_hashes=[item[1] for item in event_items],
            )
        )
        append_text_lines(self.path, lines)

    def append(self, payload: dict[str, Any]) -> None:
        """Backward-compatible single-event append (no commit frame)."""
        append_text_lines(self.path, [json.dumps(payload, ensure_ascii=False, sort_keys=True)])

    def rewrite_canonical(self, lines: list[str]) -> None:
        atomic_write_text(self.path, "".join(line + "\n" for line in lines))

    def read_records(self) -> list[dict[str, Any]]:
        """Read every raw line as a dict; malformed lines raise."""
        text = read_text_if_present(self.path)
        if text is None:
            return []
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise JournalParseError(f"malformed journal line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise JournalParseError(f"journal line {line_number} is not a JSON object")
            records.append(value)
        return records

    def read_all(self) -> list[dict[str, Any]]:
        """Return business events, tolerating legacy file shapes."""
        text = read_text_if_present(self.path)
        if text is None:
            return []
        stripped = text.lstrip()
        if stripped.startswith("["):
            value = json.loads(text)
            if not isinstance(value, list):
                raise JournalParseError("legacy JSON journal is not an array")
            return [dict(item) for item in value]
        events: list[dict[str, Any]] = []
        for record in self.read_records():
            if record.get("journal") == JOURNAL_FORMAT:
                if record.get("record") == RECORD_EVENT:
                    events.append(dict(record["event"]))
            else:
                events.append(record)
        return events


__all__ = [
    "JOURNAL_FORMAT",
    "JOURNAL_VERSION",
    "JournalParseError",
    "RECORD_COMMIT",
    "RECORD_EVENT",
    "EventJournal",
    "canonical_commit_lines",
    "commit_marker",
    "event_line",
]
