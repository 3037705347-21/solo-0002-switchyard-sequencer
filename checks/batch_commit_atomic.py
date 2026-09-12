"""Storage-level check: batch commits are all-or-nothing.

A commit must land state and events together. This check injects failures at
both write boundaries (event journal append, atomic state replace) and proves
that neither the state file, the journal, nor the in-memory view keeps any
part of the batch afterwards. It also covers a partial journal write.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

from support import PROJECT_ROOT, SRC_DIR

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from switchyard.service import batch_service, intake_service, shift_service  # noqa: E402
from switchyard.service.context import YardApplication  # noqa: E402
from switchyard.storage.journal import EventJournal, JournalWriteError  # noqa: E402


def batch_document() -> dict[str, object]:
    return {
        "code": "BATCH-ATOMIC",
        "received_at": "2026-09-12T08:30:00Z",
        "trains": [
            {
                "code": "INT-101",
                "route": "RAIL-11",
                "arrival_at": "2026-09-12T08:10:00Z",
                "cars": [
                    {
                        "code": "C-N4-101",
                        "kind": "BOX",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-HAZ-101",
                        "kind": "TANK",
                        "destination": "W9",
                        "loaded": True,
                        "length_m": 22,
                        "danger_class": "D1",
                    },
                ],
            },
            {
                "code": "INT-102",
                "route": "RAIL-12",
                "arrival_at": "2026-09-12T08:12:00Z",
                "cars": [
                    {
                        "code": "C-E7-101",
                        "kind": "FLAT",
                        "destination": "E7",
                        "loaded": False,
                        "length_m": 16,
                        "danger_class": "NONE",
                    }
                ],
            },
        ],
    }


# shift baseline: 0 cars/intakes/batches, 1 event (SHIFT_OPENED)
BASELINE = (0, 0, 0, 1)


def state_counts(app: YardApplication) -> tuple[int, int, int, int]:
    raw = json.loads(app.repository.state_path.read_text(encoding="utf-8"))
    return (
        len(raw["cars"]),
        len(raw["intakes"]),
        len(raw["batch_intakes"]),
        len(raw["events"]),
    )


def journal_kinds(app: YardApplication) -> list[str]:
    return [item["kind"] for item in app.repository.journal.read_all()]


def workspace_is_clean(app: YardApplication) -> None:
    assert state_counts(app) == BASELINE, state_counts(app)
    workspace = app.load()
    assert workspace.cars == {}
    assert workspace.intakes == {}
    assert workspace.batch_intakes == {}
    assert [str(event.kind) for event in workspace.events] == ["SHIFT_OPENED"]
    assert journal_kinds(app) == ["SHIFT_OPENED"]


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-atomic-") as tmp:
        app = YardApplication(Path(tmp))
        shift_service.open_shift(
            app,
            {"code": "SHIFT-01", "dispatcher": "LIN", "opened_at": "2026-09-12T08:00:00Z"},
        )
        manifest = Path(tmp) / "batch.json"
        manifest.write_text(json.dumps(batch_document()), encoding="utf-8")
        real_save = app.repository.save

        # 1. Journal append fails before the state file is touched.
        def journal_down(payloads: list[dict[str, object]]) -> None:
            raise OSError("simulated journal outage")

        app.repository.journal.append_many = journal_down  # type: ignore[method-assign]
        try:
            batch_service.import_intake_batch(app, {"source_path": str(manifest)})
            raise AssertionError("expected journal failure to propagate")
        except OSError as exc:
            assert "journal outage" in str(exc)
        del app.repository.journal.append_many
        workspace_is_clean(app)

        # 2. State replace fails after events were journaled: the new journal
        #    lines must be rolled back so the audit trail cannot reference
        #    trains/cars/batches that the state file lacks.
        def state_down(workspace: object) -> None:
            raise OSError("simulated state write outage")

        app.repository.save = state_down  # type: ignore[method-assign]
        try:
            batch_service.import_intake_batch(app, {"source_path": str(manifest)})
            raise AssertionError("expected state failure to propagate")
        except OSError as exc:
            assert "state write outage" in str(exc)
        app.repository.save = real_save
        workspace_is_clean(app)

        # 3. The single-train entry shares the same commit path and must also
        #    roll events back when the state write fails.
        single_payload = {
            "code": "INT-900",
            "route": "RAIL-90",
            "arrival_at": "2026-09-12T09:00:00Z",
            "cars": [
                {
                    "code": "C-N4-900",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 12,
                    "danger_class": "NONE",
                }
            ],
        }
        app.repository.save = state_down  # type: ignore[method-assign]
        try:
            intake_service.create_intake(app, single_payload)
            raise AssertionError("expected state failure to propagate")
        except OSError as exc:
            assert "state write outage" in str(exc)
        app.repository.save = real_save
        workspace_is_clean(app)

        # 4. After failures, a retry commits normally: 3 cars, 2 trains,
        #    1 batch, shift + 2 train events + 1 batch event = 4 events.
        result = batch_service.import_intake_batch(app, {"source_path": str(manifest)})
        assert state_counts(app) == (3, 2, 1, 4), state_counts(app)
        assert journal_kinds(app) == [
            "SHIFT_OPENED",
            "TRAIN_RECEIVED",
            "TRAIN_RECEIVED",
            "BATCH_IMPORTED",
        ]
        assert result["batch"]["source_path"] == str(manifest.resolve())
        workspace = app.load()
        assert set(workspace.cars) == {"C-N4-101", "C-HAZ-101", "C-E7-101"}
        assert all(train.batch_code == "BATCH-ATOMIC" for train in workspace.intakes.values())

        # 5. A partial journal write (some bytes land, then the OS rejects the
        #    rest) self-truncates back to the pre-commit size.
        journal = app.repository.journal
        real_path = journal.path
        size_before = real_path.stat().st_size
        kinds_before = journal_kinds(app)

        class PartialHandle:
            def __init__(self, mode: str = "r", *args: object, **kwargs: object):
                self._handle = open(real_path, mode, *args, **kwargs)  # noqa: SIM115

            def __enter__(self) -> "PartialHandle":
                return self

            def __exit__(self, *args: object) -> None:
                self.close()

            def seek(self, *args: object):
                return self._handle.seek(*args)

            def write(self, data: bytes) -> int:
                written = self._handle.write(data[: max(1, len(data) // 3)])
                self._handle.flush()
                raise OSError("simulated partial write")

            def flush(self) -> None:
                self._handle.flush()

            def truncate(self, *args: object):
                return self._handle.truncate(*args)

            def fileno(self) -> int:
                return self._handle.fileno()

            def close(self) -> None:
                if not self._handle.closed:
                    self._handle.close()

        fake_path = types.SimpleNamespace()
        for attribute in ("stat", "is_file", "parent"):
            setattr(fake_path, attribute, getattr(real_path, attribute))
        fake_path.open = PartialHandle
        journal.path = fake_path
        try:
            journal.append_many([{"sequence": 999, "kind": "GHOST_EVENT"}])
            raise AssertionError("expected JournalWriteError on partial write")
        except JournalWriteError as exc:
            assert "partial write" in str(exc)
        journal.path = real_path
        assert real_path.stat().st_size == size_before
        assert journal_kinds(app) == kinds_before


if __name__ == "__main__":
    run()
    print(f"OK batch_commit_atomic ({PROJECT_ROOT.name})")
    raise SystemExit(0)
