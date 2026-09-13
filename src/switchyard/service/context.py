"""Application context that owns the repository boundary."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..storage.recovery import ConsistencyError, RecoveryReport
from ..storage.repository import YardRepository
from ..storage.workspace import YardWorkspace


class YardApplication:
    def __init__(self, data_dir: Path | str, crash_hook: Any = None):
        self.data_dir = Path(data_dir)
        self.repository = YardRepository(self.data_dir, crash_hook=crash_hook)
        # Serializes the whole load -> mutate -> commit span of a command so
        # concurrent HTTP workers cannot interleave commits.
        self.command_lock = threading.RLock()

    def bootstrap(self) -> tuple[YardWorkspace, RecoveryReport]:
        """Load and reconcile state/journal. Call once at process startup."""
        return self.repository.bootstrap()

    def load(self) -> YardWorkspace:
        # Commands assume reconciliation has run; bootstrap lazily for any
        # caller that skips the explicit startup hook.
        self.repository.ensure_bootstrapped()
        return self.repository.load()

    def commit(self, workspace: YardWorkspace, events: list[object] | object | None = None) -> RecoveryReport:
        if events is None:
            items: list[object] = []
        elif isinstance(events, list):
            items = events
        else:
            items = [events]
        return self.repository.commit(workspace, items)

    def recovery(self) -> dict[str, Any]:
        report = self.repository.last_recovery
        return report.as_dict() if report is not None else {"complete": False, "action": "not-bootstrapped"}

    def data_path(self) -> str:
        return self.repository.path_text()


__all__ = ["ConsistencyError", "RecoveryReport", "YardApplication"]
