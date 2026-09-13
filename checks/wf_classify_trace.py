"""Workflow check: per-car decision trace returned by intake classification."""

from __future__ import annotations

import json
from pathlib import Path

from support import ApiClient, RunningServer

SEED_TRACK_ORDER = ["N4-A", "E7-A", "S2-A", "W9-A", "MIX-1", "HAZ-1", "MAINT-1"]


def car(code: str, kind: str, destination: str, length_m: int, danger_class: str = "NONE") -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length_m,
        "danger_class": danger_class,
    }


def intake_payload(code: str, cars: list[dict[str, object]]) -> dict[str, object]:
    return {
        "code": code,
        "route": "RAIL-22",
        "arrival_at": "2026-09-13T09:10:00Z",
        "cars": cars,
    }


def open_shift(api: ApiClient) -> None:
    opened = api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-01", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )
    assert opened["state"] == "OPEN"


def track_metrics(api: ApiClient) -> dict[str, dict[str, object]]:
    yard = api.expect_ok("GET", "/api/yard")
    return {item["code"]: item for item in yard["metrics"]["track_metrics"]}


def read_stack(server: RunningServer, track_code: str) -> list[str]:
    state_path = Path(server.temp_dir.name) / "data" / "yard-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    track = next(item for item in state["tracks"] if item["code"] == track_code)
    return list(track["stack"])


def scenario_all_placed() -> None:
    server = RunningServer()
    try:
        server.wait_ready()
        api = server.api
        open_shift(api)
        cars = [
            car("C-A-N4-1", "BOX", "N4", 18),
            car("C-A-E7-1", "FLAT", "E7", 16),
            car("C-A-HAZ-1", "TANK", "W9", 22, "D1"),
        ]
        api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-A1", cars))
        classified = api.expect_ok("POST", "/api/intake-trains/INT-A1/classify", {})
        # Legacy fields are still present for existing clients.
        assert classified["intake"]["state"] == "CLASSIFIED"
        assert classified["unplaced"] == []
        spots = classified["spots"]
        assert [item["car_code"] for item in spots] == ["C-A-N4-1", "C-A-E7-1", "C-A-HAZ-1"]
        for spot in spots:
            assert set(spot) >= {"car_code", "track_code", "index"}

        decisions = classified["decisions"]
        assert [item["car_code"] for item in decisions] == ["C-A-N4-1", "C-A-E7-1", "C-A-HAZ-1"]
        # Every placed decision agrees with the legacy spots of the same round.
        spot_by_car = {item["car_code"]: item for item in spots}
        for decision in decisions:
            spot = spot_by_car[decision["car_code"]]
            assert decision["outcome"] == "placed"
            assert decision["track_code"] == spot["track_code"]
            assert decision["index"] == spot["index"]
            ranks = [option["rank"] for option in decision["candidates"]]
            assert ranks == list(range(1, len(ranks) + 1))
            assert decision["candidates"][0]["track_code"] == decision["track_code"]

        first = decisions[0]
        assert [option["track_code"] for option in first["candidates"]] == ["N4-A", "MIX-1", "HAZ-1"]
        assert first["candidates"][0]["remaining_cars"] == 10
        assert first["candidates"][0]["remaining_length_m"] == 300
        assert first["track_code"] == "N4-A"
        assert first["index"] == 0
        assert first["remaining_cars_after"] == 9
        assert first["remaining_length_m_after"] == 282
        rejected = {item["track_code"]: item["reason"] for item in first["rejections"]}
        assert rejected["E7-A"] == "track E7-A rejects destination N4"
        assert rejected["MAINT-1"] == "track MAINT-1 is MAINTENANCE"

        haz = decisions[2]
        assert [option["track_code"] for option in haz["candidates"]] == ["HAZ-1"]
        assert haz["track_code"] == "HAZ-1"
        assert haz["remaining_cars_after"] == 7
        assert haz["remaining_length_m_after"] == 218
        haz_rejected = {item["track_code"]: item["reason"] for item in haz["rejections"]}
        assert haz_rejected["W9-A"] == "track W9-A is not hazard rated"
        assert haz_rejected["MIX-1"] == "track MIX-1 is not hazard rated"

        # The trace matches the yard state that was actually written.
        metrics = track_metrics(api)
        assert metrics["N4-A"]["cars"] == 1 and metrics["N4-A"]["length_m"] == 18
        assert metrics["HAZ-1"]["cars"] == 1 and metrics["HAZ-1"]["length_m"] == 22
        assert first["remaining_cars_after"] + metrics["N4-A"]["cars"] == metrics["N4-A"]["capacity_cars"]
        assert first["remaining_length_m_after"] + metrics["N4-A"]["length_m"] == 300
        assert haz["remaining_cars_after"] + metrics["HAZ-1"]["cars"] == 8
    finally:
        server.stop()


def scenario_partial_then_repeat() -> None:
    server = RunningServer()
    try:
        server.wait_ready()
        api = server.api
        open_shift(api)
        cars = [car(f"C-B-HAZ-{index}", "TANK", "W9", 20, "D1") for index in range(1, 10)]
        api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-B1", cars))
        classified = api.expect_ok("POST", "/api/intake-trains/INT-B1/classify", {})
        assert classified["intake"]["state"] == "PARTIAL"
        assert classified["unplaced"] == ["C-B-HAZ-9"]
        spots = classified["spots"]
        assert len(spots) == 8
        assert [item["index"] for item in spots] == list(range(8))
        assert all(item["track_code"] == "HAZ-1" for item in spots)

        decisions = classified["decisions"]
        assert [item["car_code"] for item in decisions] == [f"C-B-HAZ-{i}" for i in range(1, 10)]
        for index, decision in enumerate(decisions[:8], start=1):
            assert decision["outcome"] == "placed"
            assert decision["track_code"] == "HAZ-1"
            assert decision["index"] == index - 1
            assert [option["track_code"] for option in decision["candidates"]] == ["HAZ-1"]
            option = decision["candidates"][0]
            assert option["remaining_cars"] == 8 - (index - 1)
            assert option["remaining_length_m"] == 240 - 20 * (index - 1)
            assert decision["remaining_cars_after"] == 8 - index
            assert decision["remaining_length_m_after"] == 240 - 20 * index

        blocked = decisions[8]
        assert blocked["car_code"] == "C-B-HAZ-9"
        assert blocked["outcome"] == "unplaced"
        assert blocked["track_code"] is None
        assert blocked["candidates"] == []
        assert blocked["detail"] == "no candidate track available"
        # Rejections are listed in the order the tracks were ruled out.
        assert [item["track_code"] for item in blocked["rejections"]] == SEED_TRACK_ORDER
        reasons = {item["track_code"]: item["reason"] for item in blocked["rejections"]}
        assert reasons["HAZ-1"] == "track HAZ-1 is at car capacity"
        assert reasons["W9-A"] == "track W9-A is not hazard rated"
        assert reasons["MIX-1"] == "track MIX-1 is not hazard rated"
        assert reasons["MAINT-1"] == "track MAINT-1 is MAINTENANCE"

        # The persisted stack order matches the placement trace.
        assert read_stack(server, "HAZ-1") == [f"C-B-HAZ-{i}" for i in range(1, 9)]

        # Repeating classification only describes what this round really does.
        repeated = api.expect_ok("POST", "/api/intake-trains/INT-B1/classify", {})
        assert repeated["intake"]["state"] == "PARTIAL"
        assert repeated["spots"] == []
        assert repeated["unplaced"] == [f"C-B-HAZ-{i}" for i in range(1, 10)]
        repeat_decisions = repeated["decisions"]
        assert len(repeat_decisions) == 9
        for decision in repeat_decisions[:8]:
            assert decision["outcome"] == "unplaced"
            assert decision["track_code"] is None
            assert decision["candidates"] == []
            assert decision["rejections"] == []
            assert "STANDING" in str(decision["detail"])
        blocked_again = repeat_decisions[8]
        assert blocked_again["car_code"] == "C-B-HAZ-9"
        assert blocked_again["outcome"] == "unplaced"
        again_reasons = {item["track_code"]: item["reason"] for item in blocked_again["rejections"]}
        assert again_reasons["HAZ-1"] == "track HAZ-1 is at car capacity"

        # The repeat round wrote nothing: stack order and occupancy are unchanged.
        assert read_stack(server, "HAZ-1") == [f"C-B-HAZ-{i}" for i in range(1, 9)]
        metrics = track_metrics(api)
        assert metrics["HAZ-1"]["cars"] == 8
        assert metrics["HAZ-1"]["top_car"] == "C-B-HAZ-8"

        # The journaled events reflect each round separately, no replayed placements.
        shift = api.expect_ok("GET", "/api/shifts/SHIFT-01")
        events = [event for event in shift["events"] if event["kind"] == "TRAIN_CLASSIFIED"]
        assert len(events) == 2
        first_payload = events[0]["payload"]
        assert first_payload["spotted"] == 8
        assert len(first_payload["decisions"]) == 9
        second_payload = events[1]["payload"]
        assert second_payload["spotted"] == 0
        assert second_payload["spots"] == []
        assert all(item["outcome"] == "unplaced" for item in second_payload["decisions"])
    finally:
        server.stop()


def scenario_hazard_needs_rated_track() -> None:
    server = RunningServer()
    try:
        server.wait_ready()
        api = server.api
        open_shift(api)
        cars = [car("C-C-HAZ-1", "TANK", "S2", 24, "D2")]
        api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-C1", cars))
        classified = api.expect_ok("POST", "/api/intake-trains/INT-C1/classify", {})
        assert classified["intake"]["state"] == "CLASSIFIED"
        assert classified["unplaced"] == []
        assert len(classified["spots"]) == 1
        spot = classified["spots"][0]
        assert spot["car_code"] == "C-C-HAZ-1"
        assert spot["track_code"] == "HAZ-1"
        assert spot["index"] == 0

        decision = classified["decisions"][0]
        assert decision["outcome"] == "placed"
        # The hazard-rated general track is the only candidate.
        assert [option["track_code"] for option in decision["candidates"]] == ["HAZ-1"]
        assert decision["track_code"] == "HAZ-1"
        assert decision["remaining_cars_after"] == 7
        assert decision["remaining_length_m_after"] == 216
        reasons = {item["track_code"]: item["reason"] for item in decision["rejections"]}
        # The car's own destination track is blocked by the hazard rule.
        assert reasons["S2-A"] == "track S2-A is not hazard rated"
        assert reasons["MIX-1"] == "track MIX-1 is not hazard rated"
        assert reasons["N4-A"] == "track N4-A rejects destination S2"
        assert reasons["MAINT-1"] == "track MAINT-1 is MAINTENANCE"
    finally:
        server.stop()


def main() -> int:
    scenario_all_placed()
    scenario_partial_then_repeat()
    scenario_hazard_needs_rated_track()
    print("OK wf_classify_trace")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
