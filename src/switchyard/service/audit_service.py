"""Read-only event audit and reconciliation service.

This service never loads the workspace through the writing repository path and
never appends correction events: a missing or damaged journal must neither hide
problems nor block normal yard service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain.timeutil import now_iso
from ..report.reconciliation import apply_filters, index_snapshot, normalize_journal, reconcile
from ..storage.journal import read_journal_lines
from ..storage.repository import JOURNAL_FILE, STATE_FILE
from .context import YardApplication


def read_audit_document(
    app: YardApplication,
    *,
    shift: str | None = None,
    kind: str | None = None,
    car: str | None = None,
    pull_run: str | None = None,
) -> dict[str, Any]:
    data_dir = Path(app.data_dir)
    state_path = data_dir / STATE_FILE
    journal_path = data_dir / JOURNAL_FILE

    state_raw: dict[str, Any] | None = None
    state_read_error: str | None = None
    if state_path.is_file():
        try:
            with state_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                state_raw = loaded
            else:
                state_read_error = "state snapshot root is not a JSON object"
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            state_read_error = f"{type(exc).__name__}: {exc}"
    # A missing state file is not fatal: the journal still audits on its own.

    journal_records = normalize_journal(read_journal_lines(journal_path))
    snapshot = index_snapshot(state_raw)

    document = reconcile(
        journal_records,
        snapshot,
        data_dir=str(data_dir),
        state_file=str(state_path),
        journal_file=str(journal_path),
        generated_at=now_iso(),
        state_read_error=state_read_error,
    )
    return apply_filters(document, shift=shift, kind=kind, car=car, pull_run=pull_run)


__all__ = ["read_audit_document"]
