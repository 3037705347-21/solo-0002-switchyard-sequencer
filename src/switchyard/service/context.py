"""Application context that owns the repository boundary."""

from __future__ import annotations

from pathlib import Path

from ..storage.repository import YardRepository
from ..storage.workspace import YardWorkspace


class YardApplication:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.repository = YardRepository(self.data_dir)

    def load(self) -> YardWorkspace:
        return self.repository.load()

    def commit(self, workspace: YardWorkspace, events: list[object] | object | None = None) -> None:
        if events is None:
            # No audit events: just replace the state file atomically.
            self.repository.save(workspace)
            return
        items = events if isinstance(events, list) else [events]
        self.repository.commit(workspace, items)

    def data_path(self) -> str:
        return self.repository.path_text()


__all__ = ["YardApplication"]
