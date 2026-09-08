"""Load and save the workspace through atomic JSON writes."""

from __future__ import annotations

from pathlib import Path

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

    def journal_event(self, workspace: YardWorkspace, event: object) -> None:
        self.journal.append(event.to_dict())

    def path_text(self) -> str:
        return str(self.state_path)


__all__ = ["JOURNAL_FILE", "STATE_FILE", "YardRepository"]
