"""Startup reconciliation between the workspace state and the event journal.

Commit protocol (see ``YardRepository.commit``):

1. A business command mutates the in-memory workspace and records events.
2. A commit ledger entry (commit id, state version, event sequences and
   content hashes) is appended to ``workspace.commits`` and the state file is
   atomically replaced and fsync'd.
3. The journal receives one fsync'd batch: one framed event line per event,
   then a commit marker. The marker is the commit boundary.

Crash windows and startup verdicts:

* crash before step 2 finishes     -> old state, old journal: no commit.
* crash between step 2 and step 3  -> the state contains exactly one tail
  commit whose marker is absent: it is *rolled forward* from the state, so
  the business result is neither lost nor re-applied.
* crash inside the step 3 batch    -> event lines without a marker; if they
  belong to the single pending tail commit the batch is completed, otherwise
  (commit absent from state) the partial lines are discarded.
* duplicate journal lines          -> same commit id / (commit id, sequence)
  appears twice: duplicates are stripped via a canonical journal rewrite.
* hash mismatch, marker gap, state ahead by >1 commit, unknown marker ->
  unrecoverable divergence: the service refuses to serve the data rather
  than silently choosing one side as history.

Pre-commit-framing state files and bare-event journals are migrated once,
automatically, without manual log edits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .digest import event_content_hash
from .journal import (
    JOURNAL_FORMAT,
    RECORD_COMMIT,
    RECORD_EVENT,
    JournalParseError,
    canonical_commit_lines,
)


class ConsistencyError(RuntimeError):
    """State and journal disagree in a way automatic recovery cannot fix."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass(slots=True)
class RecoveryReport:
    complete: bool = True
    recoverable: bool = True
    action: str = "none"  # none | rollforward | migrated | repaired
    last_commit_id: str | None = None
    rolled_forward_commits: list[str] = field(default_factory=list)
    removed_duplicate_events: int = 0
    removed_duplicate_commits: int = 0
    removed_partial_events: int = 0
    completed_partial_commits: int = 0
    state_changed: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "recoverable": self.recoverable,
            "action": self.action,
            "last_commit_id": self.last_commit_id,
            "rolled_forward_commits": list(self.rolled_forward_commits),
            "removed_duplicate_events": self.removed_duplicate_events,
            "removed_duplicate_commits": self.removed_duplicate_commits,
            "removed_partial_events": self.removed_partial_events,
            "completed_partial_commits": self.completed_partial_commits,
            "notes": list(self.notes),
        }


def _events_by_sequence(workspace: Any) -> dict[int, dict[str, Any]]:
    return {int(event.sequence): event.to_dict() for event in workspace.events}


def _migrate_legacy_state(workspace: Any, report: RecoveryReport) -> None:
    """Seal pre-framing state into one synthetic baseline commit."""
    if workspace.commits:
        return
    sequences = sorted(int(event.sequence) for event in workspace.events)
    if not sequences:
        # Empty seed written by a modern build: nothing to seal.
        return
    by_sequence = _events_by_sequence(workspace)
    workspace.commits = [
        {
            "commit_id": "C-00000001-legacy",
            "commit_number": 1,
            "state_version": int(workspace.version),
            "event_sequences": sequences,
            "event_hashes": [event_content_hash(by_sequence[sequence]) for sequence in sequences],
            "legacy": True,
        }
    ]
    report.state_changed = True
    report.notes.append(
        f"state file predates commit framing; {len(sequences)} event(s) sealed as C-00000001-legacy"
    )


def _validate_state_ledger(
    state_commits: list[dict[str, Any]], events_by_sequence: dict[int, dict[str, Any]]
) -> None:
    previous_number = 0
    previous_version = 0
    for entry in state_commits:
        number = int(entry["commit_number"])
        version = int(entry["state_version"])
        if number != previous_number + 1:
            raise ConsistencyError(
                "state commit ledger has a gap", expected=previous_number + 1, found=number
            )
        if not entry.get("legacy") and version <= previous_version:
            raise ConsistencyError(
                "state commit version did not advance", commit_number=number, state_version=version
            )
        sequences = [int(value) for value in entry.get("event_sequences", [])]
        for sequence in sequences:
            if sequence not in events_by_sequence:
                raise ConsistencyError(
                    "state commit references a missing event",
                    commit_id=entry["commit_id"],
                    event_sequence=sequence,
                )
        expected_hashes = [event_content_hash(events_by_sequence[sequence]) for sequence in sequences]
        stored_hashes = list(entry.get("event_hashes", []))
        if stored_hashes and stored_hashes != expected_hashes:
            raise ConsistencyError(
                "state commit event hashes do not match stored events", commit_id=entry["commit_id"]
            )
        if not stored_hashes:
            entry["event_hashes"] = expected_hashes
        previous_number, previous_version = number, version


def _check_legacy_journal(
    records: list[dict[str, Any]], events_by_sequence: dict[int, dict[str, Any]]
) -> None:
    legacy: dict[int, dict[str, Any]] = {}
    for record in records:
        try:
            sequence = int(record["sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConsistencyError("legacy journal line is not an event", record=record) from exc
        legacy[sequence] = record
    unknown = sorted(set(legacy) - set(events_by_sequence))
    if unknown:
        raise ConsistencyError(
            "legacy journal references events absent from state", event_sequences=unknown
        )
    for sequence, record in legacy.items():
        if event_content_hash(record) != event_content_hash(events_by_sequence[sequence]):
            raise ConsistencyError(
                "legacy journal event content disagrees with state", event_sequence=sequence
            )


def reconcile(repository: Any, workspace: Any) -> RecoveryReport:
    """Make journal and state agree, repairing the documented crash windows."""
    report = RecoveryReport()
    _migrate_legacy_state(workspace, report)

    state_commits = [dict(entry) for entry in workspace.commits]
    events_by_sequence = _events_by_sequence(workspace)
    _validate_state_ledger(state_commits, events_by_sequence)
    state_ids = [str(entry["commit_id"]) for entry in state_commits]
    state_by_id = {str(entry["commit_id"]): entry for entry in state_commits}

    try:
        records = repository.journal.read_records()
    except JournalParseError as exc:
        raise ConsistencyError("journal is corrupted and unreadable", detail=str(exc)) from exc

    canonical = lambda: canonical_commit_lines(state_commits, events_by_sequence, event_content_hash)

    if records and all(record.get("journal") != JOURNAL_FORMAT for record in records):
        _check_legacy_journal(records, events_by_sequence)
        repository.journal.rewrite_canonical(canonical())
        report.action = "migrated"
        report.notes.append(f"journal migrated from {len(records)} bare event line(s)")
        return _finish(repository, workspace, report, state_commits)

    # ---- parse framed records ------------------------------------------
    events_for_commit: dict[str, list[dict[str, Any]]] = {}
    markers: list[dict[str, Any]] = []
    seen_event_keys: set[tuple[str, int]] = set()
    seen_marker_ids: set[str] = set()
    for record in records:
        if record.get("journal") != JOURNAL_FORMAT:
            raise ConsistencyError("journal mixes framed and unframed lines", record=record)
        kind = record.get("record")
        if kind == RECORD_EVENT:
            commit_id = str(record.get("commit_id", ""))
            event_dict = dict(record.get("event") or {})
            try:
                sequence = int(event_dict["sequence"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ConsistencyError("journal event lacks a sequence", record=record) from exc
            if record.get("content_hash") != event_content_hash(event_dict):
                raise ConsistencyError(
                    "journal event content hash mismatch",
                    commit_id=commit_id,
                    event_sequence=sequence,
                )
            key = (commit_id, sequence)
            if key in seen_event_keys:
                report.removed_duplicate_events += 1
                continue
            seen_event_keys.add(key)
            events_for_commit.setdefault(commit_id, []).append(event_dict)
        elif kind == RECORD_COMMIT:
            commit_id = str(record.get("commit_id", ""))
            if commit_id in seen_marker_ids:
                report.removed_duplicate_commits += 1
                continue
            seen_marker_ids.add(commit_id)
            markers.append(record)
        else:
            raise ConsistencyError("unknown journal record type", record=record)

    # ---- markers must form a strict prefix of the state ledger ---------
    marker_ids = [str(marker.get("commit_id", "")) for marker in markers]
    for commit_id in marker_ids:
        if commit_id not in state_by_id:
            raise ConsistencyError("journal contains a commit absent from state", commit_id=commit_id)
    if marker_ids != state_ids[: len(marker_ids)]:
        raise ConsistencyError(
            "journal commit ordering disagrees with the state ledger",
            journal_commits=marker_ids,
            state_commits=state_ids,
        )

    for marker, commit_id in zip(markers, marker_ids):
        entry = state_by_id[commit_id]
        expected_sequences = [int(value) for value in entry["event_sequences"]]
        actual_sequences = sorted(int(event["sequence"]) for event in events_for_commit.get(commit_id, []))
        if actual_sequences != expected_sequences:
            raise ConsistencyError(
                "journal commit event set does not match state",
                commit_id=commit_id,
                expected=expected_sequences,
                found=actual_sequences,
            )
        expected_hashes = [event_content_hash(events_by_sequence[s]) for s in expected_sequences]
        if list(marker.get("event_hashes", [])) != expected_hashes:
            raise ConsistencyError("journal marker hashes disagree with state", commit_id=commit_id)
        if int(marker.get("state_version", -1)) != int(entry["state_version"]):
            raise ConsistencyError("journal marker state_version disagrees", commit_id=commit_id)
        if int(marker.get("commit_number", -1)) != int(entry["commit_number"]):
            raise ConsistencyError("journal marker commit_number disagrees", commit_id=commit_id)

    # ---- event lines without a marker: torn batch or uncommitted noise -
    pending_id = state_ids[len(marker_ids)] if len(marker_ids) < len(state_ids) else None
    partial_tail = False
    for commit_id, events in events_for_commit.items():
        if commit_id in seen_marker_ids:
            continue
        if commit_id == pending_id:
            expected = [int(value) for value in state_by_id[commit_id]["event_sequences"]]
            actual = sorted(int(event["sequence"]) for event in events)
            if actual != expected:
                raise ConsistencyError(
                    "torn journal batch for the pending commit",
                    commit_id=commit_id,
                    expected=expected,
                    found=actual,
                )
            partial_tail = True
            report.completed_partial_commits += 1
            report.notes.append(f"completed a torn journal batch for {commit_id}")
        else:
            report.removed_partial_events += len(events)
            report.notes.append(
                f"discarded {len(events)} event line(s) for uncommitted/unknown {commit_id}"
            )

    # ---- the state may be at most one tail commit ahead of the journal -
    missing = state_commits[len(marker_ids):]
    if len(missing) > 1:
        raise ConsistencyError(
            "state is ahead of the journal by more than one commit; refusing to guess",
            state_commits=len(state_commits),
            journal_commits=len(marker_ids),
        )

    needs_rewrite = bool(
        report.removed_duplicate_events
        or report.removed_duplicate_commits
        or report.removed_partial_events
        or partial_tail
    )
    if missing:
        if needs_rewrite:
            repository.journal.rewrite_canonical(canonical())
        else:
            repository.append_journal_lines(canonical_commit_lines(missing, events_by_sequence, event_content_hash))
        report.rolled_forward_commits = [str(entry["commit_id"]) for entry in missing]
        report.action = "repaired" if needs_rewrite else "rollforward"
        report.notes.append(f"rolled forward pending commit {report.rolled_forward_commits} from state")
    elif needs_rewrite:
        repository.journal.rewrite_canonical(canonical())
        report.action = "repaired"
        report.notes.append("journal rewritten to remove duplicate or partial records")

    return _finish(repository, workspace, report, state_commits)


def _finish(
    repository: Any, workspace: Any, report: RecoveryReport, state_commits: list[dict[str, Any]]
) -> RecoveryReport:
    report.complete = True
    report.recoverable = True
    report.last_commit_id = str(state_commits[-1]["commit_id"]) if state_commits else None
    if report.state_changed:
        # Persist legacy migration / hash backfill without adding a commit.
        repository.rewrite_state(workspace)
    return report


__all__ = ["ConsistencyError", "RecoveryReport", "reconcile"]
