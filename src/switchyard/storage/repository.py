"""Load and save the workspace through atomic JSON writes.

Commits are write-ahead: event payloads are appended to the journal first,
all together; only then is the state file atomically replaced. If the state
write fails, the journal lines appended for this commit are removed again, so
state and events either both land or neither does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .atomicfile import atomic_write_json, ensure_parent, read_json_if_present
from .codec import decode_workspace, encode_workspace
from .journal import EventJournal
from .seed import build_seed_workspace
from .workspace import YardWorkspace

STATE_FILE = "yard-state.json"
JOURNAL_FILE = "events.jsonl"


class YardRepository:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / STATE_FILE
        self.journal_path = self.data_dir / JOURNAL_FILE
        ensure_parent(self.state_path)
        self.journal = EventJournal(self.journal_path)

    def exists(self) -> bool:
        return self.state_path.is_file()

    def load(self) -> YardWorkspace:
        raw = read_json_if_present(self.state_path)
        if raw is None:
            workspace = build_seed_workspace()
            self.save(workspace)
            return workspace
        return decode_workspace(dict(raw))

    def save(self, workspace: YardWorkspace) -> None:
        workspace.bump()
        payload = encode_workspace(workspace)
        atomic_write_json(self.state_path, payload)

    def commit(self, workspace: YardWorkspace, events: list[Any]) -> None:
        """Persist a state change and its events atomically (WAL order).

        Events hit the journal first as a single append. If the following
        atomic state replace fails, the journal is truncated back to its
        pre-commit size, leaving neither state nor events behind.
        """
        payloads = [event.to_dict() for event in events]
        journal_size_before = self.journal.path.stat().st_size if self.journal.path.is_file() else 0
        if payloads:
            self.journal.append_many(payloads)
        try:
            self.save(workspace)
        except BaseException:
            # State did not land: undo the journal append so the durable audit
            # trail cannot reference records that the state file lacks.
            self.journal.truncate_to(journal_size_before)
            raise

    def path_text(self) -> str:
        return str(self.state_path)


__all__ = ["JOURNAL_FILE", "STATE_FILE", "YardRepository"]
