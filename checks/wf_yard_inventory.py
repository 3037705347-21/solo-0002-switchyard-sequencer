"""Workflow check: yard inventory detail across a buffered, mid-run, restarted yard.

Builds a scenario where the sequencer has produced BUFFER actions that have not
been executed yet, then advances the run partway so all four physical locations
are occupied at once (track stack, X1 buffer, reserved-but-unpulled car, and an
assembled outbound car). Every stage asserts that:

* the inventory detail lists full stacks bottom-to-top with no car number
  appearing in two containers;
* the inventory bucket totals partition the whole car registry and match the
  pre-existing ``metrics`` summary counts;
* each car's individual state/location in the persisted state file matches the
  inventory entry describing it, including after a full service restart.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from support import ApiClient, RunningServer

N4_CARS = ["C-A-51", "C-B-51", "C-C-51", "C-D-51"]


def _track(inventory: dict[str, Any], code: str) -> dict[str, Any]:
    return next(item for item in inventory["tracks"] if item["code"] == code)


def _bay(inventory: dict[str, Any], code: str) -> dict[str, Any]:
    return next(item for item in inventory["transfer_bays"] if item["code"] == code)


def _outbound(inventory: dict[str, Any], code: str) -> dict[str, Any]:
    return next(item for item in inventory["outbound_trains"] if item["code"] == code)


def _placed_codes(inventory: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for track in inventory["tracks"]:
        codes.extend(car["code"] for car in track["cars"])
    for bay in inventory["transfer_bays"]:
        codes.extend(car["code"] for car in bay["cars"])
    for outbound in inventory["outbound_trains"]:
        codes.extend(car["code"] for car in outbound["assembled_cars"])
    return codes


def _entry_index(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for track in inventory["tracks"]:
        for car in track["cars"]:
            index[car["code"]] = {**car, "location": track["code"], "location_kind": "track"}
    for bay in inventory["transfer_bays"]:
        for car in bay["cars"]:
            index[car["code"]] = {**car, "location": bay["code"], "location_kind": "buffer"}
    for outbound in inventory["outbound_trains"]:
        for car in outbound["assembled_cars"]:
            index[car["code"]] = {**car, "location": outbound["code"], "location_kind": "outbound"}
    return index


def _assert_partition(yard: dict[str, Any], expected_total: int) -> dict[str, Any]:
    inventory = yard["inventory"]
    assert inventory["total_cars"] == expected_total
    buckets = inventory["buckets"]
    assert sum(buckets.values()) == expected_total, buckets
    codes = _placed_codes(inventory)
    assert len(codes) == len(set(codes)), f"car counted in two locations: {codes}"
    assert inventory["anomalies"] == [], inventory["anomalies"]
    return inventory


def _seed(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-05", "dispatcher": "NUNES", "opened_at": "2026-09-13T08:00:00Z"},
    )
    cars = [
        {
            "code": code,
            "kind": "BOX",
            "destination": "N4",
            "loaded": True,
            "length_m": 18,
            "danger_class": "NONE",
        }
        for code in N4_CARS
    ]
    cars.append(
        {
            "code": "C-S2-51",
            "kind": "HOPPER",
            "destination": "S2",
            "loaded": False,
            "length_m": 20,
            "danger_class": "NONE",
        }
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {"code": "INT-51", "route": "RAIL-51", "arrival_at": "2026-09-13T09:00:00Z", "cars": cars},
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-51/classify", {})
    spots = {spot["car_code"]: spot["track_code"] for spot in classified["spots"]}
    assert spots == {code: "N4-A" for code in N4_CARS} | {"C-S2-51": "S2-A"}
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-51", "destination": "N4", "car_codes": ["C-D-51", "C-A-51"]},
    )


def run(api: ApiClient, data_dir: Path) -> str:
    _seed(api)

    # --- planned run with BUFFER actions that have not been executed yet ---
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-51/sequencer",
        {"transfer_code": "X1"},
    )
    run_code = sequenced["pull_run"]["code"]
    verbs = [step["verb"] for step in sequenced["pull_run"]["steps"]]
    assert verbs == ["PULL", "BUFFER", "BUFFER", "PULL", "RETURN", "RETURN"], verbs

    yard = api.expect_ok("GET", "/api/yard")
    inventory = _assert_partition(yard, 5)

    n4 = _track(inventory, "N4-A")
    assert n4["order"] == "bottom-to-top"
    assert [car["code"] for car in n4["cars"]] == N4_CARS
    assert n4["bottom_car"] == "C-A-51" and n4["top_car"] == "C-D-51"
    positions = {car["code"]: (car["from_bottom"], car["from_top"]) for car in n4["cars"]}
    assert positions["C-A-51"] == (0, 3) and positions["C-D-51"] == (3, 0)
    reserved = {car["code"]: car for car in n4["cars"] if car["reserved"]}
    assert set(reserved) == {"C-A-51", "C-D-51"}
    assert all(car["reserved_for"] == "OB-51" for car in reserved.values())
    assert all(car["state"] == "RESERVED" for car in reserved.values())
    assert not any(car["reserved"] for car in n4["cars"] if car["code"] in {"C-B-51", "C-C-51"})

    x1 = _bay(inventory, "X1")
    assert x1["car_count"] == 0 and x1["cars"] == [] and x1["top_car"] is None

    outbound = _outbound(inventory, "OB-51")
    assert outbound["state"] == "PLANNED"
    assert outbound["assembled_count"] == 0
    assert outbound["pending_count"] == 2
    pending = {car["code"]: car for car in outbound["pending_cars"]}
    assert set(pending) == {"C-A-51", "C-D-51"}
    assert all(car["location_kind"] == "track" and car["location"] == "N4-A" for car in pending.values())

    assert inventory["buckets"] == {
        "on_tracks": 5,
        "in_buffers": 0,
        "assembled": 0,
        "pending_intake": 0,
        "departed": 0,
        "removed": 0,
    }
    # The pre-existing statistical fields and entry points are untouched.
    counts = yard["metrics"]["car_state_counts"]
    assert counts["reserved"] == 2 and counts["standing"] == 3
    n4_metrics = next(item for item in yard["metrics"]["track_metrics"] if item["code"] == "N4-A")
    assert n4_metrics["cars"] == 4 and n4_metrics["top_car"] == "C-D-51"

    # --- advance PULL D, BUFFER C, BUFFER B: every category now occupied ---
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 3})
    assert advanced["completed"] is False
    yard = api.expect_ok("GET", "/api/yard")
    inventory = _assert_partition(yard, 5)

    n4 = _track(inventory, "N4-A")
    assert [car["code"] for car in n4["cars"]] == ["C-A-51"]
    assert n4["cars"][0]["reserved"] is True and n4["cars"][0]["state"] == "RESERVED"
    x1 = _bay(inventory, "X1")
    assert [car["code"] for car in x1["cars"]] == ["C-C-51", "C-B-51"]
    assert x1["bottom_car"] == "C-C-51" and x1["top_car"] == "C-B-51"
    assert {car["state"] for car in x1["cars"]} == {"STANDING"}
    outbound = _outbound(inventory, "OB-51")
    assert [car["code"] for car in outbound["assembled_cars"]] == ["C-D-51"]
    assert outbound["assembled_cars"][0]["state"] == "ASSEMBLED"
    assert [car["code"] for car in outbound["pending_cars"]] == ["C-A-51"]
    assert outbound["pending_cars"][0]["location"] == "N4-A"

    assert inventory["buckets"] == {
        "on_tracks": 2,
        "in_buffers": 2,
        "assembled": 1,
        "pending_intake": 0,
        "departed": 0,
        "removed": 0,
    }
    counts = yard["metrics"]["car_state_counts"]
    assert counts["standing"] == 3 and counts["reserved"] == 1 and counts["assembled"] == 1

    # --- finish the run: RETURN actions restore the buffer, assembly completes ---
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 3})
    assert advanced["completed"] is True
    yard = api.expect_ok("GET", "/api/yard")
    inventory = _assert_partition(yard, 5)
    assert [car["code"] for car in _track(inventory, "N4-A")["cars"]] == ["C-B-51", "C-C-51"]
    assert _bay(inventory, "X1")["car_count"] == 0
    assert [car["code"] for car in _outbound(inventory, "OB-51")["assembled_cars"]] == [
        "C-D-51",
        "C-A-51",
    ]
    assert inventory["buckets"]["on_tracks"] == 3
    assert inventory["buckets"]["in_buffers"] == 0
    assert inventory["buckets"]["assembled"] == 2

    return run_code


def _verify_against_persistence(data_dir: Path) -> None:
    """Restart the service on the same data dir and reconcile every object."""
    persisted = json.loads((data_dir / "yard-state.json").read_text(encoding="utf-8"))
    persisted_cars = {car["code"]: car for car in persisted["cars"]}
    assert persisted["tracks"][0]["stack"]  # sanity: order lives on disk

    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        yard = server.api.expect_ok("GET", "/api/yard")
        inventory = _assert_partition(yard, 5)

        # Detail vs. persisted single objects: state and location must agree.
        entries = _entry_index(inventory)
        expected = {
            "C-B-51": ("STANDING", "N4-A", "track"),
            "C-C-51": ("STANDING", "N4-A", "track"),
            "C-S2-51": ("STANDING", "S2-A", "track"),
            "C-A-51": ("ASSEMBLED", "OB-51", "outbound"),
            "C-D-51": ("ASSEMBLED", "OB-51", "outbound"),
        }
        for code, (state, location, kind) in expected.items():
            assert entries[code]["state"] == state, code
            assert entries[code]["location"] == location, code
            assert entries[code]["location_kind"] == kind, code
            stored = persisted_cars[code]
            assert stored["state"] == state, (code, stored)
            assert stored["location"] == location, (code, stored)

        # Bottom-to-top order is the persisted stack order, not action history.
        n4_stack = next(track["stack"] for track in persisted["tracks"] if track["code"] == "N4-A")
        assert n4_stack == ["C-B-51", "C-C-51"]
        assert [car["code"] for car in _track(inventory, "N4-A")["cars"]] == n4_stack
        x1_stack = next(bay["stack"] for bay in persisted["buffer_bays"] if bay["code"] == "X1")
        assert x1_stack == []
        assert _bay(inventory, "X1")["cars"] == []

        # Summary still reconciles with the detail after a cold restart.
        counts = yard["metrics"]["car_state_counts"]
        assert counts["standing"] == 3 and counts["assembled"] == 2
        assert yard["metrics"]["total_cars"] == inventory["total_cars"] == 5
    finally:
        server.stop()


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="switchyard-inv-"))
    data_dir = root / "data"
    try:
        server = RunningServer(data_dir=data_dir)
        try:
            server.wait_ready()
            run(server.api, data_dir)
        finally:
            server.stop()
        _verify_against_persistence(data_dir)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("OK wf_yard_inventory")


if __name__ == "__main__":
    main()
