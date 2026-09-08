"""Workflow check: plan an outbound consist with a buffered pull sequence."""

from __future__ import annotations

from support import ApiClient, run_check


def classify_three_cars(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-02", "dispatcher": "RUI", "opened_at": "2026-09-08T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-21",
            "route": "RAIL-21",
            "arrival_at": "2026-09-08T09:00:00Z",
            "cars": [
                {
                    "code": "C-N4-21",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-BLK-21",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-N4-22",
                    "kind": "FLAT",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 16,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-21/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"


def run(api: ApiClient) -> None:
    classify_three_cars(api)
    created = api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-21", "destination": "N4", "car_codes": ["C-N4-22", "C-N4-21"]},
    )
    assert created["state"] == "DRAFT"
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-21/sequencer",
        {"transfer_code": "X1"},
    )
    pull_run = sequenced["pull_run"]
    outbound = sequenced["outbound"]
    assert pull_run["state"] == "QUEUED"
    assert outbound["state"] == "PLANNED"
    assert pull_run["outbound_code"] == "OB-21"
    verbs = [step["verb"] for step in pull_run["steps"]]
    assert verbs[0] == "PULL"
    assert "BUFFER" in verbs and "RETURN" in verbs
    reserved = api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]["reserved"]
    assert reserved == 2


if __name__ == "__main__":
    raise SystemExit(run_check("wf_outbound_sequence", run))
