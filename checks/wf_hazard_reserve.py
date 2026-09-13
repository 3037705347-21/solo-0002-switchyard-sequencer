"""Workflow check: hazard capacity protection during intake classification.

Four batches are verified against a fresh yard each time:

- hazard_first: hazardous cars arrive first and take HAZ-1 directly.
- normal_first: normal cars arrive first but may not consume the hazard
  reserve, so the trailing hazardous cars still fit on HAZ-1.
- normal_beyond_margin: normal cars exceed what fits outside the reserve;
  the excess is held back and the tradeoff is reported, not silent.
- no_hazard: without hazardous cars no reserve applies and normal cars may
  use HAZ-1 as ordinary general capacity.

The pre-fill intakes leave MIX-1 at 14 cars and HAZ-1 at 4 cars (general
cars ping-pong between the two by remaining-capacity score), so a following
batch of normal cars has only 4 general slots left, 2 of which sit on the
hazard-rated HAZ-1.
"""

from __future__ import annotations

from support import ApiClient, RunningServer

SHIFT = {"code": "SHIFT-01", "dispatcher": "LIN", "opened_at": "2026-09-08T08:00:00Z"}


def car(code: str, kind: str = "BOX", destination: str = "N4", length: int = 20, danger: str = "NONE") -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": danger,
    }


def haz_car(code: str, length: int = 22) -> dict[str, object]:
    return car(code, kind="TANK", destination="W9", length=length, danger="D1")


def intake_payload(code: str, cars: list[dict[str, object]]) -> dict[str, object]:
    return {"code": code, "route": "RAIL-11", "arrival_at": "2026-09-08T09:10:00Z", "cars": cars}


def open_shift(api: ApiClient) -> None:
    opened = api.expect_ok("POST", "/api/shifts", SHIFT)
    assert opened["state"] == "OPEN"


def classify(api: ApiClient, code: str, cars: list[dict[str, object]]) -> dict[str, object]:
    api.expect_ok("POST", "/api/intake-trains", intake_payload(code, cars))
    return api.expect_ok("POST", f"/api/intake-trains/{code}/classify", {})


def spot_map(classified: dict[str, object]) -> dict[str, str]:
    spots = classified["spots"]
    codes = [item["car_code"] for item in spots]
    assert len(codes) == len(set(codes)), f"car placed more than once: {codes}"
    per_track: dict[str, list[int]] = {}
    for item in spots:
        per_track.setdefault(item["track_code"], []).append(item["index"])
    for track_code, indexes in per_track.items():
        assert len(indexes) == len(set(indexes)), f"duplicate stack index on {track_code}"
    return {item["car_code"]: item["track_code"] for item in spots}


def track_metrics(api: ApiClient) -> dict[str, dict[str, object]]:
    yard = api.expect_ok("GET", "/api/yard")
    return {item["code"]: item for item in yard["metrics"]["track_metrics"]}


def standing_count(api: ApiClient) -> int:
    yard = api.expect_ok("GET", "/api/yard")
    return int(yard["metrics"]["car_state_counts"]["standing"])


def fill_general_tracks(api: ApiClient) -> None:
    """Fill N4-A and leave MIX-1 at 14 and HAZ-1 at 4 via score ping-pong."""
    first = classify(api, "INT-F1", [car(f"C-F1-{index:02d}") for index in range(1, 21)])
    assert first["intake"]["state"] == "CLASSIFIED"
    second = classify(api, "INT-F2", [car(f"C-F2-{index:02d}") for index in range(1, 9)])
    assert second["intake"]["state"] == "CLASSIFIED"
    metrics = track_metrics(api)
    assert metrics["MIX-1"]["cars"] == 14
    assert metrics["HAZ-1"]["cars"] == 4


def assert_completed_stacks_untouched(api: ApiClient) -> None:
    """N4-A was filled to its car cap by the pre-fill and must not change."""
    metrics = track_metrics(api)
    assert metrics["N4-A"]["cars"] == 10
    assert metrics["N4-A"]["top_car"] == "C-F1-10"


def scenario_hazard_first(api: ApiClient) -> None:
    open_shift(api)
    cars = [haz_car("C-HZ-01"), haz_car("C-HZ-02")] + [car(f"C-NM-{index:02d}") for index in range(1, 4)]
    classified = classify(api, "INT-T1", cars)
    assert classified["intake"]["state"] == "CLASSIFIED"
    assert classified["unplaced"] == []
    assert classified["tradeoffs"] == []
    tracks = spot_map(classified)
    assert tracks["C-HZ-01"] == "HAZ-1"
    assert tracks["C-HZ-02"] == "HAZ-1"
    for index in range(1, 4):
        assert tracks[f"C-NM-{index:02d}"] == "N4-A"
    assert standing_count(api) == 5


def scenario_normal_first(api: ApiClient) -> None:
    open_shift(api)
    fill_general_tracks(api)
    cars = [car(f"C-NM-{index:02d}") for index in range(1, 8)] + [haz_car("C-HZ-01"), haz_car("C-HZ-02")]
    classified = classify(api, "INT-T1", cars)
    tracks = spot_map(classified)
    # The reserve keeps two HAZ-1 slots: both hazardous cars are spotted even
    # though the normal cars arrived first and HAZ-1 was almost full.
    assert tracks["C-HZ-01"] == "HAZ-1"
    assert tracks["C-HZ-02"] == "HAZ-1"
    assert tracks["C-NM-02"] == "HAZ-1"
    assert tracks["C-NM-04"] == "HAZ-1"
    for code in ("C-NM-01", "C-NM-03", "C-NM-05", "C-NM-06"):
        assert tracks[code] == "MIX-1"
    # The last normal car no longer fits outside the reserve and is held back.
    assert classified["unplaced"] == ["C-NM-07"]
    assert classified["intake"]["state"] == "PARTIAL"
    tradeoffs = classified["tradeoffs"]
    assert len(tradeoffs) == 1
    assert "C-NM-07" in tradeoffs[0] and "reserved" in tradeoffs[0]
    metrics = track_metrics(api)
    assert metrics["HAZ-1"]["cars"] == 8
    assert metrics["MIX-1"]["cars"] == 18
    assert_completed_stacks_untouched(api)
    assert standing_count(api) == 36


def scenario_normal_beyond_margin(api: ApiClient) -> None:
    open_shift(api)
    fill_general_tracks(api)
    cars = [car(f"C-NM-{index:02d}") for index in range(1, 13)] + [haz_car("C-HZ-01"), haz_car("C-HZ-02")]
    classified = classify(api, "INT-T1", cars)
    tracks = spot_map(classified)
    # Only four general slots remain outside the reserve; the six normal cars
    # beyond that margin are held back and every hold is explained instead of
    # silently sacrificing the hazardous cars.
    assert tracks["C-HZ-01"] == "HAZ-1"
    assert tracks["C-HZ-02"] == "HAZ-1"
    held = [f"C-NM-{index:02d}" for index in range(7, 13)]
    assert classified["unplaced"] == held
    assert classified["intake"]["state"] == "PARTIAL"
    tradeoffs = classified["tradeoffs"]
    assert len(tradeoffs) == len(held)
    for code, note in zip(held, tradeoffs):
        assert code in note and "reserved" in note
    metrics = track_metrics(api)
    assert metrics["HAZ-1"]["cars"] == 8
    assert metrics["MIX-1"]["cars"] == 18
    assert_completed_stacks_untouched(api)
    assert standing_count(api) == 36


def scenario_no_hazard(api: ApiClient) -> None:
    open_shift(api)
    fill_general_tracks(api)
    cars = [car(f"C-NM-{index:02d}") for index in range(1, 9)]
    classified = classify(api, "INT-T1", cars)
    assert classified["intake"]["state"] == "CLASSIFIED"
    assert classified["unplaced"] == []
    assert classified["tradeoffs"] == []
    tracks = spot_map(classified)
    # No reserve applies, so HAZ-1 serves as ordinary general capacity.
    for code in ("C-NM-02", "C-NM-04", "C-NM-06", "C-NM-08"):
        assert tracks[code] == "HAZ-1"
    for code in ("C-NM-01", "C-NM-03", "C-NM-05", "C-NM-07"):
        assert tracks[code] == "MIX-1"
    metrics = track_metrics(api)
    assert metrics["HAZ-1"]["cars"] == 8
    assert metrics["MIX-1"]["cars"] == 18
    assert_completed_stacks_untouched(api)
    assert standing_count(api) == 36


SCENARIOS = [
    ("hazard_first", scenario_hazard_first),
    ("normal_first", scenario_normal_first),
    ("normal_beyond_margin", scenario_normal_beyond_margin),
    ("no_hazard", scenario_no_hazard),
]


def main() -> int:
    for name, scenario in SCENARIOS:
        server = RunningServer()
        try:
            server.wait_ready()
            scenario(server.api)
            print(f"OK wf_hazard_reserve/{name}")
        finally:
            server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
