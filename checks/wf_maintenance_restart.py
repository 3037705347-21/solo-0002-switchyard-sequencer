"""Workflow check: maintenance window state survives a service restart."""

from __future__ import annotations

import tempfile
from pathlib import Path

from support import ApiClient, RunningServer


def phase_one(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-13", "dispatcher": "MW", "opened_at": "2026-09-10T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-57",
            "route": "RAIL-57",
            "arrival_at": "2026-09-10T09:00:00Z",
            "cars": [
                {
                    "code": "C-N4-58",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-57/classify", {})
    api.expect_ok(
        "POST",
        "/api/maintenance-windows",
        {
            "code": "MW-04",
            "track_code": "N4-A",
            "planned_start": "2026-09-15T00:00:00Z",
            "planned_end": "2026-09-16T00:00:00Z",
            "reason": "tamping",
            "owner": "PARK",
        },
    )
    frozen = api.expect_ok("POST", "/api/maintenance-windows/MW-04/freeze", {})
    assert frozen["window"]["state"] == "FROZEN"


def phase_two(api: ApiClient) -> None:
    # the frozen window and its affected-plan snapshot are readable after restart
    window = api.expect_ok("GET", "/api/maintenance-windows/MW-04")["window"]
    assert window["state"] == "FROZEN"
    assert window["track_code"] == "N4-A"
    assert window["reason"] == "tamping"
    assert window["owner"] == "PARK"
    assert window["frozen_at"]
    kinds = [plan["kind"] for plan in window["affected_plans"]]
    assert "standing_car" in kinds
    assert "C-N4-58" in [plan["code"] for plan in window["affected_plans"]]
    listing = api.expect_ok("GET", "/api/maintenance-windows")
    assert [item["code"] for item in listing["windows"]] == ["MW-04"]
    # the freeze on the track itself also survived the restart
    yard = api.expect_ok("GET", "/api/yard")
    track_states = {item["code"]: item["state"] for item in yard["metrics"]["track_metrics"]}
    assert track_states["N4-A"] == "RESTRICTED"
    blocked = api.expect_error("POST", "/api/shifts/SHIFT-13/close", {})
    assert "maintenance_window" in [item["kind"] for item in blocked["details"]["blockers"]]
    # the lifecycle keeps working on the reloaded state
    cancelled = api.expect_ok("POST", "/api/maintenance-windows/MW-04/cancel", {})
    assert cancelled["window"]["state"] == "CANCELLED"
    yard2 = api.expect_ok("GET", "/api/yard")
    track_states2 = {item["code"]: item["state"] for item in yard2["metrics"]["track_metrics"]}
    assert track_states2["N4-A"] == "OPERATIONAL"
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-13/close", {})
    assert closed["shift"]["state"] == "CLOSED"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="switchyard-restart-") as tmp:
        data_dir = Path(tmp) / "data"
        server = RunningServer(data_dir)
        try:
            server.wait_ready()
            phase_one(server.api)
        finally:
            server.stop()
        restarted = RunningServer(data_dir)
        try:
            restarted.wait_ready()
            phase_two(restarted.api)
        finally:
            restarted.stop()
    print("OK wf_maintenance_restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
