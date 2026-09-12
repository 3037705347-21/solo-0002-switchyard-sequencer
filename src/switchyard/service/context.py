"""Application context that owns the repository boundary."""

from __future__ import annotations

from pathlib import Path

from ..domain.errors import ResourceBusyError
from ..storage.repository import YardRepository
from ..storage.workspace import YardWorkspace


class YardApplication:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.repository = YardRepository(self.data_dir)

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


def require_open_shift(workspace: YardWorkspace, message_hint: str) -> str:
    """Return the open shift code or reject the command context."""
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint=message_hint)


__all__ = ["YardApplication", "require_open_shift"]
