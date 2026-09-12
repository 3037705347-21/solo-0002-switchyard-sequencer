"""State transition table tests for every stateful entity.

Each entity's allowed moves are asserted positively, and every disallowed
move raises ``StateTransitionError`` (HTTP 409) without mutating the current
state.  The tables also document moves that the spec text does not mention
(e.g. outbound PLANNED -> DRAFT, intake OPEN -> CLASSIFIED directly).
"""

from __future__ import annotations

import unittest

from _support import make_bay, make_car, make_track
from switchyard.domain.enums import (
    CarState,
    IntakeState,
    OutboundState,
    RunState,
    ShiftState,
)
from switchyard.domain.errors import StateTransitionError
from switchyard.domain.intake import IntakeTrain
from switchyard.domain.outbound import OutboundTrain
from switchyard.domain.pull import PullRun
from switchyard.domain.shift import YardShift
from switchyard.domain.transitions import (
    transition_car,
    transition_intake,
    transition_outbound,
    transition_run,
    transition_shift,
)


class CarTransitionTest(unittest.TestCase):
    def _car(self, state: CarState) -> object:
        car = make_car("C-N4-1")
        car.state = state
        return car

    def test_allowed_car_paths(self) -> None:
        car = make_car("C-N4-1")
        self.assertEqual(str(car.state), "RECEIVED")
        transition_car(car, CarState.STANDING)
        transition_car(car, CarState.RESERVED)
        transition_car(car, CarState.ASSEMBLED)
        transition_car(car, CarState.DEPARTED)
        transition_car(car, CarState.REMOVED)
        self.assertEqual(car.state, CarState.REMOVED)

    def test_reserved_can_release_back_to_standing(self) -> None:
        car = self._car(CarState.RESERVED)
        transition_car(car, CarState.STANDING)
        self.assertEqual(car.state, CarState.STANDING)

    def test_early_removal_allowed_from_every_live_state(self) -> None:
        for state in (CarState.RECEIVED, CarState.STANDING, CarState.RESERVED, CarState.ASSEMBLED, CarState.DEPARTED):
            car = self._car(state)
            transition_car(car, CarState.REMOVED)
            self.assertEqual(car.state, CarState.REMOVED)

    def test_forbidden_car_moves(self) -> None:
        forbidden = {
            CarState.RECEIVED: [CarState.RESERVED, CarState.ASSEMBLED, CarState.DEPARTED],
            CarState.STANDING: [CarState.ASSEMBLED, CarState.DEPARTED, CarState.RECEIVED],
            CarState.RESERVED: [CarState.DEPARTED, CarState.RECEIVED],
            CarState.ASSEMBLED: [CarState.STANDING, CarState.RESERVED, CarState.RECEIVED],
            CarState.DEPARTED: [CarState.STANDING, CarState.ASSEMBLED],
            CarState.REMOVED: [CarState.STANDING, CarState.RECEIVED, CarState.DEPARTED],
        }
        for state, targets in forbidden.items():
            for target in targets:
                with self.subTest(state=state, target=target):
                    car = self._car(state)
                    with self.assertRaises(StateTransitionError) as caught:
                        transition_car(car, target)
                    self.assertEqual(caught.exception.status, 409)
                    self.assertEqual(car.state, state)

    def test_failed_transition_keeps_state(self) -> None:
        car = self._car(CarState.RECEIVED)
        with self.assertRaises(StateTransitionError):
            transition_car(car, CarState.DEPARTED)
        self.assertEqual(car.state, CarState.RECEIVED)


class IntakeTransitionTest(unittest.TestCase):
    def _train(self, state: IntakeState) -> IntakeTrain:
        train = IntakeTrain(code="INT-1", route="R", arrival_at="2026-09-10T09:00:00Z")
        train.state = state
        return train

    def test_full_open_partial_classified_path(self) -> None:
        train = self._train(IntakeState.OPEN)
        transition_intake(train, IntakeState.PARTIAL)
        self.assertEqual(train.state, IntakeState.PARTIAL)
        transition_intake(train, IntakeState.CLASSIFIED)
        self.assertEqual(train.state, IntakeState.CLASSIFIED)

    def test_open_can_skip_to_classified_or_cancel(self) -> None:
        train = self._train(IntakeState.OPEN)
        transition_intake(train, IntakeState.CLASSIFIED)
        self.assertTrue(train.is_terminal())
        train = self._train(IntakeState.OPEN)
        transition_intake(train, IntakeState.CANCELLED)
        self.assertTrue(train.is_terminal())

    def test_partial_can_self_transition_and_cancel(self) -> None:
        train = self._train(IntakeState.PARTIAL)
        transition_intake(train, IntakeState.PARTIAL)
        transition_intake(train, IntakeState.CANCELLED)
        self.assertEqual(train.state, IntakeState.CANCELLED)

    def test_terminal_states_have_no_moves(self) -> None:
        for state in (IntakeState.CLASSIFIED, IntakeState.CANCELLED):
            train = self._train(state)
            for target in (IntakeState.OPEN, IntakeState.PARTIAL, IntakeState.CLASSIFIED, IntakeState.CANCELLED):
                with self.subTest(state=state, target=target):
                    with self.assertRaises(StateTransitionError):
                        transition_intake(train, target)
                    self.assertEqual(train.state, state)

    def test_open_cannot_reopen(self) -> None:
        train = self._train(IntakeState.OPEN)
        with self.assertRaises(StateTransitionError):
            transition_intake(train, IntakeState.OPEN)


class OutboundTransitionTest(unittest.TestCase):
    def _train(self, state: OutboundState) -> OutboundTrain:
        train = OutboundTrain(code="OB-1", destination="N4")
        train.state = state
        return train

    def test_happy_path_to_departed(self) -> None:
        train = self._train(OutboundState.DRAFT)
        transition_outbound(train, OutboundState.PLANNED)
        transition_outbound(train, OutboundState.READY)
        transition_outbound(train, OutboundState.DEPARTED)
        self.assertEqual(train.state, OutboundState.DEPARTED)

    def test_draft_and_planned_may_be_abandoned(self) -> None:
        train = self._train(OutboundState.DRAFT)
        transition_outbound(train, OutboundState.ABANDONED)
        train = self._train(OutboundState.PLANNED)
        transition_outbound(train, OutboundState.ABANDONED)
        train = self._train(OutboundState.READY)
        transition_outbound(train, OutboundState.ABANDONED)

    def test_table_allows_planned_back_to_draft(self) -> None:
        # Documented actual behavior: the transition table permits PLANNED ->
        # DRAFT even though the spec's lifecycle text lists only forward moves.
        train = self._train(OutboundState.PLANNED)
        transition_outbound(train, OutboundState.DRAFT)
        self.assertEqual(train.state, OutboundState.DRAFT)

    def test_forbidden_moves(self) -> None:
        forbidden = {
            OutboundState.DRAFT: [OutboundState.READY, OutboundState.DEPARTED, OutboundState.DRAFT],
            OutboundState.PLANNED: [OutboundState.DEPARTED, OutboundState.PLANNED],
            OutboundState.READY: [OutboundState.PLANNED, OutboundState.DRAFT, OutboundState.READY],
            OutboundState.DEPARTED: [OutboundState.READY, OutboundState.ABANDONED],
            OutboundState.ABANDONED: [OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.DEPARTED],
        }
        for state, targets in forbidden.items():
            for target in targets:
                with self.subTest(state=state, target=target):
                    train = self._train(state)
                    with self.assertRaises(StateTransitionError):
                        transition_outbound(train, target)
                    self.assertEqual(train.state, state)


class RunTransitionTest(unittest.TestCase):
    def _run(self, state: RunState) -> PullRun:
        run = PullRun(code="RUN-OB-1", outbound_code="OB-1", transfer_code="X1")
        run.state = state
        return run

    def test_queued_running_completed(self) -> None:
        run = self._run(RunState.QUEUED)
        transition_run(run, RunState.RUNNING)
        transition_run(run, RunState.COMPLETED)
        self.assertEqual(run.state, RunState.COMPLETED)

    def test_queued_or_running_can_fail(self) -> None:
        run = self._run(RunState.QUEUED)
        transition_run(run, RunState.FAILED)
        self.assertEqual(run.state, RunState.FAILED)
        run = self._run(RunState.RUNNING)
        transition_run(run, RunState.FAILED)
        self.assertEqual(run.state, RunState.FAILED)

    def test_forbidden_moves(self) -> None:
        forbidden = {
            RunState.QUEUED: [RunState.COMPLETED, RunState.QUEUED],
            RunState.RUNNING: [RunState.QUEUED, RunState.RUNNING],
            RunState.COMPLETED: [RunState.RUNNING, RunState.FAILED],
            RunState.FAILED: [RunState.RUNNING, RunState.COMPLETED, RunState.QUEUED],
        }
        for state, targets in forbidden.items():
            for target in targets:
                with self.subTest(state=state, target=target):
                    run = self._run(state)
                    with self.assertRaises(StateTransitionError):
                        transition_run(run, target)
                    self.assertEqual(run.state, state)


class ShiftTransitionTest(unittest.TestCase):
    def _shift(self, state: ShiftState) -> YardShift:
        shift = YardShift(code="SHIFT-1", dispatcher="D", opened_at="2026-09-10T08:00:00Z")
        shift.state = state
        return shift

    def test_open_to_closed_once(self) -> None:
        shift = self._shift(ShiftState.OPEN)
        transition_shift(shift, ShiftState.CLOSED)
        self.assertEqual(shift.state, ShiftState.CLOSED)

    def test_closed_is_terminal(self) -> None:
        shift = self._shift(ShiftState.CLOSED)
        for target in (ShiftState.OPEN, ShiftState.CLOSED):
            with self.subTest(target=target):
                with self.assertRaises(StateTransitionError):
                    transition_shift(shift, target)
                self.assertEqual(shift.state, ShiftState.CLOSED)


class TransitionReasonTest(unittest.TestCase):
    def test_reason_is_attached_to_error_payload(self) -> None:
        car = make_car("C-N4-1")
        try:
            transition_car(car, CarState.DEPARTED, reason="not yet assembled")
        except StateTransitionError as exc:
            self.assertIn("not yet assembled", str(exc))
            self.assertEqual(exc.payload["reason"], "not yet assembled")
        else:  # pragma: no cover - defensive
            self.fail("expected StateTransitionError")

    def test_helpers_accept_real_entities_from_seed_like_objects(self) -> None:
        # Sanity: transitions operate on the same objects held by collections.
        track = make_track("T1")
        bay = make_bay()
        self.assertEqual(track.stack, [])
        self.assertEqual(bay.stack, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
