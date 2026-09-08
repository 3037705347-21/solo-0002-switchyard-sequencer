"""Workflow check: open a shift and classify an inbound train."""

from __future__ import annotations

from support import ApiClient, run_check


def intake_payload(code: str) -> dict[str, object]:
    return {
        "code": code,
        "route": "RAIL-11",
        "arrival_at": "2026-09-08T09:10:00Z",
        "cars": [
            {
                "code": "C-N4-11",
                "kind": "BOX",
                "destination": "N4",
                "loaded": True,
                "length_m": 18,
                "danger_class": "NONE",
            },
            {
                "code": "C-N4-12",
                "kind": "HOPPER",
                "destination": "N4",
                "loaded": True,
                "length_m": 20,
                "danger_class": "NONE",
            },
            {
                "code": "C-E7-11",
                "kind": "FLAT",
                "destination": "E7",
                "loaded": False,
                "length_m": 16,
                "danger_class": "NONE",
            },
            {
                "code": "C-HAZ-11",
                "kind": "TANK",
                "destination": "W9",
                "loaded": True,
                "length_m": 22,
                "danger_class": "D1",
            },
        ],
    }


def run(api: ApiClient) -> None:
    opened = api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-01", "dispatcher": "LIN", "opened_at": "2026-09-08T08:00:00Z"},
    )
    assert opened["state"] == "OPEN"
    created = api.expect_ok("POST", "/api/intake-trains", intake_payload("INT-01"))
    assert created["intake"]["state"] == "OPEN"
    assert len(created["cars"]) == 4
    classified = api.expect_ok("POST", "/api/intake-trains/INT-01/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"
    assert classified["unplaced"] == []
    assert len(classified["spots"]) == 4
    tracks = {item["car_code"]: item["track_code"] for item in classified["spots"]}
    assert tracks["C-N4-11"].startswith("N4-")
    assert tracks["C-E7-11"].startswith("E7-")
    assert tracks["C-HAZ-11"] == "HAZ-1"
    bad = api.expect_error("POST", "/api/intake-trains", intake_payload("INT-BAD"))
    assert bad["code"] == "CONFLICT"
    yard = api.expect_ok("GET", "/api/yard")
    standing = yard["metrics"]["car_state_counts"]["standing"]
    assert standing == 4


if __name__ == "__main__":
    raise SystemExit(run_check("wf_intake_classify", run))
