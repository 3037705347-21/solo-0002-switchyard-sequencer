"""Workflow check: track reorganization work orders.

Covers deep-position retrieval, cross-track adjustment with a server restart
(resume from the persisted work order), target-order restacking, and transfer
bay capacity overflow.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from support import ApiClient, RunningServer


def open_shift(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-10", "dispatcher": "MA", "opened_at": "2026-09-13T08:00:00Z"},
    )


def intake(api: ApiClient, code: str, cars: list[dict[str, object]]) -> None:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {"code": code, "route": f"RAIL-{code}", "arrival_at": "2026-09-13T09:00:00Z", "cars": cars},
    )
    api.expect_ok("POST", f"/api/intake-trains/{code}/classify", {})


def car(code: str, destination: str = "N4") -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": True,
        "length_m": 18,
        "danger_class": "NONE",
    }


def track_stack(api: ApiClient, track_code: str) -> list[str]:
    yard = api.expect_ok("GET", "/api/yard")
    for item in yard["metrics"]["track_metrics"]:
        if item["code"] == track_code:
            return list(item["stack"])
    raise AssertionError(f"no track metrics for {track_code}")


def bay_metrics(api: ApiClient, bay_code: str = "X1") -> dict[str, object]:
    yard = api.expect_ok("GET", "/api/yard")
    for item in yard["metrics"]["transfer_bays"]:
        if item["code"] == bay_code:
            return dict(item)
    raise AssertionError(f"no bay metrics for {bay_code}")


def scenario_deep_retrieval(api: ApiClient) -> None:
    intake(api, "INT-101", [car(f"C-DP-0{index}") for index in range(1, 5)])
    assert track_stack(api, "N4-A") == ["C-DP-01", "C-DP-02", "C-DP-03", "C-DP-04"]
    created = api.expect_ok(
        "POST",
        "/api/reorder-orders",
        {"code": "RO-101", "mode": "MOVES", "moves": [{"car_code": "C-DP-01", "to_track": "MIX-1"}]},
    )
    order = created["reorder_order"]
    assert order["state"] == "QUEUED"
    assert order["mode"] == "MOVES"
    verbs = [step["verb"] for step in order["steps"]]
    assert verbs == ["BUFFER", "BUFFER", "BUFFER", "EXTRACT", "RETURN", "RETURN", "RETURN", "RETURN"]
    assert order["steps"][3] == {
        "verb": "EXTRACT",
        "car_code": "C-DP-01",
        "source_code": "N4-A",
        "target_code": "X1",
    }
    assert order["steps"][4] == {
        "verb": "RETURN",
        "car_code": "C-DP-01",
        "source_code": "X1",
        "target_code": "MIX-1",
    }
    assert order["expected_locations"] == {"C-DP-01": "MIX-1"}
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 0
    assert yard["metrics"]["active_reorder_orders"] == ["RO-101"]
    assert yard["metrics"]["active_outbounds"] == []
    advanced = api.expect_ok("POST", "/api/reorder-orders/RO-101/advance", {"steps": 8})
    assert advanced["completed"] is True
    assert advanced["reorder_order"]["state"] == "COMPLETED"
    records = advanced["reorder_order"]["step_records"]
    assert len(records) == 8
    assert [record["step_index"] for record in records] == list(range(8))
    assert records[3]["location_after"] == "X1"
    assert records[4]["location_after"] == "MIX-1"
    assert records[7]["location_after"] == "N4-A"
    assert track_stack(api, "N4-A") == ["C-DP-02", "C-DP-03", "C-DP-04"]
    assert track_stack(api, "MIX-1") == ["C-DP-01"]
    assert bay_metrics(api)["cars"] == 0
    yard = api.expect_ok("GET", "/api/yard")
    counts = yard["metrics"]["car_state_counts"]
    assert counts["standing"] == 4
    assert counts["reserved"] == 0
    assert yard["metrics"]["active_reorder_orders"] == []


def scenario_cross_track_part1(api: ApiClient) -> None:
    intake(
        api,
        "INT-102",
        [car("C-XT-01"), car("C-XT-02"), car("C-XT-03"), car("C-XT-04", "E7"), car("C-XT-05", "E7")],
    )
    created = api.expect_ok(
        "POST",
        "/api/reorder-orders",
        {
            "code": "RO-201",
            "mode": "MOVES",
            "moves": [
                {"car_code": "C-XT-02", "to_track": "MIX-1"},
                {"car_code": "C-XT-05", "to_track": "MIX-1"},
            ],
        },
    )
    order = created["reorder_order"]
    assert len(order["steps"]) == 6
    assert order["moves"] == [
        {"car_code": "C-XT-02", "source_code": "N4-A", "target_code": "MIX-1"},
        {"car_code": "C-XT-05", "source_code": "E7-A", "target_code": "MIX-1"},
    ]
    advanced = api.expect_ok("POST", "/api/reorder-orders/RO-201/advance", {"steps": 3})
    assert advanced["completed"] is False
    assert advanced["reorder_order"]["state"] == "RUNNING"
    assert advanced["reorder_order"]["current_step"] == 3
    assert bay_metrics(api)["top_car"] == "C-XT-03"


def scenario_cross_track_part2(api: ApiClient) -> None:
    view = api.expect_ok("GET", "/api/reorder-orders/RO-201")
    order = view["reorder_order"]
    assert order["state"] == "RUNNING"
    assert order["current_step"] == 3
    assert len(order["step_records"]) == 3
    assert order["step_records"][2]["location_after"] == "MIX-1"
    assert bay_metrics(api)["top_car"] == "C-XT-03"
    advanced = api.expect_ok("POST", "/api/reorder-orders/RO-201/advance", {"steps": 3})
    assert advanced["completed"] is True
    records = advanced["reorder_order"]["step_records"]
    assert [record["step_index"] for record in records] == list(range(6))
    assert track_stack(api, "N4-A") == ["C-DP-02", "C-DP-03", "C-DP-04", "C-XT-01", "C-XT-03"]
    assert track_stack(api, "E7-A") == ["C-XT-04"]
    assert track_stack(api, "MIX-1") == ["C-DP-01", "C-XT-02", "C-XT-05"]
    assert bay_metrics(api)["cars"] == 0
    events = api.expect_ok("GET", "/api/shifts/SHIFT-10")["events"]
    started = [event for event in events if event["kind"] == "REORDER_STARTED" and "RO-201" in event["message"]]
    assert len(started) == 1


def scenario_target_order(api: ApiClient) -> None:
    created = api.expect_ok(
        "POST",
        "/api/reorder-orders",
        {
            "code": "RO-202",
            "mode": "TARGET_ORDER",
            "track_code": "MIX-1",
            "target_order": ["C-XT-05", "C-XT-02", "C-DP-01"],
        },
    )
    order = created["reorder_order"]
    assert order["mode"] == "TARGET_ORDER"
    assert order["track_code"] == "MIX-1"
    staging_targets = {move["target_code"] for move in order["moves"]}
    assert "HAZ-1" in staging_targets
    assert len(order["steps"]) == 18
    assert order["expected_locations"] == {"C-XT-05": "MIX-1", "C-XT-02": "MIX-1", "C-DP-01": "MIX-1"}
    advanced = api.expect_ok("POST", "/api/reorder-orders/RO-202/advance", {"steps": 18})
    assert advanced["completed"] is True
    assert track_stack(api, "MIX-1") == ["C-XT-05", "C-XT-02", "C-DP-01"]
    assert track_stack(api, "HAZ-1") == []
    assert bay_metrics(api)["cars"] == 0


def scenario_capacity_overflow(api: ApiClient) -> None:
    intake(api, "INT-103", [car(f"C-OV-1{index}") for index in range(1, 6)])
    intake(api, "INT-104", [car(f"C-OV-2{index}") for index in range(1, 9)])
    yard = api.expect_ok("GET", "/api/yard")
    for item in yard["metrics"]["track_metrics"]:
        if item["code"] == "N4-A":
            assert item["cars"] == 10
        if item["code"] == "MIX-1":
            assert item["cars"] == 11
    error = api.expect_error(
        "POST",
        "/api/reorder-orders",
        {"code": "RO-301", "mode": "MOVES", "moves": [{"car_code": "C-XT-05", "to_track": "HAZ-1"}]},
    )
    assert error["code"] == "VALIDATION_ERROR"
    assert "needs 11 slots but has 10" in error["message"]
    assert error["details"]["reorder"] == ["buffer-overflow"]
    missing = api.expect_error("GET", "/api/reorder-orders/RO-301")
    assert missing["code"] == "NOT_FOUND"
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["active_reorder_orders"] == []
    assert yard["metrics"]["car_state_counts"]["reserved"] == 0
    created = api.expect_ok(
        "POST",
        "/api/reorder-orders",
        {"code": "RO-302", "mode": "MOVES", "moves": [{"car_code": "C-OV-28", "to_track": "HAZ-1"}]},
    )
    assert len(created["reorder_order"]["steps"]) == 2
    advanced = api.expect_ok("POST", "/api/reorder-orders/RO-302/advance", {"steps": 2})
    assert advanced["completed"] is True
    assert track_stack(api, "HAZ-1") == ["C-OV-28"]
    yard = api.expect_ok("GET", "/api/yard")
    counts = yard["metrics"]["car_state_counts"]
    assert counts["standing"] == 22
    assert counts["reserved"] == 0
    assert counts["assembled"] == 0
    assert yard["metrics"]["active_outbounds"] == []
    assert bay_metrics(api)["cars"] == 0


def scenario_closure_consistency(api: ApiClient) -> None:
    created = api.expect_ok(
        "POST",
        "/api/reorder-orders",
        {"code": "RO-401", "mode": "MOVES", "moves": [{"car_code": "C-OV-28", "to_track": "MIX-1"}]},
    )
    assert created["reorder_order"]["state"] == "QUEUED"
    blocked = api.expect_error("POST", "/api/shifts/SHIFT-10/close", {})
    assert blocked["code"] == "RESOURCE_BUSY"
    blocker_kinds = {item["kind"] for item in blocked["details"]["blockers"]}
    assert "reorder_order" in blocker_kinds
    advanced = api.expect_ok("POST", "/api/reorder-orders/RO-401/advance", {"steps": 2})
    assert advanced["completed"] is True
    assert track_stack(api, "HAZ-1") == []
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-10/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    assert closed["metrics"]["car_state_counts"]["standing"] == 22
    assert closed["metrics"]["car_state_counts"]["reserved"] == 0


def run() -> int:
    temp_dir = tempfile.TemporaryDirectory(prefix="switchyard-reorder-")
    data_dir = Path(temp_dir.name) / "data"
    server = RunningServer(data_dir)
    try:
        server.wait_ready()
        open_shift(server.api)
        scenario_deep_retrieval(server.api)
        scenario_cross_track_part1(server.api)
    finally:
        server.stop()
    restarted = RunningServer(data_dir)
    try:
        restarted.wait_ready()
        scenario_cross_track_part2(restarted.api)
        scenario_target_order(restarted.api)
        scenario_capacity_overflow(restarted.api)
        scenario_closure_consistency(restarted.api)
        events = restarted.api.expect_ok("GET", "/api/shifts/SHIFT-10")["events"]
        kinds = {event["kind"] for event in events}
        assert {"REORDER_PLANNED", "REORDER_STARTED", "REORDER_ADVANCED", "REORDER_COMPLETED"} <= kinds
    finally:
        restarted.stop()
        temp_dir.cleanup()
    print("OK wf_reorder_order")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
