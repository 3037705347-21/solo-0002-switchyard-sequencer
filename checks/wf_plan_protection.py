"""Workflow check: a queued pull plan must survive later intake classification.

Covers the reported chain: classify an intake, plan a pull for a deep car,
then classify another same-destination car before the crew starts the run.
The second classification must not stack onto a track the queued plan depends
on; when no other track can take the car, classification must fail clearly at
classify time instead of letting the run break later with RESOURCE_BUSY.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from support import ApiClient, RunningServer


def car_payload(code: str, destination: str, danger: str = "NONE", kind: str = "BOX") -> dict[str, Any]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": 18,
        "danger_class": danger,
    }


def create_and_classify(api: ApiClient, code: str, cars: list[dict[str, Any]]) -> dict[str, Any]:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {"code": code, "route": f"RAIL-{code}", "arrival_at": "2026-09-13T09:00:00Z", "cars": cars},
    )
    return api.expect_ok("POST", f"/api/intake-trains/{code}/classify", {})


def load_state(data_dir: Path) -> dict[str, Any]:
    return json.loads((data_dir / "yard-state.json").read_text(encoding="utf-8"))


def load_journal(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def track_stack(state: dict[str, Any], code: str) -> list[str]:
    for track in state["tracks"]:
        if track["code"] == code:
            return list(track["stack"])
    raise AssertionError(f"track {code} missing from state")


def bay_stack(state: dict[str, Any], code: str) -> list[str]:
    for bay in state["buffer_bays"]:
        if bay["code"] == code:
            return list(bay["stack"])
    raise AssertionError(f"bay {code} missing from state")


def car_record(state: dict[str, Any], code: str) -> dict[str, Any]:
    for car in state["cars"]:
        if car["code"] == code:
            return car
    raise AssertionError(f"car {code} missing from state")


def intake_record(state: dict[str, Any], code: str) -> dict[str, Any]:
    for train in state["intakes"]:
        if train["code"] == code:
            return train
    raise AssertionError(f"intake {code} missing from state")


def scenario_plan_then_classify(api: ApiClient, data_dir: Path) -> None:
    """The reported chain: plan first, classify a same-destination car after."""
    classified = create_and_classify(
        api,
        "INT-01",
        [car_payload("C-N4-01", "N4"), car_payload("C-N4-02", "N4"), car_payload("C-N4-03", "N4", kind="FLAT")],
    )
    assert classified["intake"]["state"] == "CLASSIFIED"
    assert [spot["track_code"] for spot in classified["spots"]] == ["N4-A", "N4-A", "N4-A"]

    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-01", "destination": "N4", "car_codes": ["C-N4-01"]})
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-01/sequencer", {"transfer_code": "X1"})
    run = sequenced["pull_run"]
    assert run["state"] == "QUEUED"
    steps = [(step["verb"], step["car_code"], step["source_code"], step["target_code"]) for step in run["steps"]]
    assert steps == [
        ("BUFFER", "C-N4-03", "N4-A", "X1"),
        ("BUFFER", "C-N4-02", "N4-A", "X1"),
        ("PULL", "C-N4-01", "N4-A", "OB-01"),
        ("RETURN", "C-N4-02", "X1", "N4-A"),
        ("RETURN", "C-N4-03", "X1", "N4-A"),
    ]

    # Normal intake of another N4 car while the plan is still queued.
    second = create_and_classify(api, "INT-02", [car_payload("C-N4-09", "N4")])
    assert second["intake"]["state"] == "CLASSIFIED"
    assert second["spots"][0]["track_code"] != "N4-A", "classifier must not stack onto a pinned track"
    state = load_state(data_dir)
    assert track_stack(state, "N4-A") == ["C-N4-01", "C-N4-02", "C-N4-03"], "queued plan source stack changed"
    assert car_record(state, "C-N4-09")["location"] == second["spots"][0]["track_code"]

    # The queued plan now executes step by step, in generated order.
    expected_stacks = [
        (["C-N4-01", "C-N4-02"], ["C-N4-03"]),
        (["C-N4-01"], ["C-N4-03", "C-N4-02"]),
        ([], ["C-N4-03", "C-N4-02"]),
        (["C-N4-02"], ["C-N4-03"]),
        (["C-N4-02", "C-N4-03"], []),
    ]
    for index, (track_expected, bay_expected) in enumerate(expected_stacks):
        advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-01/advance", {"steps": 1})
        assert advanced["executed_steps"] == 1
        assert advanced["pull_run"]["current_step"] == index + 1
        state = load_state(data_dir)
        assert track_stack(state, "N4-A") == track_expected, f"track stack after step {index + 1}"
        assert bay_stack(state, "X1") == bay_expected, f"bay stack after step {index + 1}"
    assert advanced["completed"] is True
    assert advanced["pull_run"]["state"] == "COMPLETED"
    assert advanced["outbound"]["state"] == "READY"
    assert advanced["outbound"]["assembled_car_codes"] == ["C-N4-01"]

    departed = api.expect_ok("POST", "/api/outbound-trains/OB-01/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    state = load_state(data_dir)
    assert car_record(state, "C-N4-01")["state"] == "DEPARTED"
    assert car_record(state, "C-N4-09")["state"] == "STANDING"


def scenario_classification_must_block(api: ApiClient, data_dir: Path) -> None:
    """When the only compatible track is pinned, classification fails clearly."""
    classified = create_and_classify(api, "INT-10", [car_payload("C-HZ-01", "E7", danger="D1", kind="TANK")])
    assert classified["spots"][0]["track_code"] == "HAZ-1"
    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-02", "destination": "E7", "car_codes": ["C-HZ-01"]})
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-02/sequencer", {"transfer_code": "X1"})
    assert sequenced["pull_run"]["state"] == "QUEUED"

    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-11",
            "route": "RAIL-INT-11",
            "arrival_at": "2026-09-13T10:00:00Z",
            "cars": [car_payload("C-HZ-02", "E7", danger="D1", kind="TANK")],
        },
    )
    error = api.expect_error("POST", "/api/intake-trains/INT-11/classify", {})
    assert error["code"] == "RESOURCE_BUSY", error
    for token in ("C-HZ-02", "HAZ-1", "RUN-OB-02"):
        assert token in error["message"], f"error message should mention {token}: {error['message']}"

    # The failed classification is atomic: nothing moved, no event recorded.
    state = load_state(data_dir)
    assert track_stack(state, "HAZ-1") == ["C-HZ-01"]
    blocked_car = car_record(state, "C-HZ-02")
    assert blocked_car["state"] == "RECEIVED" and blocked_car["location"] == "INTAKE"
    assert intake_record(state, "INT-11")["state"] == "OPEN"
    journal = load_journal(data_dir)
    assert not any(
        event["kind"] == "TRAIN_CLASSIFIED" and "INT-11" in event["message"] for event in journal
    )

    # Ordinary cars for other tracks still classify while the pin is active.
    other = create_and_classify(api, "INT-12", [car_payload("C-E7-01", "E7")])
    assert other["spots"][0]["track_code"] == "E7-A"

    # Finish the queued plan; the pin lifts and the blocked intake classifies.
    advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-02/advance", {"steps": 5})
    assert advanced["completed"] is True
    api.expect_ok("POST", "/api/outbound-trains/OB-02/depart", {})
    retried = api.expect_ok("POST", "/api/intake-trains/INT-11/classify", {})
    assert retried["intake"]["state"] == "CLASSIFIED"
    assert retried["spots"][0]["track_code"] == "HAZ-1"


def verify_consistency(api: ApiClient, data_dir: Path) -> None:
    """Yard occupancy, car states, and the event journal must agree."""
    state = load_state(data_dir)
    cars = {car["code"]: car for car in state["cars"]}
    stacked: set[str] = set()
    for track in state["tracks"]:
        for code in track["stack"]:
            car = cars[code]
            assert car["location"] == track["code"], f"{code} location disagrees with {track['code']}"
            assert car["state"] in {"STANDING", "RESERVED"}, f"{code} stacked but {car['state']}"
            stacked.add(code)
    for car in cars.values():
        if car["state"] in {"STANDING", "RESERVED"}:
            assert car["code"] in stacked, f"{car['code']} is {car['state']} but not stacked"
    assert bay_stack(state, "X1") == []

    metrics = api.expect_ok("GET", "/api/yard")["metrics"]
    counts = metrics["car_state_counts"]
    assert counts["standing"] == sum(1 for car in cars.values() if car["state"] == "STANDING")
    assert counts["departed"] == sum(1 for car in cars.values() if car["state"] == "DEPARTED")
    assert counts["reserved"] == 0 and counts["received"] == 0
    assert metrics["active_runs"] == []

    journal = load_journal(data_dir)
    assert journal == state["events"], "journal and workspace events diverged"
    sequences = [event["sequence"] for event in journal]
    assert sequences == list(range(1, len(journal) + 1)), "event sequence is not contiguous"
    milestones = [
        ("TRAIN_CLASSIFIED", "INT-01"),
        ("PULL_PLANNED", "RUN-OB-01"),
        ("TRAIN_CLASSIFIED", "INT-02"),
        ("PULL_RUN_STARTED", "RUN-OB-01"),
        ("PULL_RUN_COMPLETED", "RUN-OB-01"),
        ("TRAIN_DEPARTED", "OB-01"),
        ("TRAIN_CLASSIFIED", "INT-11"),
    ]
    cursor = 0
    for kind, token in milestones:
        while cursor < len(journal) and not (
            journal[cursor]["kind"] == kind and token in journal[cursor]["message"]
        ):
            cursor += 1
        assert cursor < len(journal), f"missing event {kind} mentioning {token}"
        cursor += 1
    completed = next(event for event in journal if event["kind"] == "PULL_RUN_COMPLETED")
    assert completed["payload"]["assembled_car_codes"] == ["C-N4-01"]


def run(api: ApiClient, data_dir: Path) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-01", "dispatcher": "RUI", "opened_at": "2026-09-13T08:00:00Z"},
    )
    scenario_plan_then_classify(api, data_dir)
    scenario_classification_must_block(api, data_dir)
    verify_consistency(api, data_dir)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="switchyard-check-") as temp_dir:
        data_dir = Path(temp_dir) / "data"
        server = RunningServer(data_dir=data_dir)
        try:
            server.wait_ready()
            run(server.api, data_dir)
            print("OK wf_plan_protection")
        finally:
            server.stop()
