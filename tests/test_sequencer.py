"""LIFO pull-plan sequencing tests.

Covers ``domain.sequencer.plan_pull_run``: simple top pulls, deep pulls with
buffer/return ordering, multiple planned cars from one stack, multi-track
planning, car/blocker eligibility failures, destination mismatch, buffer
capacity overflow, car reservation and outbound state transitions, plus the
documented contract gap where a MAINTENANCE track is accepted as a pull source.
"""

from __future__ import annotations

import unittest

from _support import make_bay, make_car, make_track
from switchyard.domain.enums import CarState, MoveVerb, OutboundState, TrackState
from switchyard.domain.errors import StateTransitionError, ValidationError
from switchyard.domain.outbound import OutboundTrain
from switchyard.domain.sequencer import can_sequence, plan_pull_run


def standing_car(code: str, track: str, **overrides) -> object:
    car = make_car(code, **overrides)
    car.state = CarState.STANDING
    car.location = track
    return car


def seed_stack(tracks: dict, cars: dict, track_code: str, codes: list[str], destination: str = "N4") -> None:
    for code in codes:
        cars[code] = standing_car(code, track_code, destination=destination)
    tracks[track_code].stack.extend(codes)


def plan(outbound, cars, tracks, bays, transfer_code="X1", run_code="RUN-OB-1"):
    return plan_pull_run(run_code, outbound, cars, tracks, bays, transfer_code)


def verbs_of(run) -> list[str]:
    return [str(step.verb) for step in run.steps]


def steps_for(run, car_code: str) -> list:
    return [step for step in run.steps if step.car_code == car_code]


class SimplePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracks = {"MIX-1": make_track("MIX-1", capacity_cars=10, capacity_length_m=500)}
        self.bays = {"X1": make_bay("X1", 10)}
        self.cars: dict = {}
        seed_stack(self.tracks, self.cars, "MIX-1", ["C-N4-1", "C-N4-2"])

    def _outbound(self, codes=("C-N4-2",)) -> OutboundTrain:
        return OutboundTrain(code="OB-1", destination="N4", planned_car_codes=list(codes))

    def test_top_car_produces_single_pull(self) -> None:
        run = plan(self._outbound(), self.cars, self.tracks, self.bays)
        self.assertEqual(len(run.steps), 1)
        step = run.steps[0]
        self.assertEqual(step.verb, MoveVerb.PULL)
        self.assertEqual(step.car_code, "C-N4-2")
        self.assertEqual(step.source_code, "MIX-1")
        self.assertEqual(step.target_code, "OB-1")

    def test_planning_reserves_cars_and_moves_outbound_to_planned(self) -> None:
        outbound = self._outbound()
        run = plan(outbound, self.cars, self.tracks, self.bays)
        self.assertEqual(run.state.value, "QUEUED")
        self.assertEqual(outbound.state, OutboundState.PLANNED)
        self.assertIn(run.code, outbound.run_codes)
        self.assertEqual(self.cars["C-N4-2"].state, CarState.RESERVED)
        # The blocker below must remain standing and unreserved.
        self.assertEqual(self.cars["C-N4-1"].state, CarState.STANDING)

    def test_cannot_plan_same_outbound_twice(self) -> None:
        outbound = self._outbound()
        plan(outbound, self.cars, self.tracks, self.bays)
        with self.assertRaises(StateTransitionError):
            plan(outbound, self.cars, self.tracks, self.bays, run_code="RUN-OB-2")

    def test_unknown_transfer_bay(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            plan(self._outbound(), self.cars, self.tracks, {}, "ZZ")
        self.assertIn("transfer_code", caught.exception.payload)


class DeepPullOrderingTest(unittest.TestCase):
    def setUp(self) -> None:
        # Bottom -> top: [A, B1, B2] where A is the deep target.
        self.tracks = {"N4-A": make_track("N4-A", capacity_cars=10, capacity_length_m=500)}
        self.bays = {"X1": make_bay("X1", 10)}
        self.cars: dict = {}
        seed_stack(self.tracks, self.cars, "N4-A", ["C-A", "C-B1", "C-B2"])
        self.outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-A"])

    def test_buffer_pull_return_order_for_deep_car(self) -> None:
        run = plan(self.outbound, self.cars, self.tracks, self.bays)
        verbs = verbs_of(run)
        # Above cars are buffered top-first (B2 then B1), A pulled, then
        # blockers returned bottom-first (B1 then B2) to restore stack order.
        self.assertEqual(verbs, ["BUFFER", "BUFFER", "PULL", "RETURN", "RETURN"])
        self.assertEqual([step.car_code for step in run.steps], ["C-B2", "C-B1", "C-A", "C-B1", "C-B2"])
        self.assertEqual([step.source_code for step in run.steps[:2]], ["N4-A", "N4-A"])
        self.assertEqual([step.target_code for step in run.steps[:2]], ["X1", "X1"])
        self.assertEqual([step.source_code for step in run.steps[3:]], ["X1", "X1"])
        self.assertEqual([step.target_code for step in run.steps[3:]], ["N4-A", "N4-A"])

    def test_returned_blockers_restore_original_lifo_order(self) -> None:
        # Execute the plan against a workspace-like container to prove the
        # buffer and return moves leave the blockers in their original order.
        from switchyard.storage.workspace import YardWorkspace
        from switchyard.domain.executor import execute_step

        workspace = YardWorkspace(tracks=self.tracks, buffer_bays=self.bays, cars=self.cars, outbounds={"OB-1": self.outbound})
        run = plan(self.outbound, self.cars, self.tracks, self.bays)
        for step in run.steps:
            execute_step(workspace, run, step)
        self.assertEqual(self.tracks["N4-A"].stack, ["C-B1", "C-B2"])
        self.assertEqual(self.bays["X1"].stack, [])
        self.assertEqual(self.cars["C-A"].state, CarState.ASSEMBLED)
        self.assertEqual(self.cars["C-B1"].state, CarState.STANDING)
        self.assertEqual(self.cars["C-B2"].state, CarState.STANDING)
        self.assertEqual(self.outbound.assembled_car_codes, ["C-A"])

    def test_two_deep_cars_from_same_stack_share_blocker_burden(self) -> None:
        # Bottom -> top: [A1, A2, B]; valid LIFO planned order is top-first
        # [A2, A1].  Each pull buffers the common blocker B and restores it
        # before the next planned car is accessed.
        tracks = {"N4-A": make_track("N4-A", capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 10)}
        cars: dict = {}
        seed_stack(tracks, cars, "N4-A", ["C-A1", "C-A2", "C-B"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-A2", "C-A1"])
        run = plan(outbound, cars, tracks, bays)
        # A2: buffer B ; pull A2 ; return B  (working stack becomes [A1, B])
        # A1: buffer B ; pull A1 ; return B
        self.assertEqual(
            [step.car_code for step in run.steps],
            ["C-B", "C-A2", "C-B", "C-B", "C-A1", "C-B"],
        )
        self.assertEqual(
            verbs_of(run),
            ["BUFFER", "PULL", "RETURN", "BUFFER", "PULL", "RETURN"],
        )
        self.assertEqual(cars["C-A1"].state, CarState.RESERVED)
        self.assertEqual(cars["C-A2"].state, CarState.RESERVED)
        self.assertEqual(cars["C-B"].state, CarState.STANDING)

    def test_planned_order_must_respect_stack_order(self) -> None:
        # Bottom -> top: [A1, A2]; the valid LIFO order is [A2, A1].  Asking
        # for deep A1 first would require buffering A2 which is itself planned
        # later and must be rejected ("blocked-sequence").
        tracks = {"N4-A": make_track("N4-A", capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 10)}
        cars: dict = {}
        seed_stack(tracks, cars, "N4-A", ["C-A1", "C-A2"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-A1", "C-A2"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("blocked-sequence", caught.exception.payload["sequencer"])
        # Failure leaves the outbound in DRAFT and cars unreserved.
        self.assertEqual(outbound.state, OutboundState.DRAFT)
        self.assertEqual(cars["C-A1"].state, CarState.STANDING)
        self.assertEqual(cars["C-A2"].state, CarState.STANDING)


class MultiTrackPlanTest(unittest.TestCase):
    def test_plan_spanning_two_source_tracks(self) -> None:
        tracks = {
            "N4-A": make_track("N4-A", capacity_cars=10, capacity_length_m=500),
            "MIX-1": make_track("MIX-1", capacity_cars=10, capacity_length_m=500),
        }
        bays = {"X1": make_bay("X1", 10)}
        cars: dict = {}
        seed_stack(tracks, cars, "N4-A", ["C-A", "C-B"])
        seed_stack(tracks, cars, "MIX-1", ["C-C", "C-D"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-A", "C-D"])
        run = plan(outbound, cars, tracks, bays)
        self.assertEqual(
            [(str(step.verb), step.car_code, step.source_code, step.target_code) for step in run.steps],
            [
                ("BUFFER", "C-B", "N4-A", "X1"),
                ("PULL", "C-A", "N4-A", "OB-1"),
                ("RETURN", "C-B", "X1", "N4-A"),
                ("PULL", "C-D", "MIX-1", "OB-1"),
            ],
        )
        self.assertEqual(bays["X1"].stack, [])


class PlanningFailureTest(unittest.TestCase):
    def _world(self, **car_overrides) -> tuple[dict, dict, dict]:
        tracks = {"MIX-1": make_track("MIX-1", capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 10)}
        cars = {"C-N4-1": standing_car("C-N4-1", "MIX-1", **car_overrides)}
        tracks["MIX-1"].stack.append("C-N4-1")
        return tracks, bays, cars

    def test_missing_car(self) -> None:
        tracks, bays, cars = self._world()
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-GONE"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("car-missing", caught.exception.payload["sequencer"])
        self.assertEqual(outbound.state, OutboundState.DRAFT)

    def test_car_not_standing(self) -> None:
        tracks, bays, cars = self._world()
        cars["C-N4-1"].state = CarState.RESERVED
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-N4-1"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("car-not-standing", caught.exception.payload["sequencer"])

    def test_destination_mismatch(self) -> None:
        tracks, bays, cars = self._world(destination="E7")
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-N4-1"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("destination-mismatch", caught.exception.payload["sequencer"])

    def test_car_without_track_location(self) -> None:
        tracks, bays, cars = self._world()
        cars["C-N4-1"].location = None
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-N4-1"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("car-not-on-track", caught.exception.payload["sequencer"])

    def test_car_missing_from_stack(self) -> None:
        tracks, bays, cars = self._world()
        tracks["MIX-1"].stack.clear()
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-N4-1"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("car-not-in-stack", caught.exception.payload["sequencer"])

    def test_reserved_blocker_cannot_be_buffered(self) -> None:
        tracks = {"MIX-1": make_track("MIX-1", capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 10)}
        cars = {
            "C-TARGET": standing_car("C-TARGET", "MIX-1"),
            "C-RES": standing_car("C-RES", "MIX-1"),
        }
        cars["C-RES"].state = CarState.RESERVED
        tracks["MIX-1"].stack.extend(["C-TARGET", "C-RES"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-TARGET"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("blocker-reserved", caught.exception.payload["sequencer"])
        self.assertEqual(outbound.state, OutboundState.DRAFT)

    def test_buffer_overflow_uses_peak_concurrent_blockers(self) -> None:
        # Deep car with two blockers but a one-slot bay -> overflow.
        tracks = {"MIX-1": make_track("MIX-1", capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 1)}
        cars: dict = {}
        seed_stack(tracks, cars, "MIX-1", ["C-A", "C-B1", "C-B2"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-A"])
        with self.assertRaises(ValidationError) as caught:
            plan(outbound, cars, tracks, bays)
        self.assertIn("buffer-overflow", caught.exception.payload["sequencer"])
        # No reservations survive the failed plan.
        for car in cars.values():
            self.assertEqual(car.state, CarState.STANDING)

    def test_buffer_capacity_exactly_at_limit_is_allowed(self) -> None:
        tracks = {"MIX-1": make_track("MIX-1", capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 2)}
        cars: dict = {}
        seed_stack(tracks, cars, "MIX-1", ["C-A", "C-B1", "C-B2"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-A"])
        run = plan(outbound, cars, tracks, bays)
        self.assertEqual(len(run.steps), 5)


class MaintenanceSourceTest(unittest.TestCase):
    def _world(self) -> tuple[dict, dict, dict, OutboundTrain]:
        tracks = {"MAINT-1": make_track("MAINT-1", state=TrackState.MAINTENANCE, capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 10)}
        cars: dict = {}
        seed_stack(tracks, cars, "MAINT-1", ["C-N4-1"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-N4-1"])
        return tracks, bays, cars, outbound

    def test_actual_behavior_planner_accepts_maintenance_source(self) -> None:
        # Evidence for the contract gap: spec says a maintenance track cannot
        # be used as a pull source, but plan_pull_run never inspects state.
        tracks, bays, cars, outbound = self._world()
        run = plan(outbound, cars, tracks, bays)
        self.assertEqual(run.steps[0].source_code, "MAINT-1")
        self.assertEqual(outbound.state, OutboundState.PLANNED)

    @unittest.expectedFailure
    def test_contract_maintenance_track_should_reject_pull_source(self) -> None:
        # CONTRACT GAP (PROJECT_SPEC.md "State and rules"): the sequencer
        # should refuse to plan pulls from a MAINTENANCE track.
        tracks, bays, cars, outbound = self._world()
        with self.assertRaises(ValidationError):
            plan(outbound, cars, tracks, bays)


class CanSequenceProbeTest(unittest.TestCase):
    def test_probe_reports_unavailable_and_blocked_cars(self) -> None:
        tracks = {"MIX-1": make_track("MIX-1", capacity_cars=10, capacity_length_m=500)}
        bays = {"X1": make_bay("X1", 10)}
        cars: dict = {}
        seed_stack(tracks, cars, "MIX-1", ["C-A1", "C-A2"])
        outbound = OutboundTrain(code="OB-1", destination="N4", planned_car_codes=["C-A2", "C-A1"])
        failures = can_sequence(outbound, cars, tracks, bays)
        self.assertTrue(any("blocked by a later planned car" in item for item in failures))
        cars["C-A2"].destination = "E7"
        failures = can_sequence(
            OutboundTrain(code="OB-2", destination="N4", planned_car_codes=["C-A2"]),
            cars,
            tracks,
            bays,
        )
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main(verbosity=2)
