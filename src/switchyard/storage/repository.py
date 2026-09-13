"""Load, commit, and reconcile the workspace through atomic JSON writes."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .atomicfile import append_text_lines, atomic_write_json, ensure_parent, read_json_if_present
from .codec import decode_workspace, encode_workspace
from .digest import event_content_hash, new_commit_id
from .journal import EventJournal, commit_marker, event_line
from .recovery import ConsistencyError, RecoveryReport, reconcile
from .seed import build_seed_workspace
from .workspace import YardWorkspace

STATE_FILE = "yard-state.json"
JOURNAL_FILE = "events.jsonl"

# Fault injection hooks (used by the persistence checks). The value names the
# point at which the process must "die" during a commit:
#   after_state          state replaced, journal untouched
#   after_event_lines    event lines appended, marker missing
CRASH_HOOK_ENV = "SWITCHYARD_CRASH_AFTER"


class YardRepository:
    def __init__(self, data_dir: Path | str, crash_hook: Any = None):
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / STATE_FILE
        self.journal_path = self.data_dir / JOURNAL_FILE
        ensure_parent(self.state_path)
        self.journal = EventJournal(self.journal_path)
        self._lock = threading.RLock()
        self._bootstrapped = False
        self.last_recovery: RecoveryReport | None = None
        # Test seam: callable(str) that simulates a hard process interruption.
        self.crash_hook = crash_hook

    # ------------------------------------------------------------------
    def exists(self) -> bool:
        return self.state_path.is_file()

    def load(self) -> YardWorkspace:
        raw = read_json_if_present(self.state_path)
        if raw is None:
            workspace = build_seed_workspace()
            self.rewrite_state(workspace)
            return workspace
        return decode_workspace(dict(raw))

    def bootstrap(self) -> tuple[YardWorkspace, RecoveryReport]:
        """Load once at startup and reconcile state against the journal."""
        with self._lock:
            workspace = self.load()
            report = reconcile(self, workspace)
            self.last_recovery = report
            self._bootstrapped = True
            return workspace, report

    def ensure_bootstrapped(self) -> None:
        if not self._bootstrapped:
            self.bootstrap()

    # ------------------------------------------------------------------
    def rewrite_state(self, workspace: YardWorkspace) -> None:
        """Persist the state as-is (no version bump, no commit entry)."""
        atomic_write_json(self.state_path, encode_workspace(workspace))

    def append_journal_lines(self, lines: list[str]) -> None:
        append_text_lines(self.journal_path, lines)

    # ------------------------------------------------------------------
    def _crash(self, point: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point)
            return
        import os
        import sys

        wanted = os.environ.get(CRASH_HOOK_ENV)
        if wanted and point == wanted:
            # Simulate a hard interruption exactly as a kill -9 would: no
            # finally blocks, no buffered flush. stderr is unbuffered enough
            # for the checks to observe the point.
            sys.stderr.write(f"[fault-injection] simulating crash at {point}\n")
            sys.stderr.flush()
            os._exit(91)

    def commit(self, workspace: YardWorkspace, events: list[Any]) -> RecoveryReport:
        """Persist one business command atomically-and-detectably.

        Ordering: state first (durable marker of the decision), then the
        journal batch with the commit marker last. A startup reconciliation
        bridges the only ambiguous window (state present, journal pending).
        """
        with self._lock:
            self.ensure_bootstrapped()
            ordered = list(events)
            commit_number = workspace.next_commit_number()
            event_items: list[tuple[dict[str, Any], str]] = []
            for event in ordered:
                event_dict = event.to_dict()
                event_items.append((event_dict, event_content_hash(event_dict)))

            # The version bump must happen before the state is written so the
            # ledger entry records the exact version landing on disk.
            workspace.bump()
            commit_id = new_commit_id(commit_number)
            workspace.commits.append(
                {
                    "commit_id": commit_id,
                    "commit_number": commit_number,
                    "state_version": workspace.version,
                    "event_sequences": [int(item[0]["sequence"]) for item in event_items],
                    "event_hashes": [item[1] for item in event_items],
                }
            )
            self.rewrite_state(workspace)
            self._crash("after_state")

            # Events first as one durable batch, then the commit marker. The
            # marker is the commit boundary: a missing marker is detectable
            # and either completed from state or discarded at next startup.
            event_lines = [
                event_line(event_dict, content_hash, commit_id) for event_dict, content_hash in event_items
            ]
            if event_lines:
                append_text_lines(self.journal_path, event_lines)
            self._crash("after_event_lines")
            marker = commit_marker(
                commit_id=commit_id,
                commit_number=commit_number,
                state_version=workspace.version,
                event_sequences=[int(item[0]["sequence"]) for item in event_items],
                event_hashes=[item[1] for item in event_items],
            )
            append_text_lines(self.journal_path, [marker])

            report = self.last_recovery or RecoveryReport()
            report.last_commit_id = commit_id
            return report

    def path_text(self) -> str:
        return str(self.state_path)


__all__ = [
    "CRASH_HOOK_ENV",
    "JOURNAL_FILE",
    "STATE_FILE",
    "ConsistencyError",
    "YardRepository",
]
