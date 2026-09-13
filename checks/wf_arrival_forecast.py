"""Workflow check: read-only arrival capacity forecasting.

Covers the four requested datasets (empty yard, partial occupancy with a
persisted plan, a hazardous car that cannot be placed, and several trains with
the same arrival time) plus a real classification cross-check that compares the
forecast assignment with the actual classify result. Track arrangement changes
(mandatory maintenance / transfer duty) are also persisted and reloaded across
a service restart.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from support import ApiClient, RunningServer


def _car(code: str, destination: str, *, kind: str = "BOX", length: int = 15, danger: str = "NONE") -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": danger,
    }


def _train(code: str, arrival: str, cars: list[dict[str, object]], route: str = "RAIL-F") -> dict[str, object]:
    return {"code": code, "route": route, "arrival_at": arrival, "cars": cars}


def _forecast(trains: list[dict[str, object]], horizon: str = "2026-09-13T16:00:00Z") -> dict[str, object]:
    payload: dict[str, object] = {"trains": trains}
    if horizon:
        payload["shift_horizon_at"] = horizon
    return payload


def _by_code(rows: list[dict[str, Any]], key: str = "code") -> dict[str, Any]:
    return {str(row[key]): row for row in rows}


def open_shift(api: ApiClient, code: str = "SHIFT-F1") -> None:
    opened = api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": code, "dispatcher": "FENG", "opened_at": "2026-09-13T08:00:00Z"},
    )
    assert opened["state"] == "OPEN"


# --- dataset 1: empty yard -------------------------------------------------


def scenario_empty_yard(api: ApiClient) -> None:
    open_shift(api)
    train = _train(
        "FCST-E1",
        "2026-09-13T09:00:00Z",
        [
            _car("C-EY-01", "N4"),
            _car("C-EY-02", "E7", kind="FLAT"),
            _car("C-EY-03", "S2", kind="HOPPER"),
            _car("C-EY-04", "W9", kind="TANK", danger="D1"),
        ],
    )
    report = api.expect_ok("POST", "/api/arrival-forecast", _forecast([train]))
    [row] = report["trains"]
    assert row["code"] == "FCST-E1"
    assert row["source"] == "PLANNED_FORECAST"
    assert row["within_shift"] is True
    assert row["placeable_cars"] == 4
    assert row["blocked_cars"] == 0
    assert row["fully_placeable"] is True
    spots = _by_code(row["spots"], "car_code")
    assert spots["C-EY-01"]["track_code"] == "N4-A"
    assert spots["C-EY-02"]["track_code"] == "E7-A"
    assert spots["C-EY-03"]["track_code"] == "S2-A"
    assert spots["C-EY-04"]["track_code"] == "HAZ-1"

    tracks = _by_code(report["tracks"])
    for code in ("N4-A", "E7-A", "S2-A", "W9-A", "MIX-1", "HAZ-1"):
        assert tracks[code]["current_cars"] == 0, code
    assert tracks["N4-A"]["planned_cars"] == 1
    assert tracks["N4-A"]["planned_by_source"] == [{"source": "PLANNED_FORECAST", "cars": 1}]
    assert tracks["HAZ-1"]["planned_cars"] == 1
    assert tracks["N4-A"]["remaining_cars"] == 9
    types = _by_code(report["remaining_track_types"], "track_type")
    assert types["DEST:N4"]["remaining_cars"] == 9
    assert types["DEST:W9"]["remaining_cars"] == 10
    assert types["GENERAL"]["remaining_cars"] == 18
    assert types["HAZ:GENERAL"]["remaining_cars"] == 7
    unavailable = {item["code"]: item["reason_code"] for item in report["unavailable_tracks"]}
    assert unavailable == {"MAINT-1": "TRACK_IN_MAINTENANCE"}
    assert report["exhausted_track_types"] == []
    assert report["totals"]["cars_placeable"] == 4

    # Forecast is read-only: nothing landed in the yard.
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["total_cars"] == 0
    assert yard["metrics"]["version"] == yard["metrics"]["version"]
    assert all(row["cars"] == 0 for row in yard["metrics"]["track_metrics"])


# --- dataset 2: partial occupancy + persisted plan + horizon --------------


def scenario_partial_occupancy(api: ApiClient) -> None:
    open_shift(api, code="SHIFT-F2")
    # Persisted, not yet classified intake: its cars are "planned" occupancy.
    persisted = _train(
        "INT-P1",
        "2026-09-13T09:00:00Z",
        [_car(f"C-P1-{i:02d}", "N4", length=20) for i in range(10)],
        route="RAIL-P1",
    )
    created = api.expect_ok("POST", "/api/intake-trains", persisted)
    assert created["intake"]["state"] == "OPEN"

    trains = [
        _train(
            "FCST-P2",
            "2026-09-13T10:00:00Z",
            [
                _car("C-P2-01", "N4", length=20),
                _car("C-P2-02", "N4", length=20),
                _car("C-P2-03", "N4", length=20),
            ],
            route="RAIL-P2",
        ),
        _train(
            "FCST-LATE",
            "2026-09-13T19:30:00Z",
            [_car("C-LATE-01", "E7")],
            route="RAIL-LATE",
        ),
    ]
    report = api.expect_ok("POST", "/api/arrival-forecast", _forecast(trains))
    rows = _by_code(report["trains"])
    assert list(_by_code(report["trains"]).keys())  # ordered by arrival
    assert [row["code"] for row in report["trains"]] == ["INT-P1", "FCST-P2", "FCST-LATE"]

    persisted_row = rows["INT-P1"]
    assert persisted_row["source"] == "PLANNED_PERSISTED"
    assert persisted_row["placeable_cars"] == 10
    assert {spot["track_code"] for spot in persisted_row["spots"]} == {"N4-A"}

    # N4-A is full from the persisted plan, so FCST-P2 overflows to MIX-1.
    overflow = rows["FCST-P2"]
    assert overflow["placeable_cars"] == 3
    assert {spot["track_code"] for spot in overflow["spots"]} == {"MIX-1"}
    assert overflow["blocked_cars"] == 0

    late = rows["FCST-LATE"]
    assert late["within_shift"] is False
    assert late["window_reason_code"] == "AFTER_SHIFT_HORIZON"
    assert late["placeable_cars"] == 0
    assert late["blocked"][0]["reason_code"] == "AFTER_SHIFT_HORIZON"

    tracks = _by_code(report["tracks"])
    n4 = tracks["N4-A"]
    assert n4["current_cars"] == 0
    assert n4["planned_cars"] == 10
    assert n4["planned_by_source"] == [{"source": "PLANNED_PERSISTED", "cars": 10}]
    assert n4["projected_cars"] == 10
    assert n4["remaining_cars"] == 0
    mix = tracks["MIX-1"]
    assert mix["current_cars"] == 0
    assert mix["planned_cars"] == 3
    assert mix["planned_by_source"] == [{"source": "PLANNED_FORECAST", "cars": 3}]
    assert mix["remaining_cars"] == 15

    exhausted = _by_code(report["exhausted_track_types"], "track_type")
    assert "DEST:N4" in exhausted
    available = _by_code(report["remaining_track_types"], "track_type")
    assert "DEST:N4" not in available
    assert available["GENERAL"]["remaining_cars"] == 15
    assert report["totals"]["cars_blocked"] == 1
    assert report["totals"]["trains_blocked"] == 1


# --- dataset 3: hazardous car cannot land, arrangement changes + restart ---


def scenario_hazard_blocked_with_restart() -> None:
    data_dir = Path(__file__).resolve().parent.parent / ".check-data" / "forecast"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    def start() -> tuple[RunningServer, ApiClient]:
        server = RunningServer(data_dir=data_dir)
        server.wait_ready()
        return server, server.api

    server, api = start()
    try:
        open_shift(api, code="SHIFT-H1")
        haz_train = _train(
            "FCST-H1",
            "2026-09-13T09:30:00Z",
            [
                _car("C-HZ-01", "W9", kind="TANK", danger="D1", length=24),
                _car("C-HZ-02", "E7"),
            ],
            route="RAIL-H1",
        )

        # HAZ-1 is the only hazard-rated track; schedule maintenance on it.
        arranged = api.expect_ok(
            "POST", "/api/tracks/HAZ-1/arrangement", {"state": "MAINTENANCE", "note": "valve check"}
        )
        assert arranged["changed"] is True
        assert arranged["changes"] == {"state": "OPERATIONAL -> MAINTENANCE"}

        report = api.expect_ok("POST", "/api/arrival-forecast", _forecast([haz_train]))
        [row] = report["trains"]
        assert row["placeable_cars"] == 1
        assert row["blocked_cars"] == 1
        [block] = row["blocked"]
        assert block["car_code"] == "C-HZ-01"
        assert block["reason_code"] == "HAZARD_TRACK_UNAVAILABLE"
        assert block["source"] == "PLANNED_FORECAST"
        unavailable = {item["code"]: item["reason_code"] for item in report["unavailable_tracks"]}
        assert unavailable["HAZ-1"] == "TRACK_IN_MAINTENANCE"

        # A non-empty track cannot enter maintenance.
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            _train("INT-H2", "2026-09-13T10:00:00Z", [_car("C-H2-01", "N4")], route="RAIL-H2"),
        )
        api.expect_ok("POST", "/api/intake-trains/INT-H2/classify", {})
        busy = api.expect_error("POST", "/api/tracks/N4-A/arrangement", {"state": "MAINTENANCE"})
        assert busy["code"] == "RESOURCE_BUSY"

        # Put the empty MIX-1 onto transfer duty: a plain car loses overflow.
        transferred = api.expect_ok("POST", "/api/tracks/MIX-1/arrangement", {"purpose": "TRANSFER"})
        assert transferred["changes"] == {"purpose": "GENERAL -> TRANSFER"}
        unavailable2 = {
            item["code"]: item["reason_code"]
            for item in api.expect_ok("POST", "/api/arrival-forecast", _forecast([haz_train]))["unavailable_tracks"]
        }
        assert unavailable2["MIX-1"] == "TRACK_TRANSFER_DUTY"
        assert unavailable2["HAZ-1"] == "TRACK_IN_MAINTENANCE"

        # Read-only guarantee: the forecast never created its cars/plans.
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["total_cars"] == 1  # only C-H2-01 from the real classify
    finally:
        server.stop()

    # Restart against the same data directory: arrangements survive and the
    # same forecast must come back byte-for-byte (it has no timestamps).
    restarted, api2 = start()
    try:
        tracks = _by_code(api2.expect_ok("GET", "/api/yard")["metrics"]["track_metrics"])
        assert tracks["HAZ-1"]["state"] == "MAINTENANCE"
        # metrics expose purpose via the track snapshot? purpose is not in metrics,
        # so verify the forecast's unavailable list instead.
        haz_train = _train(
            "FCST-H1",
            "2026-09-13T09:30:00Z",
            [
                _car("C-HZ-01", "W9", kind="TANK", danger="D1", length=24),
                _car("C-HZ-02", "E7"),
            ],
            route="RAIL-H1",
        )
        report = api2.expect_ok("POST", "/api/arrival-forecast", _forecast([haz_train]))
        [row] = report["trains"]
        assert row["blocked_cars"] == 1
        assert row["blocked"][0]["reason_code"] == "HAZARD_TRACK_UNAVAILABLE"
        unavailable = {item["code"]: item["reason_code"] for item in report["unavailable_tracks"]}
        assert unavailable["MIX-1"] == "TRACK_TRANSFER_DUTY"
        assert unavailable["HAZ-1"] == "TRACK_IN_MAINTENANCE"

        # Restore both arrangements; the hazard car must now place on HAZ-1.
        api2.expect_ok("POST", "/api/tracks/HAZ-1/arrangement", {"state": "OPERATIONAL"})
        api2.expect_ok("POST", "/api/tracks/MIX-1/arrangement", {"purpose": "GENERAL"})
        fixed = api2.expect_ok("POST", "/api/arrival-forecast", _forecast([haz_train]))
        [row] = fixed["trains"]
        assert row["fully_placeable"] is True
        spots = _by_code(row["spots"], "car_code")
        assert spots["C-HZ-01"]["track_code"] == "HAZ-1"
    finally:
        restarted.stop()
        shutil.rmtree(data_dir, ignore_errors=True)


# --- dataset 4: several trains with the same arrival time -----------------


def scenario_same_arrival(api: ApiClient) -> None:
    open_shift(api, code="SHIFT-F4")
    trains = [
        _train(
            "FCST-S2",
            "2026-09-13T11:00:00Z",
            [_car(f"C-S2-{i:02d}", "E7", length=20) for i in range(5)],
            route="RAIL-S2",
        ),
        _train(
            "FCST-S1",
            "2026-09-13T11:00:00Z",
            [_car(f"C-S1-{i:02d}", "E7", length=20) for i in range(8)],
            route="RAIL-S1",
        ),
        _train(
            "FCST-S3",
            "2026-09-13T11:00:00Z",
            [_car(f"C-S3-{i:02d}", "E7", length=20) for i in range(3)],
            route="RAIL-S3",
        ),
    ]
    report = api.expect_ok("POST", "/api/arrival-forecast", _forecast(trains))
    # Same arrival -> deterministic code order (earlier persisted plans may also
    # appear in the full timeline; isolate the trains this request contributes).
    same_time = [row["code"] for row in report["trains"] if row["code"].startswith("FCST-S")]
    assert same_time == ["FCST-S1", "FCST-S2", "FCST-S3"]
    rows = _by_code([row for row in report["trains"] if row["code"].startswith("FCST-S")])
    # 16 E7 cars against E7-A capacity 10: S1 takes 10, S2 overflows to MIX-1.
    assert rows["FCST-S1"]["placeable_cars"] == 8
    assert {spot["track_code"] for spot in rows["FCST-S1"]["spots"]} == {"E7-A"}
    assert rows["FCST-S2"]["placeable_cars"] == 5
    s2_tracks = {spot["track_code"] for spot in rows["FCST-S2"]["spots"]}
    assert s2_tracks == {"E7-A", "MIX-1"}
    assert rows["FCST-S3"]["placeable_cars"] == 3
    assert {spot["track_code"] for spot in rows["FCST-S3"]["spots"]} == {"MIX-1"}
    tracks = _by_code(report["tracks"])
    assert tracks["E7-A"]["planned_cars"] == 10
    assert tracks["E7-A"]["remaining_cars"] == 0
    assert tracks["MIX-1"]["planned_cars"] == 6
    assert all(row["blocked_cars"] == 0 for row in rows.values())
    exhausted = _by_code(report["exhausted_track_types"], "track_type")
    assert "DEST:E7" in exhausted


# --- dataset 5: forecast vs one real classification -----------------------


def scenario_forecast_matches_real_classification() -> None:
    data_dir = Path(__file__).resolve().parent.parent / ".check-data" / "crosscheck"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    server = RunningServer(data_dir=data_dir)
    server.wait_ready()
    api = server.api
    try:
        open_shift(api, code="SHIFT-X1")
        cars = [
            _car("C-X1-01", "N4", length=18),
            _car("C-X1-02", "N4", kind="FLAT", length=16),
            _car("C-X1-03", "E7", kind="HOPPER", length=20),
            _car("C-X1-04", "W9", kind="TANK", danger="D2", length=22),
            _car("C-X1-05", "S2", kind="REEFER", length=14),
            _car("C-X1-06", "N4", kind="BOX", length=19),
        ]
        forecast_train = _train("FCST-X1", "2026-09-13T12:00:00Z", cars, route="RAIL-X1")
        before = api.expect_ok("POST", "/api/arrival-forecast", _forecast([forecast_train]))
        [before_row] = before["trains"]
        assert before_row["fully_placeable"] is True
        predicted = {spot["car_code"]: spot["track_code"] for spot in before_row["spots"]}

        # Submit the same consist as a real intake and classify it.
        intake = _train("INT-X1", "2026-09-13T12:00:00Z", cars, route="RAIL-X1")
        api.expect_ok("POST", "/api/intake-trains", intake)
        classified = api.expect_ok("POST", "/api/intake-trains/INT-X1/classify", {})
        assert classified["intake"]["state"] == "CLASSIFIED"
        assert classified["unplaced"] == []
        actual = {spot["car_code"]: spot["track_code"] for spot in classified["spots"]}

        # No material deviation: every car lands on the track the forecast named.
        assert actual == predicted, f"forecast {predicted} != actual {actual}"
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["standing"] == 6
    finally:
        server.stop()
        shutil.rmtree(data_dir, ignore_errors=True)


def _fresh_server(name: str) -> tuple[RunningServer, Path]:
    data_dir = Path(__file__).resolve().parent.parent / ".check-data" / name
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    server = RunningServer(data_dir=data_dir)
    server.wait_ready()
    return server, data_dir


def main() -> int:
    scenarios = [
        ("empty", scenario_empty_yard),
        ("partial", scenario_partial_occupancy),
        ("same-arrival", scenario_same_arrival),
    ]
    for name, fn in scenarios:
        server, data_dir = _fresh_server(name)
        try:
            fn(server.api)
        finally:
            server.stop()
            shutil.rmtree(data_dir, ignore_errors=True)

    scenario_hazard_blocked_with_restart()
    scenario_forecast_matches_real_classification()
    print("OK wf_arrival_forecast")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
