"""Pull-run executor tests at the domain boundary.

Covers ``domain.executor.execute_step``: buffer/pull/return preconditions,
stack and bay top verification, capacity rejection, state transitions, and
the key mid-failure semantics: when a step raises, earlier steps in the same
in-memory batch remain applied and there is no automatic rollback.  The
service layer's commit boundary is tested separately in
``test_run_service.py``.
"""

from __future__ import annotations

import unittest

from _support import make_bay, make_car, make_track
from switchyard.domain.enums import CarState, MoveVerb, RunState, TrackState
from switchyard.domain.errors import ResourceBusyError, StateTransitionError
from switchyard.domain.executor import execute_step
from switchyard.domain.outbound import OutboundTrain
from switchyard.domain.pull import MoveStep, PullRun
from switchyard.domain.sequencer import plan_pull_run
from switchyard.storage.workspace import YardWorkspace


def build_world(stack_bottom_to_top: list[str], bay_capacity: int = 10, track_code: str = "N4-A"):
    track = make_track(track_code, capacity_cars=20, capacity_length_m=900)
    bay = make_bay("X1", bay_capacity)
    cars: dict = {}
    for code in stack_bottom_to_top:
        car = make_car(code)
        car.state = CarState.STANDING
        car.location = track_code
        cars[code] = car
    track.stack.extend(stack_bottom_to_top)
    outbound = OutboundTrain(code="OB-1", destination="N4")
    workspace = YardWorkspace(
        tracks={track_code: track},
        buffer_bays={"X1": bay},
        cars=cars,
        outbounds={"OB-1": outbound},
    )
    return workspace, track, bay, cars, outbound


def planned_run(world, outbound, target_codes: list[str]) -> PullRun:
    workspace, track, bay, cars, _outbound = world
    outbound.planned_car_codes = list(target_codes)
    return plan_pull_run(
        "RUN-OB-1",
        outbound,
        cars,
        {track.code: track},
        {"X1": bay},
        "X1",
    )


class HappyPathExecutionTest(unittest.TestCase):
    def test_full_deep_pull_sequence_restores_blockers(self) -> None:
        world = build_world(["C-A", "C-B1", "C-B2"])
        workspace, track, bay, cars, outbound = world
        run = planned_run(world, outbound, ["C-A"])
        self.assertEqual(run.state, RunState.QUEUED)
        results = [execute_step(workspace, run, step) for step in run.steps]
        self.assertEqual([message.split()[0] for message in results], ["buffered", "buffered", "pulled", "returned", "returned"])
        self.assertEqual(track.stack, ["C-B1", "C-B2"])
        self.assertEqual(bay.stack, [])
        self.assertEqual(cars["C-A"].state, CarState.ASSEMBLED)
        self.assertEqual(cars["C-A"].location, "OB-1")
        self.assertEqual(cars["C-B1"].state, CarState.STANDING)
        self.assertEqual(cars["C-B2"].state, CarState.STANDING)
        self.assertEqual(outbound.assembled_car_codes, ["C-A"])

    def test_executing_on_queued_run_is_allowed_until_completion(self) -> None:
        world = build_world(["C-A"])
        workspace, track, bay, cars, outbound = world
        run = planned_run(world, outbound, ["C-A"])
        execute_step(workspace, run, run.steps[0])
        self.assertEqual(cars["C-A"].state, CarState.ASSEMBLED)

    def test_steps_serialize_round_trip(self) -> None:
        world = build_world(["C-A", "C-B"])
        workspace, track, bay, cars, outbound = world
        run = planned_run(world, outbound, ["C-A"])
        for raw, step in zip([item.to_dict() for item in run.steps], run.steps):
            restored = MoveStep.from_dict(raw)
            self.assertEqual(restored.verb, step.verb)
            self.assertEqual(restored.car_code, step.car_code)


class BufferPreconditionTest(unittest.TestCase):
    def test_buffer_requires_car_on_top(self) -> None:
        world = build_world(["C-A", "C-B"])
        workspace, track, bay, cars, _outbound = world
        step = MoveStep(MoveVerb.BUFFER, "C-A", track.code, "X1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        with self.assertRaises(ResourceBusyError) as caught:
            execute_step(workspace, run, step)
        self.assertIn("on top of", str(caught.exception))
        # Nothing moved.
        self.assertEqual(track.stack, ["C-A", "C-B"])
        self.assertEqual(bay.stack, [])

    def test_buffer_requires_standing_car(self) -> None:
        world = build_world(["C-A"])
        workspace, track, bay, cars, _outbound = world
        cars["C-A"].state = CarState.RESERVED
        step = MoveStep(MoveVerb.BUFFER, "C-A", track.code, "X1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        with self.assertRaises(StateTransitionError):
            execute_step(workspace, run, step)

    def test_buffer_rejects_full_bay(self) -> None:
        world = build_world(["C-A"], bay_capacity=1)
        workspace, track, bay, cars, _outbound = world
        # Occupy the only bay slot with another car.
        parked = make_car("C-PARK")
        parked.state = CarState.STANDING
        parked.location = "X1"
        bay.stack.append("C-PARK")
        workspace.cars["C-PARK"] = parked
        step = MoveStep(MoveVerb.BUFFER, "C-A", track.code, "X1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        with self.assertRaises(ResourceBusyError) as caught:
            execute_step(workspace, run, step)
        self.assertIn("no free capacity", str(caught.exception))
        self.assertEqual(track.stack, ["C-A"])

    def test_buffer_moves_car_location_to_bay(self) -> None:
        world = build_world(["C-A"])
        workspace, track, bay, cars, _outbound = world
        step = MoveStep(MoveVerb.BUFFER, "C-A", track.code, "X1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        execute_step(workspace, run, step)
        self.assertEqual(bay.stack, ["C-A"])
        self.assertEqual(cars["C-A"].location, "X1")
        self.assertEqual(cars["C-A"].state, CarState.STANDING)


class PullPreconditionTest(unittest.TestCase):
    def test_pull_requires_reserved_car(self) -> None:
        world = build_world(["C-A"])
        workspace, track, bay, cars, _outbound = world
        # Standing, not reserved: must fail.
        step = MoveStep(MoveVerb.PULL, "C-A", track.code, "OB-1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        with self.assertRaises(StateTransitionError):
            execute_step(workspace, run, step)
        self.assertEqual(track.stack, ["C-A"])

    def test_pull_requires_top_position(self) -> None:
        world = build_world(["C-A", "C-B"])
        workspace, track, bay, cars, outbound = world
        cars["C-A"].state = CarState.RESERVED
        step = MoveStep(MoveVerb.PULL, "C-A", track.code, "OB-1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        with self.assertRaises(ResourceBusyError):
            execute_step(workspace, run, step)
        self.assertEqual(track.stack, ["C-A", "C-B"])

    def test_pull_appends_in_planned_order(self) -> None:
        world = build_world(["C-A", "C-B"])
        workspace, track, bay, cars, outbound = world
        cars["C-B"].state = CarState.RESERVED
        step = MoveStep(MoveVerb.PULL, "C-B", track.code, "OB-1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        execute_step(workspace, run, step)
        self.assertEqual(outbound.assembled_car_codes, ["C-B"])
        self.assertEqual(cars["C-B"].state, CarState.ASSEMBLED)


class ReturnPreconditionTest(unittest.TestCase):
    def _buffered_world(self) -> tuple:
        world = build_world(["C-A", "C-B"])
        workspace, track, bay, cars, _outbound = world
        buffer_step = MoveStep(MoveVerb.BUFFER, "C-B", track.code, "X1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[buffer_step])
        execute_step(workspace, run, buffer_step)
        return workspace, track, bay, cars, run

    def test_return_requires_bay_top(self) -> None:
        workspace, track, bay, cars, _run = self._buffered_world()
        self.assertEqual(bay.stack, ["C-B"])
        # To test the bay-top guard (rather than the missing-car guard), put a
        # real second car on top of the bay and ask to return the lower one.
        other = make_car("C-OTHER")
        other.state = CarState.STANDING
        other.location = "X1"
        workspace.cars["C-OTHER"] = other
        bay.stack.append("C-OTHER")
        step = MoveStep(MoveVerb.RETURN, "C-B", "X1", track.code)
        run = PullRun(code="RUN-2", outbound_code="OB-1", transfer_code="X1", steps=[step])
        with self.assertRaises(ResourceBusyError) as caught:
            execute_step(workspace, run, step)
        self.assertIn("on top of bay", str(caught.exception))
        self.assertEqual(bay.stack, ["C-B", "C-OTHER"])

    def test_return_restores_car_on_track_top(self) -> None:
        workspace, track, bay, cars, _run = self._buffered_world()
        step = MoveStep(MoveVerb.RETURN, "C-B", "X1", track.code)
        run = PullRun(code="RUN-2", outbound_code="OB-1", transfer_code="X1", steps=[step])
        execute_step(workspace, run, step)
        self.assertEqual(track.stack, ["C-A", "C-B"])
        self.assertEqual(bay.stack, [])
        self.assertEqual(cars["C-B"].location, track.code)


class MidBatchFailureMemorySemanticsTest(unittest.TestCase):
    def test_failed_step_keeps_earlier_in_memory_moves_without_rollback(self) -> None:
        # Drive a QUEUED run: first buffer succeeds, then sabotage the stack so
        # the second buffer fails. The already-buffered car must remain in the
        # bay in memory (the service layer is responsible for discarding this
        # workspace instead of committing it).
        world = build_world(["C-A", "C-B1", "C-B2"])
        workspace, track, bay, cars, outbound = world
        run = planned_run(world, outbound, ["C-A"])
        steps = run.steps
        self.assertEqual(str(steps[0].verb), "BUFFER")
        execute_step(workspace, run, steps[0])
        self.assertEqual(bay.stack, ["C-B2"])
        # Simulate external state divergence: top blocker is gone on disk/track.
        track.stack.remove("C-B1")
        with self.assertRaises(ResourceBusyError):
            execute_step(workspace, run, steps[1])
        # Earlier in-memory mutation is NOT rolled back automatically.
        self.assertEqual(bay.stack, ["C-B2"])
        self.assertEqual(cars["C-B2"].location, "X1")
        # current_step tracking is owned by the service, not execute_step.
        self.assertEqual(run.state, RunState.QUEUED)

    def test_finished_run_rejects_further_execution(self) -> None:
        world = build_world(["C-A"])
        workspace, track, bay, cars, outbound = world
        run = planned_run(world, outbound, ["C-A"])
        run.state = RunState.COMPLETED
        with self.assertRaises(StateTransitionError) as caught:
            execute_step(workspace, run, run.steps[0])
        self.assertIn("finished", str(caught.exception))
        run.state = RunState.FAILED
        with self.assertRaises(StateTransitionError):
            execute_step(workspace, run, run.steps[0])

    def test_missing_car_and_missing_targets_fail_closed(self) -> None:
        world = build_world(["C-A"])
        workspace, track, bay, cars, outbound = world
        reserved_car = cars["C-A"]
        reserved_car.state = CarState.RESERVED
        step = MoveStep(MoveVerb.PULL, "C-A", track.code, "OB-1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        # Missing car.
        del workspace.cars["C-A"]
        with self.assertRaises(ResourceBusyError) as caught:
            execute_step(workspace, run, step)
        self.assertIn("missing car", str(caught.exception))
        # Missing outbound target.
        workspace.cars["C-A"] = reserved_car
        del workspace.outbounds["OB-1"]
        with self.assertRaises(ResourceBusyError) as caught:
            execute_step(workspace, run, step)
        self.assertIn("missing outbound", str(caught.exception))
        # Missing source track.
        workspace.outbounds["OB-1"] = outbound
        del workspace.tracks[track.code]
        with self.assertRaises(ResourceBusyError):
            execute_step(workspace, run, step)


class MaintenanceSourceExecutorTest(unittest.TestCase):
    @unittest.expectedFailure
    def test_contract_executor_should_reject_maintenance_source(self) -> None:
        # CONTRACT GAP: spec says a maintenance track cannot be a pull source,
        # but execute_step only checks the top car, not track state.
        world = build_world(["C-A"], track_code="MAINT-1")
        workspace, track, bay, cars, outbound = world
        track.state = TrackState.MAINTENANCE
        cars["C-A"].state = CarState.RESERVED
        step = MoveStep(MoveVerb.PULL, "C-A", track.code, "OB-1")
        run = PullRun(code="RUN-1", outbound_code="OB-1", transfer_code="X1", steps=[step])
        with self.assertRaises((ResourceBusyError, StateTransitionError)):
            execute_step(workspace, run, step)


if __name__ == "__main__":
    unittest.main(verbosity=2)
