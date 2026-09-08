"""Workflow check: blocked closure then a clean shift handoff."""

from __future__ import annotations

from support import ApiClient, run_check


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-04", "dispatcher": "KE", "opened_at": "2026-09-08T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-41",
            "route": "RAIL-41",
            "arrival_at": "2026-09-08T09:30:00Z",
            "cars": [
                {
                    "code": "C-N4-41",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-S2-41",
                    "kind": "HOPPER",
                    "destination": "S2",
                    "loaded": True,
                    "length_m": 20,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-41/classify", {})
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-42",
            "route": "RAIL-42",
            "arrival_at": "2026-09-08T09:45:00Z",
            "cars": [
                {
                    "code": "C-N4-42",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    blocked = api.expect_error("POST", "/api/shifts/SHIFT-04/close", {})
    assert blocked["code"] == "RESOURCE_BUSY"
    api.expect_ok("POST", "/api/intake-trains/INT-42/classify", {})
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-04/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    assert closed["metrics"]["car_state_counts"]["standing"] == 3
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["active_shift"] == "NONE"
    assert yard["metrics"]["car_state_counts"]["standing"] == 3
    shift = api.expect_ok("GET", "/api/shifts/SHIFT-04")
    kinds = [event["kind"] for event in shift["events"]]
    assert "SHIFT_CLOSED" in kinds


if __name__ == "__main__":
    raise SystemExit(run_check("wf_close_shift", run))
