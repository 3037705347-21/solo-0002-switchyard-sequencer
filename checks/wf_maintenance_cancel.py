"""Workflow check: cancelling a frozen maintenance window restores a consistent yard."""

from __future__ import annotations

from support import ApiClient, run_check


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-12", "dispatcher": "MW", "opened_at": "2026-09-10T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-55",
            "route": "RAIL-55",
            "arrival_at": "2026-09-10T09:00:00Z",
            "cars": [
                {
                    "code": "C-N4-56",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-55/classify", {})
    assert classified["spots"][0]["track_code"] == "N4-A"
    scheduled = api.expect_ok(
        "POST",
        "/api/maintenance-windows",
        {
            "code": "MW-03",
            "track_code": "N4-A",
            "planned_start": "2026-09-15T00:00:00Z",
            "planned_end": "2026-09-16T00:00:00Z",
            "reason": "frog replacement",
            "owner": "LEE",
        },
    )
    assert scheduled["window"]["state"] == "SCHEDULED"
    frozen = api.expect_ok("POST", "/api/maintenance-windows/MW-03/freeze", {})
    assert frozen["window"]["state"] == "FROZEN"
    assert frozen["window"]["prior_track_state"] == "OPERATIONAL"
    yard = api.expect_ok("GET", "/api/yard")
    track_states = {item["code"]: item["state"] for item in yard["metrics"]["track_metrics"]}
    assert track_states["N4-A"] == "RESTRICTED"
    # cancel the window: the freeze is lifted and the track returns to its prior state
    cancelled = api.expect_ok("POST", "/api/maintenance-windows/MW-03/cancel", {})
    assert cancelled["window"]["state"] == "CANCELLED"
    assert cancelled["window"]["cancelled_at"]
    yard2 = api.expect_ok("GET", "/api/yard")
    track_states2 = {item["code"]: item["state"] for item in yard2["metrics"]["track_metrics"]}
    assert track_states2["N4-A"] == "OPERATIONAL"
    # intake allocation is consistent again: new N4 cars return to N4-A
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-56",
            "route": "RAIL-56",
            "arrival_at": "2026-09-10T09:30:00Z",
            "cars": [
                {
                    "code": "C-N4-57",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    classified2 = api.expect_ok("POST", "/api/intake-trains/INT-56/classify", {})
    assert classified2["spots"][0]["track_code"] == "N4-A"
    # plans that existed before the freeze were never touched and still sequence
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-53", "destination": "N4", "car_codes": ["C-N4-56"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-53/sequencer", {"transfer_code": "X1"})
    assert sequenced["pull_run"]["state"] == "QUEUED"
    assert sequenced["outbound"]["state"] == "PLANNED"
    # a cancelled window is terminal and stays in the history
    again = api.expect_error("POST", "/api/maintenance-windows/MW-03/cancel", {})
    assert again["code"] == "STATE_TRANSITION"
    refreeze = api.expect_error("POST", "/api/maintenance-windows/MW-03/freeze", {})
    assert refreeze["code"] == "STATE_TRANSITION"
    history = api.expect_ok("GET", "/api/maintenance-windows/MW-03")
    assert history["window"]["state"] == "CANCELLED"
    assert history["window"]["prior_track_state"] == "OPERATIONAL"
    listing = api.expect_ok("GET", "/api/maintenance-windows")
    assert [window["code"] for window in listing["windows"]] == ["MW-03"]
    # the freed track no longer blocks closure
    blockers = api.expect_ok("GET", "/api/yard")["blockers"]
    assert all(item["kind"] != "maintenance_window" for item in blockers)


if __name__ == "__main__":
    raise SystemExit(run_check("wf_maintenance_cancel", run))
