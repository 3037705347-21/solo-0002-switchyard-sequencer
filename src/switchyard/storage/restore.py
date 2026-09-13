"""Restore a validated backup packet into a live data directory.

The restore never writes directly into the target.  Inspection happens in an
isolated staging directory, normalized output is prepared there, and only then
is the target swapped with a directory-level atomic rename.  A rollback rename
restores the previous target when the commit or its post-commit verification
cannot complete, so a failed restore leaves the target directory exactly as it
was.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomicfile import read_json_if_present
from .backup import (
    JOURNAL_FILE,
    MANIFEST_FILE,
    STATE_FILE,
    BackupManifest,
    Issue,
    canonical_payloads,
    inspect_packet,
    parse_journal_bytes,
)
from .migration import migrate_journal_events, migrate_state
from .repository import STATE_FILE as REPO_STATE_FILE


class RestoreRejected(Exception):
    """The restore was refused before any target data changed."""

    def __init__(self, message: str, issues: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.issues = issues or []


@dataclass(slots=True)
class RestoreReport:
    packet_path: Path
    target_dir: Path
    restored: bool
    replaced_existing: bool
    migrated: bool
    manifest: BackupManifest | None = None
    issues: list[Issue] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_path": str(self.packet_path),
            "target_dir": str(self.target_dir),
            "restored": self.restored,
            "replaced_existing": self.replaced_existing,
            "migrated": self.migrated,
            "manifest": None if self.manifest is None else self.manifest.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
            "summary": dict(self.summary),
        }


def target_has_state(target_dir: Path | str) -> bool:
    return (Path(target_dir) / REPO_STATE_FILE).is_file()


def restore_backup(
    packet_path: Path | str,
    target_dir: Path | str,
    *,
    replace_existing: bool = False,
) -> RestoreReport:
    """Validate a packet in isolation and commit it atomically on success."""

    packet = Path(packet_path)
    target = Path(target_dir)

    # Refuse up front when the target already runs with state.
    replaced_existing = False
    if target_has_state(target):
        if not replace_existing:
            existing = read_json_if_present(target / REPO_STATE_FILE)
            version = None
            try:
                version = dict(existing or {}).get("version")
            except (TypeError, ValueError):
                version = None
            raise RestoreRejected(
                f"target {target} already holds running state (state version {version}); "
                "pass replace_existing=True to replace it",
            )
        replaced_existing = True

    work_root = Path(tempfile.mkdtemp(prefix="sypack-restore-"))
    try:
        stage_root = work_root / "packet"
        stage_root.mkdir(parents=True, exist_ok=True)
        report = inspect_packet(packet, keep_stage=True, stage_root=stage_root)
        if not report.ok:
            errors = [issue.to_dict() for issue in report.errors()]
            raise RestoreRejected(
                f"packet {packet} failed validation; target left untouched",
                issues=errors,
            )

        migrated = report.migration is not None
        state_bytes = (report.stage_dir / STATE_FILE).read_bytes()
        journal_bytes = (report.stage_dir / JOURNAL_FILE).read_bytes()
        raw_state = json.loads(state_bytes.decode("utf-8"))
        journal_events = parse_journal_bytes(journal_bytes)
        if migrated:
            raw_state = migrate_state(raw_state)
            journal_events = migrate_journal_events(journal_events)
        state_bytes, journal_bytes = canonical_payloads(raw_state, journal_events)

        prepared = work_root / "prepared"
        prepared.mkdir(parents=True, exist_ok=True)
        (prepared / STATE_FILE).write_bytes(state_bytes)
        (prepared / JOURNAL_FILE).write_bytes(journal_bytes)
        # Carry the backup manifest along for an operator-auditable restore
        # record. It is written before the swap, so the commit stays atomic.
        if report.manifest is not None:
            manifest_text = json.dumps(
                report.manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
            (prepared / MANIFEST_FILE).write_text(manifest_text + "\n", encoding="utf-8")

        commit = _atomic_commit(prepared, target)
        try:
            # Post-commit verification against the bytes now at the target.
            committed_state = (target / STATE_FILE).read_bytes()
            committed_journal = (target / JOURNAL_FILE).read_bytes()
            if committed_state != state_bytes or committed_journal != journal_bytes:
                raise RestoreRejected("post-commit verification failed; rolling back")
        except BaseException:
            commit.rollback()
            raise
        commit.confirm()

        return RestoreReport(
            packet_path=packet,
            target_dir=target,
            restored=True,
            replaced_existing=replaced_existing,
            migrated=migrated,
            manifest=report.manifest,
            issues=list(report.issues),
            summary=dict(report.summary),
        )
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


class _CommitHandle:
    """Directory swap that can still be undone until confirmed."""

    def __init__(self, target: Path, hold: Path | None, backup_dir: Path):
        self._target = target
        self._hold = hold
        self._backup_dir = backup_dir
        self._closed = False

    def rollback(self) -> None:
        if self._closed:
            return
        if self._target.exists():
            shutil.rmtree(self._target, ignore_errors=True)
        if self._hold is not None:
            os.replace(self._hold, self._target)
        self._closed = True
        shutil.rmtree(self._backup_dir, ignore_errors=True)

    def confirm(self) -> None:
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._backup_dir, ignore_errors=True)


def _atomic_commit(prepared: Path, target: Path) -> _CommitHandle:
    """Swap the prepared directory into the target with a rollback handle."""

    target.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = Path(tempfile.mkdtemp(prefix="sypack-old-", dir=str(target.parent)))
    hold: Path | None = None
    try:
        if target.exists():
            hold = backup_dir / "previous"
            os.replace(target, hold)
        try:
            os.replace(prepared, target)
        except BaseException:
            if hold is not None and not target.exists():
                os.replace(hold, target)
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise
    except BaseException:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise
    return _CommitHandle(target, hold, backup_dir)


__all__ = [
    "Issue",
    "RestoreReport",
    "RestoreRejected",
    "restore_backup",
    "target_has_state",
]
