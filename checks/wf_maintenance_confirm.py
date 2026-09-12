"""Workflow check: clear a frozen track, confirm maintenance, and restore operation."""

from __future__ import annotations

from support import ApiClient, run_check


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-11", "dispatcher": "MW", "opened_at": "2026-09-10T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-53",
            "route": "RAIL-53",
            "arrival_at": "2026-09-10T09:00:00Z",
            "cars": [
                {
                    "code": "C-N4-53",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-N4-54",
                    "kind": "HOPPER",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 20,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-53/classify", {})
    assert {spot["track_code"] for spot in classified["spots"]} == {"N4-A"}
    api.expect_ok(
        "POST",
        "/api/maintenance-windows",
        {
            "code": "MW-02",
            "track_code": "N4-A",
            "planned_start": "2026-09-15T00:00:00Z",
            "planned_end": "2026-09-16T00:00:00Z",
            "reason": "switch motor swap",
            "owner": "PARK",
        },
    )
    frozen = api.expect_ok("POST", "/api/maintenance-windows/MW-02/freeze", {})
    assert frozen["window"]["state"] == "FROZEN"
    # confirmation is refused while cars still stand on the track
    busy = api.expect_error("POST", "/api/maintenance-windows/MW-02/confirm", {})
    assert busy["code"] == "RESOURCE_BUSY"
    assert busy["details"]["cars"] == ["C-N4-53", "C-N4-54"]
    # start clearing the track: pull the bottom car, parking the top car in the bay
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-52", "destination": "N4", "car_codes": ["C-N4-53"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-52/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 2})
    assert advanced["completed"] is False
    # the stack is empty now, but the running plan still owes the track a return
    busy_run = api.expect_error("POST", "/api/maintenance-windows/MW-02/confirm", {})
    assert busy_run["code"] == "RESOURCE_BUSY"
    assert run_code in busy_run["details"]["runs"]
    finished = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 5})
    assert finished["completed"] is True
    # the returned car is back on the track, so confirmation is still refused
    busy_again = api.expect_error("POST", "/api/maintenance-windows/MW-02/confirm", {})
    assert busy_again["details"]["cars"] == ["C-N4-54"]
    # clear the last car with a second outbound pull
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-53", "destination": "N4", "car_codes": ["C-N4-54"]},
    )
    sequenced2 = api.expect_ok("POST", "/api/outbound-trains/OB-53/sequencer", {"transfer_code": "X1"})
    run_code2 = sequenced2["pull_run"]["code"]
    cleared = api.expect_ok("POST", f"/api/pull-runs/{run_code2}/advance", {"steps": 5})
    assert cleared["completed"] is True
    confirmed = api.expect_ok("POST", "/api/maintenance-windows/MW-02/confirm", {})
    assert confirmed["window"]["state"] == "ACTIVE"
    assert confirmed["window"]["confirmed_at"]
    yard = api.expect_ok("GET", "/api/yard")
    track_states = {item["code"]: item["state"] for item in yard["metrics"]["track_metrics"]}
    assert track_states["N4-A"] == "MAINTENANCE"
    # a track in maintenance receives no new cars
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-54",
            "route": "RAIL-54",
            "arrival_at": "2026-09-10T10:00:00Z",
            "cars": [
                {
                    "code": "C-N4-55",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    classified2 = api.expect_ok("POST", "/api/intake-trains/INT-54/classify", {})
    assert classified2["spots"][0]["track_code"] != "N4-A"
    # an active window blocks closure until it is restored
    blocked = api.expect_error("POST", "/api/shifts/SHIFT-11/close", {})
    assert "maintenance_window" in [item["kind"] for item in blocked["details"]["blockers"]]
    restored = api.expect_ok("POST", "/api/maintenance-windows/MW-02/restore", {})
    assert restored["window"]["state"] == "RESTORED"
    assert restored["window"]["restored_at"]
    yard2 = api.expect_ok("GET", "/api/yard")
    track_states2 = {item["code"]: item["state"] for item in yard2["metrics"]["track_metrics"]}
    assert track_states2["N4-A"] == "OPERATIONAL"
    # the finished window stays queryable as history
    history = api.expect_ok("GET", "/api/maintenance-windows/MW-02")
    assert history["window"]["state"] == "RESTORED"
    assert history["window"]["frozen_at"]
    listing = api.expect_ok("GET", "/api/maintenance-windows")
    assert [window["code"] for window in listing["windows"]] == ["MW-02"]
    # the yard returns to a clean handoff state
    api.expect_ok("POST", "/api/outbound-trains/OB-52/depart", {})
    api.expect_ok("POST", "/api/outbound-trains/OB-53/depart", {})
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-11/close", {})
    assert closed["shift"]["state"] == "CLOSED"


if __name__ == "__main__":
    raise SystemExit(run_check("wf_maintenance_confirm", run))
