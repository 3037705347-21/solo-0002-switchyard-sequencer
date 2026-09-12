"""Classification allocation tests: destination, kind, hazard, dual capacity.

Covers ``domain.allocator.classify_intake`` and the per-track rule helpers in
``domain.rules``: destination affinity, hazard rating, allowed kinds,
maintenance/restricted receiving bans, car-count and total-length capacity,
candidate ranking, partial classification, terminal conflicts, and the
classification rollback helper.
"""

from __future__ import annotations

import unittest

from _support import make_car, make_track
from switchyard.domain.allocator import classify_intake, rollback_classification
from switchyard.domain.enums import CarState, IntakeState, TrackPurpose, TrackState
from switchyard.domain.errors import ConflictError
from switchyard.domain.intake import IntakeTrain
from switchyard.domain.rules import (
    candidate_tracks_for,
    destination_allowed,
    hazard_allowed,
    kind_allowed,
    stack_occupancy,
    track_receives_car,
)


def train(code: str, consist: list[str]) -> IntakeTrain:
    return IntakeTrain(code=code, route="R1", arrival_at="2026-09-10T09:00:00Z", consist=list(consist))


def classify(tracks: dict, cars: dict, codes: list[str], train_code: str = "INT-1"):
    intake = train(train_code, codes)
    spots = classify_intake(intake, cars, tracks)
    return intake, spots


class RuleHelperTest(unittest.TestCase):
    def test_destination_affinity_rule(self) -> None:
        dest_track = make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4")
        general = make_track("MIX-1", purpose=TrackPurpose.GENERAL)
        transfer = make_track("X1", purpose=TrackPurpose.TRANSFER)
        n4_car = make_car("C-N4-1", destination="N4")
        e7_car = make_car("C-E7-1", destination="E7")
        self.assertTrue(destination_allowed(dest_track, n4_car))
        self.assertFalse(destination_allowed(dest_track, e7_car))
        self.assertTrue(destination_allowed(general, n4_car))
        self.assertTrue(destination_allowed(general, e7_car))
        # Transfer bays never receive classification spots.
        self.assertFalse(destination_allowed(transfer, n4_car))

    def test_allowed_kinds_rule(self) -> None:
        unrestricted = make_track("T1", allowed_kinds=[])
        box_only = make_track("T2", allowed_kinds=["BOX"])
        box_car = make_car("C-1", kind="BOX")  # type: ignore[arg-type]
        tank_car = make_car("C-2", kind="TANK")  # type: ignore[arg-type]
        self.assertTrue(kind_allowed(unrestricted, box_car))
        self.assertTrue(kind_allowed(box_only, box_car))
        self.assertFalse(kind_allowed(box_only, tank_car))

    def test_hazard_rule(self) -> None:
        normal_track = make_track("T1", hazard_rated=False)
        rated_track = make_track("T2", hazard_rated=True)
        plain = make_car("C-1", danger_class="NONE")
        dangerous = make_car("C-2", danger_class="D1")
        explosive = make_car("C-3", danger_class="D2")
        self.assertTrue(hazard_allowed(normal_track, plain))
        self.assertFalse(hazard_allowed(normal_track, dangerous))
        self.assertTrue(hazard_allowed(rated_track, dangerous))
        self.assertTrue(hazard_allowed(rated_track, explosive))

    def test_reason_strings_for_each_rejection(self) -> None:
        car = make_car("C-1", destination="E7")
        cars = {"C-1": car}
        dest_track = make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4")
        self.assertIn("rejects destination", track_receives_car(dest_track, car, cars))
        kind_track = make_track("T1", allowed_kinds=["TANK"])
        self.assertIn("rejects kind", track_receives_car(kind_track, make_car("C-9", kind="BOX"), {"C-9": make_car("C-9", kind="BOX")}))
        hazard_track = make_track("T2", hazard_rated=False)
        haz_car = make_car("C-8", danger_class="D1")
        self.assertIn("not hazard rated", track_receives_car(hazard_track, haz_car, {"C-8": haz_car}))
        maint = make_track("T3", state=TrackState.MAINTENANCE)
        self.assertIn("MAINTENANCE", track_receives_car(maint, car, cars))
        restricted = make_track("T4", state=TrackState.RESTRICTED)
        self.assertIn("RESTRICTED", track_receives_car(restricted, car, cars))


class CapacityRuleTest(unittest.TestCase):
    def test_car_count_capacity_boundary(self) -> None:
        small = make_track("T1", capacity_cars=1, capacity_length_m=300)
        cars = {"C-A": make_car("C-A"), "C-B": make_car("C-B")}
        small.stack.append("C-A")
        count, length = stack_occupancy(small, cars)
        self.assertEqual((count, length), (1, 18))
        # Second car fits by length (36 <= 300) but not by count.
        self.assertIn("car capacity", track_receives_car(small, cars["C-B"], cars))

    def test_length_capacity_boundary(self) -> None:
        track = make_track("T1", capacity_cars=10, capacity_length_m=30)
        cars = {"C-BIG": make_car("C-BIG", length_m=20)}
        track.stack.append("C-BIG")
        other = make_car("C-2", length_m=11)
        cars["C-2"] = other
        self.assertIn("length capacity", track_receives_car(track, other, cars))
        # 20 + 10 == 30 must still fit (boundary is inclusive).
        exact = make_car("C-3", length_m=10)
        cars["C-3"] = exact
        self.assertIsNone(track_receives_car(track, exact, cars))

    def test_candidate_tracks_filters_incompatible(self) -> None:
        cars: dict = {}
        n4 = make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4")
        mix = make_track("MIX-1", purpose=TrackPurpose.GENERAL)
        maint = make_track("MAINT-1", state=TrackState.MAINTENANCE)
        tracks = {"N4-A": n4, "MIX-1": mix, "MAINT-1": maint}
        e7_car = make_car("C-E7-1", destination="E7")
        candidates = candidate_tracks_for(e7_car, cars, tracks.values())
        self.assertEqual([track.code for track in candidates], ["MIX-1"])


class ClassifyAllocationTest(unittest.TestCase):
    def test_destination_tracks_preferred_over_general(self) -> None:
        tracks = {
            "N4-A": make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=10, capacity_length_m=300),
            "MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL, capacity_cars=18, capacity_length_m=500),
        }
        cars = {"C-N4-1": make_car("C-N4-1", destination="N4")}
        intake, spots = classify(tracks, cars, ["C-N4-1"])
        self.assertEqual(intake.state, IntakeState.CLASSIFIED)
        self.assertEqual(spots[0].track_code, "N4-A")
        self.assertEqual(tracks["N4-A"].stack, ["C-N4-1"])
        self.assertEqual(tracks["MIX-1"].stack, [])

    def test_emptier_destination_track_wins_ranking(self) -> None:
        tracks = {
            "N4-A": make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=10, capacity_length_m=300),
            "N4-B": make_track("N4-B", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=10, capacity_length_m=300),
        }
        parked = make_car("C-N4-0", destination="N4")
        parked.state = CarState.STANDING
        parked.location = "N4-A"
        tracks["N4-A"].stack.append("C-N4-0")
        cars = {"C-N4-0": parked, "C-N4-1": make_car("C-N4-1", destination="N4")}
        _intake, spots = classify(tracks, cars, ["C-N4-1"])
        self.assertEqual(spots[0].track_code, "N4-B")

    def test_hazard_car_requires_rated_general_track(self) -> None:
        tracks = {
            "W9-A": make_track("W9-A", purpose=TrackPurpose.DESTINATION, destination="W9", capacity_cars=10, capacity_length_m=300),
            "HAZ-1": make_track("HAZ-1", purpose=TrackPurpose.GENERAL, capacity_cars=8, capacity_length_m=240, hazard_rated=True),
        }
        cars = {"C-HAZ-1": make_car("C-HAZ-1", destination="W9", danger_class="D1", kind="TANK")}  # type: ignore[arg-type]
        _intake, spots = classify(tracks, cars, ["C-HAZ-1"])
        self.assertEqual(spots[0].track_code, "HAZ-1")
        self.assertEqual(cars["C-HAZ-1"].state, CarState.STANDING)
        self.assertEqual(cars["C-HAZ-1"].location, "HAZ-1")

    def test_hazard_car_unplaced_without_rated_track(self) -> None:
        tracks = {
            "W9-A": make_track("W9-A", purpose=TrackPurpose.DESTINATION, destination="W9", capacity_cars=10, capacity_length_m=300),
            "MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL, capacity_cars=18, capacity_length_m=500),
        }
        cars = {"C-HAZ-9": make_car("C-HAZ-9", destination="W9", danger_class="D2")}
        intake, spots = classify(tracks, cars, ["C-HAZ-9"])
        self.assertEqual(spots, [])
        self.assertEqual(intake.state, IntakeState.PARTIAL)
        self.assertEqual(intake.unplaced, ["C-HAZ-9"])
        self.assertEqual(cars["C-HAZ-9"].state, CarState.RECEIVED)

    def test_length_overflow_falls_back_to_general_track(self) -> None:
        tracks = {
            "N4-A": make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=10, capacity_length_m=300),
            "MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL, capacity_cars=18, capacity_length_m=500),
        }
        cars = {
            "C-N4-1": make_car("C-N4-1", destination="N4", length_m=150),
            "C-N4-2": make_car("C-N4-2", destination="N4", length_m=150),
            "C-N4-3": make_car("C-N4-3", destination="N4", length_m=150),
        }
        intake, spots = classify(tracks, cars, ["C-N4-1", "C-N4-2", "C-N4-3"])
        placement = {spot.car_code: spot.track_code for spot in spots}
        self.assertEqual(placement["C-N4-1"], "N4-A")
        self.assertEqual(placement["C-N4-2"], "N4-A")
        # 450 > 300 on the destination track; general track absorbs the third.
        self.assertEqual(placement["C-N4-3"], "MIX-1")
        self.assertEqual(intake.state, IntakeState.CLASSIFIED)
        _count, length = stack_occupancy(tracks["N4-A"], cars)
        self.assertEqual(length, 300)

    def test_count_overflow_falls_back(self) -> None:
        tracks = {
            "N4-A": make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=2, capacity_length_m=500),
            "MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL, capacity_cars=18, capacity_length_m=500),
        }
        cars = {f"C-N4-{i}": make_car(f"C-N4-{i}", destination="N4", length_m=10) for i in range(1, 4)}
        intake, spots = classify(tracks, cars, ["C-N4-1", "C-N4-2", "C-N4-3"])
        self.assertEqual(len(tracks["N4-A"].stack), 2)
        self.assertEqual(tracks["MIX-1"].stack, ["C-N4-3"])
        self.assertEqual(intake.state, IntakeState.CLASSIFIED)

    def test_kind_restricted_track_rejects_car(self) -> None:
        tracks = {"BOX-1": make_track("BOX-1", purpose=TrackPurpose.GENERAL, allowed_kinds=["BOX"])}
        cars = {"C-T-1": make_car("C-T-1", kind="TANK")}  # type: ignore[arg-type]
        intake, spots = classify(tracks, cars, ["C-T-1"])
        self.assertEqual(spots, [])
        self.assertEqual(intake.unplaced, ["C-T-1"])
        self.assertEqual(intake.state, IntakeState.PARTIAL)

    def test_maintenance_track_never_receives(self) -> None:
        tracks = {"MAINT-1": make_track("MAINT-1", state=TrackState.MAINTENANCE)}
        cars = {"C-1": make_car("C-1")}
        intake, spots = classify(tracks, cars, ["C-1"])
        self.assertEqual(spots, [])
        self.assertEqual(intake.state, IntakeState.PARTIAL)
        self.assertEqual(tracks["MAINT-1"].stack, [])

    def test_transfer_bay_is_never_a_classification_target(self) -> None:
        tracks = {
            "X1": make_track("X1", purpose=TrackPurpose.TRANSFER),
            "MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL),
        }
        cars = {"C-1": make_car("C-1")}
        _intake, spots = classify(tracks, cars, ["C-1"])
        self.assertEqual(spots[0].track_code, "MIX-1")

    def test_partial_classification_keeps_placed_and_lists_unplaced(self) -> None:
        tracks = {
            "N4-A": make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=10, capacity_length_m=300),
        }
        cars = {
            "C-N4-1": make_car("C-N4-1", destination="N4"),
            "C-HAZ-1": make_car("C-HAZ-1", destination="N4", danger_class="D1"),
        }
        intake, spots = classify(tracks, cars, ["C-N4-1", "C-HAZ-1"])
        self.assertEqual(intake.state, IntakeState.PARTIAL)
        self.assertEqual([spot.car_code for spot in spots], ["C-N4-1"])
        self.assertEqual(intake.unplaced, ["C-HAZ-1"])
        self.assertEqual(cars["C-N4-1"].state, CarState.STANDING)
        self.assertEqual(cars["C-HAZ-1"].state, CarState.RECEIVED)
        self.assertEqual(spots[0].index, 0)

    def test_unknown_and_non_received_consist_codes_go_unplaced(self) -> None:
        tracks = {"MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL)}
        standing = make_car("C-2")
        standing.state = CarState.STANDING
        standing.location = "MIX-1"
        tracks["MIX-1"].stack.append("C-2")
        cars = {"C-2": standing}
        intake, spots = classify(tracks, cars, ["C-1", "C-2"])
        self.assertEqual(spots, [])
        self.assertEqual(intake.unplaced, ["C-1", "C-2"])
        # The already-standing car is not double-spotted.
        self.assertEqual(tracks["MIX-1"].stack, ["C-2"])

    def test_terminal_train_cannot_be_classified_again(self) -> None:
        tracks = {"MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL)}
        cars = {"C-1": make_car("C-1")}
        intake, _spots = classify(tracks, cars, ["C-1"])
        with self.assertRaises(ConflictError):
            classify_intake(intake, cars, tracks)

    def test_lifo_stack_order_matches_manifest_scan(self) -> None:
        tracks = {"MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL, capacity_cars=10, capacity_length_m=500)}
        cars = {f"C-{i}": make_car(f"C-{i}", length_m=10) for i in range(1, 4)}
        intake, spots = classify(tracks, cars, ["C-1", "C-2", "C-3"])
        self.assertEqual(tracks["MIX-1"].stack, ["C-1", "C-2", "C-3"])
        self.assertEqual([spot.index for spot in spots], [0, 1, 2])
        self.assertEqual(tracks["MIX-1"].top_code(), "C-3")

    @unittest.expectedFailure
    def test_contract_retry_of_partial_train_should_reach_classified(self) -> None:
        # CONTRACT GAP (PROJECT_SPEC.md workflow 1: open -> partial -> classified):
        # A retry after yard capacity changes should only reconsider unplaced
        # cars. Actual implementation marks every already-standing consist car
        # as unplaced, so the train can never reach CLASSIFIED.
        tracks = {
            "N4-A": make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=10, capacity_length_m=300),
        }
        cars = {
            "C-N4-1": make_car("C-N4-1", destination="N4"),
            "C-HAZ-1": make_car("C-HAZ-1", destination="N4", danger_class="D1"),
        }
        intake, _spots = classify(tracks, cars, ["C-N4-1", "C-HAZ-1"])
        self.assertEqual(intake.state, IntakeState.PARTIAL)
        # Simulate a hazard-rated track becoming available, then retry.
        tracks["HAZ-1"] = make_track("HAZ-1", purpose=TrackPurpose.GENERAL, hazard_rated=True)
        classify_intake(intake, cars, tracks)
        self.assertEqual(intake.state, IntakeState.CLASSIFIED)
        self.assertEqual(intake.unplaced, [])


class RollbackClassificationTest(unittest.TestCase):
    def test_rollback_pulls_cars_back_to_received(self) -> None:
        tracks = {
            "N4-A": make_track("N4-A", purpose=TrackPurpose.DESTINATION, destination="N4", capacity_cars=10, capacity_length_m=300),
            "MIX-1": make_track("MIX-1", purpose=TrackPurpose.GENERAL, capacity_cars=18, capacity_length_m=500),
        }
        cars = {
            "C-N4-1": make_car("C-N4-1", destination="N4"),
            "C-E7-1": make_car("C-E7-1", destination="E7"),
        }
        intake, spots = classify(tracks, cars, ["C-N4-1", "C-E7-1"])
        self.assertEqual(intake.state, IntakeState.CLASSIFIED)
        rollback_classification(intake, cars, tracks)
        self.assertEqual(intake.state, IntakeState.OPEN)
        self.assertEqual(set(intake.unplaced), {"C-N4-1", "C-E7-1"})
        self.assertEqual(tracks["N4-A"].stack, [])
        self.assertEqual(tracks["MIX-1"].stack, [])
        for car in cars.values():
            self.assertEqual(car.state, CarState.RECEIVED)
            self.assertEqual(car.location, "INTAKE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
