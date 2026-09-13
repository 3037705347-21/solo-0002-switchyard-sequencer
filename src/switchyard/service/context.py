"""Application context that owns the repository boundary."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..storage.repository import YardRepository
from ..storage.workspace import YardWorkspace


class YardApplication:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.repository = YardRepository(self.data_dir)
        # Every read-modify-write command runs under this lock so that events
        # appended by concurrent worker threads cannot overwrite each other.
        self._write_lock = threading.RLock()

    def load(self) -> YardWorkspace:
        return self.repository.load()

    @contextmanager
    def update(self, *, persist_on_error: bool = False) -> Iterator[YardWorkspace]:
        """Load a workspace, mutate it, then persist and journal atomically.

        Events recorded inside the block are detected by sequence and appended
        to the journal after the state file has been replaced. When
        ``persist_on_error`` is set, an exception raised inside the block does
        not roll back the recorded state: the workspace is still persisted and
        the exception is re-raised afterwards. This is used by commands such as
        shift closure that record an audit event and then reject the action.
        """

        with self._write_lock:
            workspace = self.repository.load()
            seen_sequences = {event.sequence for event in workspace.events}
            try:
                yield workspace
            except Exception:
                if not persist_on_error:
                    raise
                self.repository.save(workspace)
                for event in workspace.events:
                    if event.sequence not in seen_sequences:
                        self.repository.journal_event(workspace, event)
                raise
            self.repository.save(workspace)
            for event in workspace.events:
                if event.sequence not in seen_sequences:
                    self.repository.journal_event(workspace, event)

    def commit(self, workspace: YardWorkspace, events: list[object] | object | None = None) -> None:
        with self._write_lock:
            self.repository.save(workspace)
            if events is None:
                return
            items = events if isinstance(events, list) else [events]
            for event in items:
                self.repository.journal_event(workspace, event)

    def data_path(self) -> str:
        return self.repository.path_text()


__all__ = ["YardApplication"]
