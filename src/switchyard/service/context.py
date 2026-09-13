"""Application context that owns the repository boundary."""

from __future__ import annotations

from pathlib import Path

from ..storage.repository import YardRepository
from ..storage.workspace import YardWorkspace


class YardApplication:
    def __init__(self, data_dir: Path | str):
        self.repository = YardRepository(Path(data_dir))

    def load(self) -> YardWorkspace:
        return self.repository.load()

    def commit(self, workspace: YardWorkspace, events: list[object] | object | None = None) -> None:
        self.repository.save(workspace)
        if events is None:
            return
        items = events if isinstance(events, list) else [events]
        for event in items:
            self.repository.journal_event(workspace, event)

    def data_path(self) -> str:
        return self.repository.path_text()

    @property
    def data_dir(self) -> Path:
        return self.repository.data_dir


__all__ = ["YardApplication"]
