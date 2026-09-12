"""Persistence tests: codec round-trip, atomic writes, empty-dir seeding.

Covers ``storage.codec`` (full workspace encode/decode including stacks,
events and closure snapshots), ``storage.atomicfile`` (temp-file + replace
semantics, failure cleanup, fsync text path), ``storage.repository`` (fresh
directory initialization and reload), and the append-only event journal.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import make_car
from switchyard.domain.enums import (
    CarState,
    EventKind,
    IntakeState,
    MoveVerb,
    OutboundState,
    RunState,
    ShiftState,
    TrackPurpose,
)
from switchyard.domain.intake import IntakeTrain
from switchyard.domain.outbound import OutboundTrain
from switchyard.domain.pull import MoveStep, PullRun, YardEvent
from switchyard.domain.shift import YardShift
from switchyard.storage import codec
from switchyard.storage.atomicfile import (
    append_text_line,
    atomic_write_json,
    atomic_write_text,
    read_json_file,
    read_json_if_present,
)
from switchyard.storage.journal import EventJournal
from switchyard.storage.repository import JOURNAL_FILE, STATE_FILE, YardRepository
from switchyard.storage.seed import build_seed_workspace, seed_bays, seed_tracks
from switchyard.storage.workspace import SCHEMA_VERSION, YardWorkspace


class SeedWorkspaceTest(unittest.TestCase):
    def test_fresh_seed_contents(self) -> None:
        workspace = build_seed_workspace()
        codes = [track.code for track in seed_tracks()]
        self.assertEqual(list(workspace.tracks), codes)
        self.assertTrue(any(track.purpose == TrackPurpose.DESTINATION for track in workspace.tracks.values()))
        self.assertTrue(any(track.hazard_rated for track in workspace.tracks.values()))
        maint = workspace.tracks["MAINT-1"]
        self.assertEqual(str(maint.state), "MAINTENANCE")
        self.assertEqual([bay.code for bay in seed_bays()], ["X1"])
        self.assertEqual(list(workspace.buffer_bays), ["X1"])
        self.assertEqual(workspace.version, 1)
        self.assertEqual(workspace.schema_version, SCHEMA_VERSION)
        self.assertEqual(workspace.next_event_sequence, 1)

    def test_repository_initializes_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="switchyard-persist-") as directory:
            data_dir = Path(directory) / "nested" / "yard"
            repository = YardRepository(data_dir)
            self.assertFalse(repository.exists())
            workspace = repository.load()
            # Loading a missing workspace seeds and persists it immediately.
            self.assertTrue(repository.exists())
            state_path = data_dir / STATE_FILE
            self.assertTrue(state_path.is_file())
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 1)
            self.assertGreaterEqual(len(raw["tracks"]), 7)
            self.assertEqual(raw["cars"], [])
            # Journal file is not created until an event is appended.
            self.assertFalse((data_dir / JOURNAL_FILE).exists())
            # A second repository over the same directory loads the same data.
            again = YardRepository(data_dir).load()
            self.assertEqual(list(again.tracks), list(workspace.tracks))


class CodecRoundTripTest(unittest.TestCase):
    def _populated_workspace(self) -> YardWorkspace:
        workspace = build_seed_workspace()
        car = make_car("C-N4-1", destination="N4", danger_class="D1", note="haz")
        car.state = CarState.RESERVED
        car.location = "N4-A"
        workspace.cars["C-N4-1"] = car
        workspace.tracks["N4-A"].stack.append("C-N4-1")
        intake = IntakeTrain(code="INT-1", route="R1", arrival_at="2026-09-10T09:00:00Z", consist=["C-N4-1"])
        intake.state = IntakeState.CLASSIFIED
        intake.placed_at = "2026-09-10T09:05:00Z"
        workspace.intakes["INT-1"] = intake
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-N4-1"])
        outbound.state = OutboundState.PLANNED
        outbound.run_codes = ["RUN-OB-1"]
        workspace.outbounds["OB-1"] = outbound
        run = PullRun(
            code="RUN-OB-1",
            outbound_code="OB-1",
            transfer_code="X1",
            steps=[MoveStep(verb=MoveVerb.PULL, car_code="C-N4-1", source_code="N4-A", target_code="OB-1")],
            state=RunState.QUEUED,
        )
        workspace.runs["RUN-OB-1"] = run
        shift = YardShift(code="SHIFT-1", dispatcher="LIN", opened_at="2026-09-10T08:00:00Z")
        shift.state = ShiftState.OPEN
        workspace.shifts["SHIFT-1"] = shift
        event = YardEvent(
            sequence=1,
            at="2026-09-10T08:00:00Z",
            shift_code="SHIFT-1",
            kind=EventKind.SHIFT_OPENED,
            message="shift opened",
            payload={"dispatcher": "LIN"},
        )
        workspace.events.append(event)
        workspace.next_event_sequence = 2
        workspace.closure_snapshots.append({"code": "SNAP-SHIFT-1", "metrics": {"total_cars": 1}, "blockers": []})
        workspace.version = 7
        return workspace

    def test_encode_decode_preserves_everything(self) -> None:
        workspace = self._populated_workspace()
        encoded = codec.encode_workspace(workspace)
        # Encoding must produce JSON-safe primitives only.
        json.dumps(encoded)
        decoded = codec.decode_workspace(encoded)
        self.assertEqual(decoded.version, 7)
        self.assertEqual(decoded.schema_version, SCHEMA_VERSION)
        self.assertEqual(decoded.next_event_sequence, 2)
        self.assertEqual(decoded.tracks["N4-A"].stack, ["C-N4-1"])
        self.assertEqual(decoded.cars["C-N4-1"].state, CarState.RESERVED)
        self.assertEqual(decoded.cars["C-N4-1"].danger_class, "D1")
        self.assertEqual(decoded.cars["C-N4-1"].note, "haz")
        self.assertEqual(decoded.intakes["INT-1"].state, IntakeState.CLASSIFIED)
        self.assertEqual(decoded.intakes["INT-1"].placed_at, "2026-09-10T09:05:00Z")
        self.assertEqual(decoded.outbounds["OB-1"].state, OutboundState.PLANNED)
        self.assertEqual(decoded.outbounds["OB-1"].run_codes, ["RUN-OB-1"])
        run = decoded.runs["RUN-OB-1"]
        self.assertEqual(run.state, RunState.QUEUED)
        self.assertEqual(run.steps[0].car_code, "C-N4-1")
        self.assertEqual(str(run.steps[0].verb), "PULL")
        self.assertEqual(decoded.shifts["SHIFT-1"].state, ShiftState.OPEN)
        self.assertEqual(len(decoded.events), 1)
        self.assertEqual(decoded.events[0].kind, EventKind.SHIFT_OPENED)
        self.assertEqual(decoded.events[0].payload, {"dispatcher": "LIN"})
        self.assertEqual(decoded.closure_snapshots[0]["code"], "SNAP-SHIFT-1")

    def test_decode_copies_collections(self) -> None:
        original_workspace = self._populated_workspace()
        encoded = codec.encode_workspace(original_workspace)
        decoded = codec.decode_workspace(encoded)
        decoded.tracks["N4-A"].stack.append("C-MUT")
        decoded.cars["C-N4-1"].note = "changed"
        encoded_again = codec.encode_workspace(decoded)
        # Re-decoding the original payload yields the original values.
        original = codec.decode_workspace(encoded)
        self.assertEqual(original.tracks["N4-A"].stack, ["C-N4-1"])
        self.assertEqual(original.cars["C-N4-1"].note, "haz")
        self.assertNotEqual(encoded_again, encoded)

    def test_decode_applies_defaults_for_legacy_fields(self) -> None:
        raw = {
            "schema_version": 1,
            "version": 3,
            "tracks": [
                {
                    "code": "T1",
                    "purpose": "GENERAL",
                    "capacity_cars": 5,
                    "capacity_length_m": 100,
                }
            ],
            "buffer_bays": [{"code": "X1", "capacity_cars": 2}],
        }
        workspace = codec.decode_workspace(raw)
        self.assertEqual(str(workspace.tracks["T1"].state), "OPERATIONAL")
        self.assertEqual(workspace.tracks["T1"].stack, [])
        self.assertEqual(workspace.buffer_bays["X1"].stack, [])
        self.assertEqual(workspace.cars, {})
        self.assertEqual(workspace.next_event_sequence, 1)

    def test_repository_save_bumps_version_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="switchyard-persist-") as directory:
            repository = YardRepository(Path(directory))
            workspace = repository.load()
            # Loading an empty dir seeds AND persists immediately, so the
            # in-memory copy is already at version 2 (seed built at 1, save
            # bumped to 2).
            self.assertEqual(workspace.version, 2)
            repository.save(workspace)
            reloaded = repository.load()
            # The explicit save bumped 2 -> 3; reload is a pure read.
            self.assertEqual(reloaded.version, 3)
            self.assertEqual(list(reloaded.tracks), list(repository.load().tracks))


class AtomicWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="switchyard-atomic-")
        self.directory = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_atomic_write_replaces_existing_file(self) -> None:
        target = self.directory / "state" / "yard-state.json"
        atomic_write_json(target, {"version": 1})
        first = target.read_text(encoding="utf-8")
        self.assertTrue(first.endswith("\n"))
        self.assertEqual(read_json_file(target), {"version": 1})
        atomic_write_json(target, {"version": 2})
        self.assertEqual(read_json_file(target), {"version": 2})
        self.assertEqual(read_json_if_present(target), {"version": 2})

    def test_read_if_present_returns_none_for_missing_file(self) -> None:
        missing = self.directory / "absent.json"
        self.assertIsNone(read_json_if_present(missing))

    def test_no_temp_files_left_behind(self) -> None:
        target = self.directory / "yard-state.json"
        for version in range(3):
            atomic_write_json(target, {"version": version})
        leftovers = [name for name in os.listdir(self.directory) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_failed_write_keeps_original_and_removes_temp(self) -> None:
        target = self.directory / "yard-state.json"
        atomic_write_text(target, "ORIGINAL")
        # Force the flush/fsync stage to fail: replace must never happen and
        # the temporary file must be cleaned up.
        with mock.patch("os.fsync", side_effect=OSError("simulated disk fault")):
            with self.assertRaises(OSError):
                atomic_write_text(target, "CORRUPTED")
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL")
        leftovers = [name for name in os.listdir(self.directory) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_json_is_pretty_sorted_and_utf8(self) -> None:
        target = self.directory / "yard-state.json"
        atomic_write_json(target, {"b": "铁路", "a": 1})
        text = target.read_text(encoding="utf-8")
        self.assertIn("铁路", text)  # ensure_ascii=False keeps Chinese readable
        self.assertIn("\n", text.rstrip("\n"))  # pretty-printed over multiple lines
        lines = [line for line in text.splitlines() if '"a"' in line or '"b"' in line]
        self.assertEqual(lines[0].strip(), '"a": 1,')  # sort_keys=True

    def test_append_text_line_adds_single_newline(self) -> None:
        target = self.directory / "events.jsonl"
        append_text_line(target, '{"a": 1}')
        append_text_line(target, '{"b": 2}\n')
        content = target.read_text(encoding="utf-8")
        self.assertEqual(content, '{"a": 1}\n{"b": 2}\n')


class JournalTest(unittest.TestCase):
    def test_missing_and_single_line_journal_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="switchyard-journal-") as directory:
            path = Path(directory) / "events.jsonl"
            journal = EventJournal(path)
            self.assertEqual(journal.read_all(), [])
            journal.append({"sequence": 1, "kind": "SHIFT_OPENED"})
            records = journal.read_all()
            self.assertEqual(records, [{"sequence": 1, "kind": "SHIFT_OPENED"}])

    @unittest.expectedFailure
    def test_contract_read_all_should_parse_multi_entry_jsonl(self) -> None:
        # CONTRACT GAP: EventJournal.read_all calls json.load on the whole
        # file, which raises json.JSONDecodeError as soon as the append-only
        # journal contains two or more lines.  Production code only appends
        # (read_all is currently unused), but any future replay breaks.
        with tempfile.TemporaryDirectory(prefix="switchyard-journal-") as directory:
            path = Path(directory) / "events.jsonl"
            journal = EventJournal(path)
            journal.append({"sequence": 1, "kind": "SHIFT_OPENED"})
            journal.append({"sequence": 2, "kind": "TRAIN_RECEIVED"})
            records = journal.read_all()
            self.assertEqual([item["sequence"] for item in records], [1, 2])

    def test_repository_journals_event_after_state_save(self) -> None:
        with tempfile.TemporaryDirectory(prefix="switchyard-persist-") as directory:
            repository = YardRepository(Path(directory))
            workspace = repository.load()
            event = workspace.record_event(
                "SHIFT-1",
                EventKind.SHIFT_OPENED,
                "shift opened",
                {"dispatcher": "LIN"},
            )
            repository.save(workspace)
            repository.journal_event(workspace, event)
            journal_path = Path(directory) / JOURNAL_FILE
            lines = journal_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["kind"], "SHIFT_OPENED")
            self.assertEqual(payload["shift_code"], "SHIFT-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
