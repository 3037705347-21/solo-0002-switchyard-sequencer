"""Service-level workflow tests driving commands against a real repository.

Covers the four workflows end to end in memory + temp-dir persistence, plus
service-boundary failure assertions: shift uniqueness, duplicate trains and
cars, missing resources, classification conflicts, outbound eligibility and
double planning, closure blockers (open/partial intakes, queued/running runs,
maintenance tracks with cars, unclassified cars, active outbounds), blocked
closure journaling, and a clean closure snapshot.
"""

from __future__ import annotations

import json
import unittest

from _support import TempDirCase, car_payload
from switchyard.domain.enums import CarState, OutboundState
from switchyard.domain.errors import (
    ConflictError,
    NotFoundError,
    ResourceBusyError,
    ValidationError,
)
from switchyard.report.closure import closure_blockers
from switchyard.service.intake_service import classify_intake_command, create_intake
from switchyard.service.outbound_service import create_outbound, sequence_outbound
from switchyard.service.query_service import yard_view
from switchyard.service.shift_service import get_shift


def n4_car(code: str, **overrides) -> dict:
    return car_payload(code, destination="N4", **overrides)


class ShiftLifecycleTest(TempDirCase):
    def test_open_and_get_shift_records_event(self) -> None:
        result = self.open_shift("SHIFT-1", "LIN")
        self.assertEqual(result["state"], "OPEN")
        view = get_shift(self.app, "SHIFT-1")
        self.assertEqual(view["shift"]["code"], "SHIFT-1")
        self.assertEqual(view["events"][0]["kind"], "SHIFT_OPENED")

    def test_duplicate_shift_conflicts(self) -> None:
        self.open_shift("SHIFT-1")
        with self.assertRaises(ConflictError):
            self.open_shift("SHIFT-1")

    def test_only_one_open_shift_at_a_time(self) -> None:
        self.open_shift("SHIFT-1")
        with self.assertRaises(ResourceBusyError) as caught:
            self.open_shift("SHIFT-2")
        self.assertEqual(caught.exception.payload.get("open_shift"), "SHIFT-1")

    def test_get_missing_shift(self) -> None:
        with self.assertRaises(NotFoundError):
            get_shift(self.app, "SHIFT-NOPE")

    def test_no_shift_blocks_yard_work(self) -> None:
        with self.assertRaises(ResourceBusyError):
            create_intake(self.app, {"code": "INT-1", "route": "R", "arrival_at": "2026-09-10T09:00:00Z",
                                     "cars": [n4_car("C-N4-1")]})


class IntakeServiceTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.open_shift()

    def test_create_and_classify_persists_cars_standing(self) -> None:
        created = self.create_intake("INT-1", [n4_car("C-N4-1"), n4_car("C-N4-2")])
        self.assertEqual(created["intake"]["state"], "OPEN")
        self.assertEqual(len(created["cars"]), 2)
        self.assertEqual(created["cars"][0]["state"], "RECEIVED")
        classified = self.classify("INT-1")
        self.assertEqual(classified["intake"]["state"], "CLASSIFIED")
        self.assertEqual(classified["unplaced"], [])
        workspace = self.load()
        self.assertEqual(workspace.cars["C-N4-1"].state, CarState.STANDING)
        self.assertIn("C-N4-1", workspace.tracks["N4-A"].stack)

    def test_duplicate_intake_code_conflicts(self) -> None:
        self.create_intake("INT-1", [n4_car("C-N4-1")])
        with self.assertRaises(ConflictError):
            self.create_intake("INT-1", [n4_car("C-N4-9")])

    def test_duplicate_car_code_across_trains_conflicts(self) -> None:
        self.create_intake("INT-1", [n4_car("C-N4-1")])
        with self.assertRaises(ConflictError):
            self.create_intake("INT-2", [n4_car("C-N4-1")])

    def test_classify_unknown_intake(self) -> None:
        with self.assertRaises(NotFoundError):
            classify_intake_command(self.app, "INT-NOPE")

    def test_double_classify_is_rejected(self) -> None:
        self.create_intake("INT-1", [n4_car("C-N4-1")])
        self.classify("INT-1")
        with self.assertRaises(ValidationError) as caught:
            self.classify("INT-1")
        self.assertIn("already classified", str(caught.exception))

    def test_failed_create_persists_nothing(self) -> None:
        before = self.load()
        with self.assertRaises(ValidationError):
            create_intake(
                self.app,
                {"code": "INT-BAD", "route": "R", "arrival_at": "2026-09-10T09:00:00Z", "cars": []},
            )
        after = self.load()
        self.assertNotIn("INT-BAD", after.intakes)
        self.assertEqual(after.version, before.version)
        self.assertEqual(len(after.events), len(before.events))


class PartialClassificationServiceTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.open_shift()

    def test_unplaceable_hazcar_makes_partial_intake(self) -> None:
        # HAZ-1 has 8 cars / 240m capacity. Fill it first so the new haz car
        # has nowhere to go, then classify a mixed intake.
        fillers = [car_payload(f"C-HF-{i}", destination="W9", kind="TANK", danger_class="D1", length_m=29)
                   for i in range(8)]
        self.create_intake("INT-FILL", fillers)
        fill_result = self.classify("INT-FILL")
        self.assertEqual(fill_result["intake"]["state"], "CLASSIFIED")
        self.create_intake(
            "INT-PART",
            [n4_car("C-N4-1"), car_payload("C-HAZ-9", destination="W9", kind="TANK", danger_class="D2", length_m=20)],
        )
        result = self.classify("INT-PART")
        self.assertEqual(result["intake"]["state"], "PARTIAL")
        self.assertEqual(result["unplaced"], ["C-HAZ-9"])
        workspace = self.load()
        self.assertEqual(workspace.cars["C-N4-1"].state, CarState.STANDING)
        self.assertEqual(workspace.cars["C-HAZ-9"].state, CarState.RECEIVED)
        # Partial intake appears in closure blockers.
        blockers = closure_blockers(workspace)
        kinds = {(item["kind"], item["code"]) for item in blockers}
        self.assertIn(("intake", "INT-PART"), kinds)
        self.assertIn(("unclassified_car", "C-HAZ-9"), kinds)


class OutboundServiceTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.open_shift()
        self.create_intake("INT-1", [n4_car("C-N4-1"), n4_car("C-N4-2")])
        self.classify("INT-1")

    def test_create_outbound_requires_standing_destination_match(self) -> None:
        # Car for the wrong destination.
        with self.assertRaises(ValidationError):
            create_outbound(self.app, {"code": "OB-9", "destination": "E7", "car_codes": ["C-N4-1"]})
        # Unknown car.
        with self.assertRaises(ValidationError):
            create_outbound(self.app, {"code": "OB-9", "destination": "N4", "car_codes": ["C-GONE"]})
        # Not stacked (tamper location).
        workspace = self.load()
        workspace.cars["C-N4-1"].location = "NOWHERE"
        self.app.commit(workspace)
        with self.assertRaises(ValidationError):
            create_outbound(self.app, {"code": "OB-9", "destination": "N4", "car_codes": ["C-N4-1"]})

    def test_car_reserved_by_another_outbound_is_busy(self) -> None:
        first = self.create_outbound("OB-1", "N4", ["C-N4-1"])
        self.assertEqual(first["state"], "DRAFT")
        # Even a DRAFT outbound occupies the car for another draft.
        with self.assertRaises(ResourceBusyError):
            self.create_outbound("OB-2", "N4", ["C-N4-1"])

    def test_double_sequence_rejected_and_failed_sequence_persists_nothing(self) -> None:
        self.create_outbound("OB-1", "N4", ["C-N4-1"])
        first = self.sequence("OB-1")
        self.assertEqual(first["outbound"]["state"], "PLANNED")
        with self.assertRaises(ValidationError):
            sequence_outbound(self.app, "OB-1", {"transfer_code": "X1"})
        # Sequencing an unknown outbound.
        with self.assertRaises(NotFoundError):
            sequence_outbound(self.app, "OB-NOPE", {"transfer_code": "X1"})
        # Unknown transfer bay: plan failure must leave no run behind.
        self.create_outbound("OB-2", "N4", ["C-N4-2"])
        with self.assertRaises(ValidationError):
            sequence_outbound(self.app, "OB-2", {"transfer_code": "ZZ"})
        self.assertNotIn("RUN-OB-2", self.load().runs)
        self.assertEqual(self.load().outbounds["OB-2"].state, OutboundState.DRAFT)

    def test_sequence_impossible_lifo_fails_closed(self) -> None:
        # Stack bottom -> top is [C-N4-1, C-N4-2]; planning the deep car
        # first ([C-N4-1, C-N4-2]) violates LIFO and must fail closed.
        self.create_outbound("OB-3", "N4", ["C-N4-1", "C-N4-2"])
        with self.assertRaises(ValidationError) as caught:
            self.sequence("OB-3")
        self.assertIn("blocked-sequence", caught.exception.payload.get("sequencer", []))
        self.assertIn("must be pulled before", str(caught.exception))
        workspace = self.load()
        self.assertNotIn("RUN-OB-3", workspace.runs)
        self.assertEqual(workspace.cars["C-N4-2"].state, CarState.STANDING)
        self.assertEqual(workspace.outbounds["OB-3"].state, OutboundState.DRAFT)

    def test_duplicate_outbound_code_conflicts(self) -> None:
        self.create_outbound("OB-1", "N4", ["C-N4-1"])
        with self.assertRaises(ConflictError):
            self.create_outbound("OB-1", "N4", ["C-N4-2"])


class FullWorkflowAndClosureTest(TempDirCase):
    def _build_completed_workflow(self) -> None:
        self.open_shift("SHIFT-1")
        self.create_intake("INT-1", [n4_car("C-AAAA"), n4_car("C-BBB0")])
        self.classify("INT-1")
        # Stack bottom -> top is [C-A, C-B]; plan top car first.
        self.create_outbound("OB-1", "N4", ["C-BBB0"])
        self.sequence("OB-1")
        advanced = self.advance("RUN-OB-1", steps=10)
        self.assertTrue(advanced["completed"])
        self.depart("OB-1")

    def test_end_to_end_shift_then_clean_closure(self) -> None:
        self._build_completed_workflow()
        closed = self.close_shift("SHIFT-1")
        self.assertEqual(closed["shift"]["state"], "CLOSED")
        self.assertEqual(closed["snapshot"]["code"], "SNAP-SHIFT-1")
        self.assertEqual(closed["metrics"]["car_state_counts"]["departed"], 1)
        self.assertEqual(closed["metrics"]["car_state_counts"]["standing"], 1)
        workspace = self.load()
        self.assertEqual(str(workspace.shifts["SHIFT-1"].state), "CLOSED")
        self.assertEqual(workspace.shifts["SHIFT-1"].closure_snapshot_code, "SNAP-SHIFT-1")
        self.assertEqual(len(workspace.closure_snapshots), 1)
        # Yard view is read-only and reports no active shift.
        yard = yard_view(self.app)
        self.assertEqual(yard["active_shift"], "NONE")
        self.assertEqual(yard["metrics"]["car_state_counts"]["departed"], 1)

    def test_close_unknown_or_double_close(self) -> None:
        self.open_shift("SHIFT-1")
        with self.assertRaises(NotFoundError):
            self.close_shift("SHIFT-NOPE")
        # Open intake blocks closure.
        self.create_intake("INT-1", [n4_car("C-AAAA")])
        with self.assertRaises(ResourceBusyError):
            self.close_shift("SHIFT-1")
        self.classify("INT-1")
        self.close_shift("SHIFT-1")
        with self.assertRaises(ResourceBusyError):
            self.close_shift("SHIFT-1")

    def test_blocked_closure_records_event_but_keeps_shift_open(self) -> None:
        self.open_shift("SHIFT-1")
        self.create_intake("INT-1", [n4_car("C-AAAA")])
        # Intake left OPEN (never classified).
        with self.assertRaises(ResourceBusyError) as caught:
            self.close_shift("SHIFT-1")
        blocker_codes = {item["code"] for item in caught.exception.payload["blockers"]}
        self.assertIn("INT-1", blocker_codes)
        workspace = self.load()
        self.assertEqual(str(workspace.shifts["SHIFT-1"].state), "OPEN")
        kinds = [event.kind.value for event in workspace.events]
        self.assertIn("CLOSURE_BLOCKED", kinds)
        self.assertEqual(workspace.closure_snapshots, [])

    def test_queued_and_running_runs_block_closure(self) -> None:
        self.open_shift("SHIFT-1")
        # Three cars -> stack [C-AAAA, C-BBB1, C-BBB2]; planning the deep
        # target yields a 5-step run so partial advancement stays RUNNING.
        self.create_intake("INT-1", [n4_car("C-AAAA"), n4_car("C-BBB1"), n4_car("C-BBB2")])
        self.classify("INT-1")
        self.create_outbound("OB-1", "N4", ["C-AAAA"])
        self.sequence("OB-1")
        workspace = self.load()
        self.assertIn(("pull_run", "RUN-OB-1"),
                      {(item["kind"], item["code"]) for item in closure_blockers(workspace)})
        with self.assertRaises(ResourceBusyError):
            self.close_shift("SHIFT-1")
        # A partially advanced (RUNNING) run still blocks.
        self.advance("RUN-OB-1", steps=2)
        self.assertEqual(self.load().runs["RUN-OB-1"].state.value, "RUNNING")
        with self.assertRaises(ResourceBusyError):
            self.close_shift("SHIFT-1")
        # After completion and departure, the run no longer blocks.
        self.advance("RUN-OB-1", steps=10)
        self.depart("OB-1")
        closed = self.close_shift("SHIFT-1")
        self.assertEqual(closed["shift"]["state"], "CLOSED")

    def test_maintenance_track_with_cars_blocks_closure(self) -> None:
        self.open_shift("SHIFT-1")
        self.create_intake("INT-1", [n4_car("C-AAAA")])
        self.classify("INT-1")
        workspace = self.load()
        # Simulate cars parked on the maintenance track.
        workspace.tracks["MAINT-1"].stack.append("C-AAAA")
        workspace.cars["C-AAAA"].location = "MAINT-1"
        self.app.commit(workspace)
        with self.assertRaises(ResourceBusyError) as caught:
            self.close_shift("SHIFT-1")
        blocker = [item for item in caught.exception.payload["blockers"] if item["kind"] == "maintenance_track"]
        self.assertEqual(len(blocker), 1)
        self.assertEqual(blocker[0]["code"], "MAINT-1")

    def test_draft_outbound_blocks_closure(self) -> None:
        self.open_shift("SHIFT-1")
        self.create_intake("INT-1", [n4_car("C-AAAA")])
        self.classify("INT-1")
        self.create_outbound("OB-1", "N4", ["C-AAAA"])
        with self.assertRaises(ResourceBusyError):
            self.close_shift("SHIFT-1")

    def test_work_after_closed_shift_is_rejected(self) -> None:
        self._build_completed_workflow()
        self.close_shift("SHIFT-1")
        with self.assertRaises(ResourceBusyError):
            self.create_intake("INT-9", [n4_car("C-N4-9")])

    def test_state_file_grows_versions_and_journal_tells_story(self) -> None:
        self._build_completed_workflow()
        self.close_shift("SHIFT-1")
        state_path = self.data_dir / "yard-state.json"
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        # Every successful command bumps the version at least once.
        self.assertGreaterEqual(raw["version"], 8)
        journal_path = self.data_dir / "events.jsonl"
        kinds = [json.loads(line)["kind"] for line in journal_path.read_text(encoding="utf-8").splitlines()]
        for expected in [
            "SHIFT_OPENED",
            "TRAIN_RECEIVED",
            "TRAIN_CLASSIFIED",
            "TRAIN_CREATED",
            "PULL_PLANNED",
            "PULL_RUN_STARTED",
            "PULL_RUN_COMPLETED",
            "TRAIN_DEPARTED",
            "SHIFT_CLOSED",
        ]:
            self.assertIn(expected, kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
