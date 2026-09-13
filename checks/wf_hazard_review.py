"""Workflow check: hazardous goods compliance review across yard states."""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any, Iterator

from support import ApiClient, RunningServer

from switchyard.domain.car import FreightCar
from switchyard.domain.enums import CarKind, CarState
from switchyard.storage.codec import encode_workspace
from switchyard.storage.seed import build_seed_workspace


@contextlib.contextmanager
def yard_server(data_dir: Path | None = None) -> Iterator[ApiClient]:
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        yield server.api
    finally:
        server.stop()


def make_car(
    code: str,
    kind: str,
    destination: str,
    loaded: bool,
    length_m: int,
    danger_class: str,
    track_code: str,
    note: str = "",
) -> FreightCar:
    return FreightCar(
        code=code,
        kind=CarKind.parse(kind),
        destination=destination,
        loaded=loaded,
        length_m=length_m,
        danger_class=danger_class,
        state=CarState.STANDING,
        location=track_code,
        note=note,
    )


def seed_state(data_dir: Path, cars: list[FreightCar], stacks: dict[str, list[str]]) -> None:
    workspace = build_seed_workspace()
    for track_code, stack in stacks.items():
        workspace.tracks[track_code].stack = list(stack)
    for car in cars:
        workspace.cars[car.code] = car
    payload = encode_workspace(workspace)
    path = data_dir / "yard-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def find_one(review: dict[str, Any], rule: str, car_code: str) -> dict[str, Any]:
    matches = [
        item
        for item in review["findings"]
        if item["rule"] == rule and item["car_code"] == car_code
    ]
    assert len(matches) == 1, f"expected one {rule} finding for {car_code}, got {len(matches)}"
    return matches[0]


def assert_finding_shape(review: dict[str, Any]) -> None:
    for item in review["findings"]:
        assert item["car_code"], f"finding misses a car: {item}"
        assert item["track_code"], f"finding misses a track: {item}"
        assert item["severity"] in {"BLOCKING", "WATCH"}, f"bad severity: {item}"
        assert item["message"], f"finding misses a message: {item}"
        assert item["evidence"]["basis"], f"finding misses a basis: {item}"


def scenario_no_hazard() -> None:
    with yard_server() as api:
        api.expect_ok(
            "POST",
            "/api/shifts",
            {"code": "SHIFT-H1", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
        )
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {
                "code": "INT-H1",
                "route": "RAIL-H1",
                "arrival_at": "2026-09-13T09:00:00Z",
                "cars": [
                    {
                        "code": "C-HAZ-901",
                        "kind": "TANK",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 20,
                        "danger_class": "NONE",
                        "note": "stencil says flammable; certified non-hazardous",
                    },
                    {
                        "code": "C-N4-902",
                        "kind": "BOX",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 18,
                        "danger_class": "NONE",
                        "note": "D2 spare parts, paperwork only",
                    },
                    {
                        "code": "C-E7-903",
                        "kind": "FLAT",
                        "destination": "E7",
                        "loaded": False,
                        "length_m": 16,
                        "danger_class": "NONE",
                    },
                ],
            },
        )
        classified = api.expect_ok("POST", "/api/intake-trains/INT-H1/classify", {})
        assert classified["unplaced"] == []
        review = api.expect_ok("GET", "/api/hazard-review")
        assert review["summary"]["hazard_cars_on_tracks"] == 0
        assert review["summary"]["finding_count"] == 0
        assert review["findings"] == []


def scenario_compliant_track() -> None:
    with yard_server() as api:
        api.expect_ok(
            "POST",
            "/api/shifts",
            {"code": "SHIFT-H2", "dispatcher": "KE", "opened_at": "2026-09-13T08:00:00Z"},
        )
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {
                "code": "INT-H2",
                "route": "RAIL-H2",
                "arrival_at": "2026-09-13T09:10:00Z",
                "cars": [
                    {
                        "code": "C-HZ-101",
                        "kind": "TANK",
                        "destination": "W9",
                        "loaded": True,
                        "length_m": 22,
                        "danger_class": "D1",
                    },
                    {
                        "code": "C-HZ-102",
                        "kind": "TANK",
                        "destination": "W9",
                        "loaded": False,
                        "length_m": 22,
                        "danger_class": "D1",
                    },
                    {
                        "code": "C-W9-103",
                        "kind": "BOX",
                        "destination": "W9",
                        "loaded": True,
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                ],
            },
        )
        classified = api.expect_ok("POST", "/api/intake-trains/INT-H2/classify", {})
        tracks = {item["car_code"]: item["track_code"] for item in classified["spots"]}
        assert tracks["C-HZ-101"] == "HAZ-1"
        assert tracks["C-HZ-102"] == "HAZ-1"
        yard_before = api.expect_ok("GET", "/api/yard")
        first = api.expect_ok("GET", "/api/hazard-review")
        second = api.expect_ok("GET", "/api/hazard-review")
        assert first == second, "repeated reviews over one state must be identical"
        assert first["summary"]["hazard_cars_on_tracks"] == 2
        assert first["summary"]["finding_count"] == 0
        assert first["findings"] == []
        yard_after = api.expect_ok("GET", "/api/yard")
        assert yard_after["metrics"]["version"] == yard_before["metrics"]["version"]


def scenario_unrated_track() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-hazard-check-") as tmp:
        data_dir = Path(tmp) / "data"
        cars = [
            make_car("C-N4-501", "TANK", "N4", True, 22, "D1", "N4-A"),
            make_car("C-N4-502", "BOX", "N4", True, 18, "NONE", "N4-A"),
            make_car("C-E7-501", "TANK", "S2", False, 20, "D2", "E7-A"),
        ]
        stacks = {"N4-A": ["C-N4-501", "C-N4-502"], "E7-A": ["C-E7-501"]}
        seed_state(data_dir, cars, stacks)
        with yard_server(data_dir) as api:
            review = api.expect_ok("GET", "/api/hazard-review")
            summary = review["summary"]
            assert summary["hazard_cars_on_tracks"] == 2
            assert summary["finding_count"] == 4
            assert summary["blocking_count"] == 2
            assert summary["watch_count"] == 2
            assert_finding_shape(review)

            loaded = find_one(review, "HAZARD_ON_UNRATED_TRACK", "C-N4-501")
            assert loaded["severity"] == "BLOCKING"
            assert loaded["track_code"] == "N4-A"
            assert loaded["evidence"]["car_danger_class"] == "D1"
            assert loaded["evidence"]["car_loaded"] is True
            assert loaded["evidence"]["track_hazard_rated"] is False

            empty = find_one(review, "HAZARD_ON_UNRATED_TRACK", "C-E7-501")
            assert empty["severity"] == "WATCH"
            assert empty["track_code"] == "E7-A"
            assert empty["evidence"]["car_loaded"] is False

            mismatch = find_one(review, "HAZARD_DESTINATION_MISMATCH", "C-E7-501")
            assert mismatch["severity"] == "BLOCKING"
            assert mismatch["track_code"] == "E7-A"
            assert mismatch["evidence"]["car_destination"] == "S2"
            assert mismatch["evidence"]["track_destination"] == "E7"

            neighbor = find_one(review, "HAZARD_NEIGHBOR_NONHAZARD", "C-N4-501")
            assert neighbor["severity"] == "WATCH"
            assert neighbor["track_code"] == "N4-A"
            assert neighbor["evidence"]["neighbor_code"] == "C-N4-502"
            assert neighbor["evidence"]["neighbor_danger_class"] == "NONE"

            subjects = {item["car_code"] for item in review["findings"]}
            assert "C-N4-502" not in subjects, "non-hazardous car must never be flagged"

            yard_before = api.expect_ok("GET", "/api/yard")
            again = api.expect_ok("GET", "/api/hazard-review")
            assert again == review, "repeated reviews over one state must be identical"
            yard_after = api.expect_ok("GET", "/api/yard")
            assert yard_after["metrics"]["version"] == yard_before["metrics"]["version"]
            track_metrics = {item["code"]: item for item in yard_after["metrics"]["track_metrics"]}
            assert track_metrics["N4-A"]["cars"] == 2
            assert track_metrics["N4-A"]["top_car"] == "C-N4-502"


def scenario_mixed_tracks() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-hazard-check-") as tmp:
        data_dir = Path(tmp) / "data"
        cars = [
            make_car("C-MIX-01", "BOX", "N4", True, 18, "NONE", "MIX-1"),
            make_car("C-MIX-02", "TANK", "E7", True, 22, "D1", "MIX-1"),
            make_car("C-MIX-03", "HOPPER", "S2", False, 20, "NONE", "MIX-1"),
            make_car("C-HZ-01", "TANK", "W9", True, 22, "D1", "HAZ-1"),
            make_car("C-HZ-02", "TANK", "W9", True, 22, "D2", "HAZ-1"),
            make_car("C-HZ-03", "TANK", "W9", False, 22, "D2", "HAZ-1"),
        ]
        stacks = {
            "MIX-1": ["C-MIX-01", "C-MIX-02", "C-MIX-03"],
            "HAZ-1": ["C-HZ-01", "C-HZ-02", "C-HZ-03"],
        }
        seed_state(data_dir, cars, stacks)
        with yard_server(data_dir) as api:
            review = api.expect_ok("GET", "/api/hazard-review")
            summary = review["summary"]
            assert summary["hazard_cars_on_tracks"] == 4
            assert summary["finding_count"] == 4
            assert summary["blocking_count"] == 1
            assert summary["watch_count"] == 3
            assert_finding_shape(review)

            unrated = find_one(review, "HAZARD_ON_UNRATED_TRACK", "C-MIX-02")
            assert unrated["severity"] == "BLOCKING"
            assert unrated["track_code"] == "MIX-1"

            neighbors = [
                item
                for item in review["findings"]
                if item["rule"] == "HAZARD_NEIGHBOR_NONHAZARD" and item["car_code"] == "C-MIX-02"
            ]
            assert len(neighbors) == 2
            assert {item["evidence"]["neighbor_code"] for item in neighbors} == {"C-MIX-01", "C-MIX-03"}
            assert all(item["severity"] == "WATCH" for item in neighbors)

            mixed = find_one(review, "HAZARD_CLASS_MIX_NEIGHBOR", "C-HZ-02")
            assert mixed["severity"] == "WATCH"
            assert mixed["track_code"] == "HAZ-1"
            assert mixed["evidence"]["car_danger_class"] == "D2"
            assert mixed["evidence"]["neighbor_code"] == "C-HZ-01"
            assert mixed["evidence"]["neighbor_danger_class"] == "D1"
            assert mixed["evidence"]["track_hazard_rated"] is True

            subjects = {item["car_code"] for item in review["findings"]}
            assert subjects == {"C-MIX-02", "C-HZ-02"}
            assert "C-HZ-03" not in subjects, "same-class neighbor must not be flagged"

            again = api.expect_ok("GET", "/api/hazard-review")
            assert again == review, "repeated reviews over one state must be identical"


def main() -> int:
    scenarios = [
        ("no_hazard", scenario_no_hazard),
        ("compliant_track", scenario_compliant_track),
        ("unrated_track", scenario_unrated_track),
        ("mixed_tracks", scenario_mixed_tracks),
    ]
    for name, scenario in scenarios:
        scenario()
        print(f"OK wf_hazard_review/{name}")
    print("OK wf_hazard_review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
