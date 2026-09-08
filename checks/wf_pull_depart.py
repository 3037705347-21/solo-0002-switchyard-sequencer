"""Workflow check: execute buffer actions and depart an assembled train."""

from __future__ import annotations

from support import ApiClient, run_check


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-03", "dispatcher": "MA", "opened_at": "2026-09-08T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-31",
            "route": "RAIL-31",
            "arrival_at": "2026-09-08T09:20:00Z",
            "cars": [
                {
                    "code": "C-N4-31",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-BLK-31",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-N4-32",
                    "kind": "REEFER",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 20,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-31/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-31", "destination": "N4", "car_codes": ["C-N4-32", "C-N4-31"]},
    )
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-31/sequencer",
        {"transfer_code": "X1"},
    )
    run_code = sequenced["pull_run"]["code"]
    total_steps = len(sequenced["pull_run"]["steps"])
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": total_steps})
    assert advanced["completed"] is True
    assert advanced["pull_run"]["state"] == "COMPLETED"
    assert advanced["outbound"]["state"] == "READY"
    assert advanced["outbound"]["assembled_car_codes"] == ["C-N4-32", "C-N4-31"]
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-31/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    assert departed["departed_car_count"] == 2
    yard = api.expect_ok("GET", "/api/yard")
    standing = yard["metrics"]["car_state_counts"]["standing"]
    departed_cars = yard["metrics"]["car_state_counts"]["departed"]
    assert standing == 1
    assert departed_cars == 2


if __name__ == "__main__":
    raise SystemExit(run_check("wf_pull_depart", run))
