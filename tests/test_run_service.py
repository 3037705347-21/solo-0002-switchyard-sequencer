"""Service-level pull execution tests: commit boundary and failure semantics.

These tests drive ``service.run_service.advance_run`` / ``depart_outbound``
against a real repository in a temporary directory.  The key property under
test is that a multi-step advance which fails midway persists nothing from
the failed batch: the workspace on disk stays at the last committed state and
can be retried, while a fresh in-memory copy shows no leaked mutations.
"""

from __future__ import annotations

import json
import unittest

from _support import TempDirCase, car_payload
from switchyard.domain.enums import CarState, RunState
from switchyard.domain.errors import (
    ConflictError,
    ResourceBusyError,
    ValidationError,
)
from switchyard.service.run_service import advance_run


def three_car_intake(app_caller, create_intake, classify) -> None:
    create_intake(
        "INT-1",
        [
            car_payload("C-AAAA"),
            car_payload("C-BBB1"),
            car_payload("C-BBB2"),
        ],
    )
    result = classify("INT-1")
    return result


class AdvanceHappyPathTest(TempDirCase):
    def _plan_deep_pull(self) -> str:
        self.open_shift()
        three_car_intake(None, self.create_intake, self.classify)
        workspace = self.load()
        # Bottom -> top on N4-A: C-A, C-B1, C-B2 (manifest order).
        self.assertEqual(workspace.tracks["N4-A"].stack, ["C-AAAA", "C-BBB1", "C-BBB2"])
        self.create_outbound("OB-1", "N4", ["C-AAAA"])
        sequenced = self.sequence("OB-1")
        return sequenced["pull_run"]["code"]

    def test_advance_in_batches_then_complete(self) -> None:
        run_code = self._plan_deep_pull()
        first = self.advance(run_code, steps=2)
        self.assertFalse(first["completed"])
        self.assertEqual(first["executed_steps"], 2)
        self.assertEqual(first["pull_run"]["state"], "RUNNING")
        workspace = self.load()
        run = workspace.runs[run_code]
        self.assertEqual(run.state, RunState.RUNNING)
        self.assertEqual(run.current_step, 2)
        # Two blockers buffered: bay LIFO is [B1, B2] (B2 on top).
        self.assertEqual(workspace.buffer_bays["X1"].stack, ["C-BBB2", "C-BBB1"])
        self.assertEqual(workspace.cars["C-BBB2"].location, "X1")
        self.assertEqual(workspace.cars["C-AAAA"].state, CarState.RESERVED)

        second = self.advance(run_code, steps=3)
        self.assertTrue(second["completed"])
        self.assertEqual(second["pull_run"]["state"], "COMPLETED")
        self.assertEqual(second["outbound"]["state"], "READY")
        self.assertEqual(second["outbound"]["assembled_car_codes"], ["C-AAAA"])
        workspace = self.load()
        self.assertEqual(workspace.tracks["N4-A"].stack, ["C-BBB1", "C-BBB2"])
        self.assertEqual(workspace.buffer_bays["X1"].stack, [])
        self.assertEqual(workspace.cars["C-AAAA"].state, CarState.ASSEMBLED)
        self.assertEqual(workspace.cars["C-BBB1"].state, CarState.STANDING)

    def test_default_steps_is_one(self) -> None:
        run_code = self._plan_deep_pull()
        result = self.advance(run_code)
        self.assertEqual(result["executed_steps"], 1)
        self.assertEqual(result["pull_run"]["current_step"], 1)

    def test_full_advance_and_depart(self) -> None:
        run_code = self._plan_deep_pull()
        advanced = self.advance(run_code, steps=200)
        self.assertTrue(advanced["completed"])
        departed = self.depart("OB-1")
        self.assertEqual(departed["outbound"]["state"], "DEPARTED")
        self.assertEqual(departed["departed_car_count"], 1)
        workspace = self.load()
        self.assertEqual(workspace.cars["C-AAAA"].state, CarState.DEPARTED)
        self.assertEqual(workspace.cars["C-AAAA"].location, "OB-1")
        self.assertIsNotNone(workspace.outbounds["OB-1"].departed_at)

    def test_cannot_depart_before_ready(self) -> None:
        run_code = self._plan_deep_pull()
        with self.assertRaises(ValidationError) as caught:
            self.depart("OB-1")
        self.assertEqual(caught.exception.status, 422)
        # The planned run is untouched.
        self.assertEqual(self.load().runs[run_code].state, RunState.QUEUED)

    def test_advance_unknown_and_unknown_outbound_depart(self) -> None:
        self.open_shift()
        from switchyard.domain.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.advance("RUN-NOPE")
        with self.assertRaises(NotFoundError):
            self.depart("OB-NOPE")

    def test_advance_completed_run_conflicts(self) -> None:
        run_code = self._plan_deep_pull()
        self.advance(run_code, steps=200)
        with self.assertRaises(ConflictError):
            self.advance(run_code)

    def test_advance_without_open_shift(self) -> None:
        # Fresh seeded workspace, no shift at all.
        with self.assertRaises(ResourceBusyError):
            advance_run(self.app, "RUN-OB-1", {"steps": 1})


class MidBatchFailureCommitTest(TempDirCase):
    def _planned_deep_run(self) -> str:
        self.open_shift()
        three_car_intake(None, self.create_intake, self.classify)
        self.create_outbound("OB-1", "N4", ["C-AAAA"])
        sequenced = self.sequence("OB-1")
        return sequenced["pull_run"]["code"]

    def _snapshot_persisted(self) -> dict:
        state_path = self.data_dir / "yard-state.json"
        return json.loads(state_path.read_text(encoding="utf-8"))

    def test_mid_batch_failure_persists_nothing_and_is_retriable(self) -> None:
        run_code = self._planned_deep_run()
        before = self._snapshot_persisted()

        # Advance one step (buffer C-B2) and commit.
        first = self.advance(run_code, steps=1)
        self.assertEqual(first["executed_steps"], 1)
        committed = self.load()
        self.assertEqual(committed.runs[run_code].state, RunState.RUNNING)
        self.assertEqual(committed.buffer_bays["X1"].stack, ["C-BBB2"])
        last_committed_version = int(self._snapshot_persisted()["version"])

        # Sabotage the committed track stack between requests: the next buffer
        # step expects C-B1 on top of N4-A, but it is no longer there.
        state_path = self.data_dir / "yard-state.json"
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        for track in raw["tracks"]:
            if track["code"] == "N4-A":
                track["stack"] = ["C-AAAA"]
        state_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

        with self.assertRaises(ResourceBusyError) as caught:
            self.advance(run_code, steps=1)
        self.assertIn("on top of", str(caught.exception))

        # (1) Disk state must be exactly the last committed state: the failed
        # batch neither advanced current_step nor moved cars into the bay.
        after_failure = self._snapshot_persisted()
        self.assertEqual(int(after_failure["version"]), last_committed_version)
        n4 = next(track for track in after_failure["tracks"] if track["code"] == "N4-A")
        self.assertEqual(n4["stack"], ["C-AAAA"])
        run = next(item for item in after_failure["pull_runs"] if item["code"] == run_code)
        self.assertEqual(run["current_step"], 1)
        self.assertEqual(run["state"], "RUNNING")
        bay = after_failure["buffer_bays"][0]
        self.assertEqual(bay["stack"], ["C-BBB2"])

        # (2) A freshly loaded workspace (what the next HTTP request sees) has
        # no leaked in-memory mutation: the failed buffer was discarded.
        fresh = self.load()
        self.assertEqual(fresh.buffer_bays["X1"].stack, ["C-BBB2"])
        self.assertEqual(fresh.cars["C-BBB1"].state, CarState.STANDING)
        self.assertEqual(fresh.cars["C-BBB1"].location, "N4-A")

        # (3) Restore the blocker and retry: the run continues from the last
        # committed step and completes normally.
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        for track in raw["tracks"]:
            if track["code"] == "N4-A":
                track["stack"] = ["C-AAAA", "C-BBB1"]
        state_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        completed = self.advance(run_code, steps=10)
        self.assertTrue(completed["completed"])
        self.assertEqual(completed["pull_run"]["state"], "COMPLETED")
        self.assertEqual(completed["outbound"]["assembled_car_codes"], ["C-AAAA"])
        final = self.load()
        self.assertEqual(final.tracks["N4-A"].stack, ["C-BBB1", "C-BBB2"])
        self.assertEqual(final.buffer_bays["X1"].stack, [])

        # The failed request must not have recorded an extra event compared
        # with the state right after the first successful advance.
        committed_event_count = len(committed.events)
        self.assertEqual(len(after_failure["events"]), committed_event_count)

    def test_failed_advance_does_not_journal_events(self) -> None:
        run_code = self._planned_deep_run()
        self.advance(run_code, steps=1)
        journal_path = self.data_dir / "events.jsonl"
        lines_before = len(journal_path.read_text(encoding="utf-8").splitlines())
        raw = json.loads((self.data_dir / "yard-state.json").read_text(encoding="utf-8"))
        for track in raw["tracks"]:
            if track["code"] == "N4-A":
                track["stack"] = ["C-AAAA"]
        (self.data_dir / "yard-state.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ResourceBusyError):
            self.advance(run_code, steps=1)
        lines_after = len(journal_path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(lines_after, lines_before)

    def test_failed_run_is_left_running_not_marked_failed(self) -> None:
        # Documents actual behavior: the FAILED state in the transition table
        # is never written by advance_run; a failed action keeps the run in
        # RUNNING so it can be retried. See the test report for the gap.
        run_code = self._planned_deep_run()
        self.advance(run_code, steps=1)
        raw = json.loads((self.data_dir / "yard-state.json").read_text(encoding="utf-8"))
        for track in raw["tracks"]:
            if track["code"] == "N4-A":
                track["stack"] = ["C-AAAA"]
        (self.data_dir / "yard-state.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ResourceBusyError):
            self.advance(run_code, steps=1)
        self.assertEqual(self.load().runs[run_code].state, RunState.RUNNING)


class DepartureValidationTest(TempDirCase):
    def test_depart_rejects_when_assembly_list_disagrees(self) -> None:
        # The service guards depart with assembly_complete(); engineer a
        # completed run whose outbound list disagrees via direct mutation of a
        # freshly persisted state, proving the guard reads committed state.
        self.open_shift()
        three_car_intake(None, self.create_intake, self.classify)
        self.create_outbound("OB-1", "N4", ["C-BBB2"])
        sequenced = self.sequence("OB-1")
        run_code = sequenced["pull_run"]["code"]
        self.advance(run_code, steps=10)
        state_path = self.data_dir / "yard-state.json"
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        for outbound in raw["outbounds"]:
            if outbound["code"] == "OB-1":
                outbound["assembled_car_codes"] = ["C-UNEXPECTED"]
        state_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.depart("OB-1")

    def test_depart_requires_all_cars_assembled(self) -> None:
        self.open_shift()
        three_car_intake(None, self.create_intake, self.classify)
        self.create_outbound("OB-1", "N4", ["C-BBB2"])
        self.sequence("OB-1")
        # READY is required; PLANNED depart is refused (already covered above
        # from the service API) — here also verify car-state guard through a
        # READY outbound with a tampered car.
        run_code = "RUN-OB-1"
        self.advance(run_code, steps=10)
        state_path = self.data_dir / "yard-state.json"
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        for car in raw["cars"]:
            if car["code"] == "C-BBB2":
                car["state"] = "RESERVED"
                car["location"] = "N4-A"
        state_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValidationError) as caught:
            self.depart("OB-1")
        self.assertIn("not in assembled state", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
