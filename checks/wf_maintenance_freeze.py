"""Workflow check: scheduling a maintenance window and freezing intake allocation."""

from __future__ import annotations

from support import ApiClient, run_check


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-10", "dispatcher": "MW", "opened_at": "2026-09-10T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-51",
            "route": "RAIL-51",
            "arrival_at": "2026-09-10T09:00:00Z",
            "cars": [
                {
                    "code": "C-N4-51",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-51/classify", {})
    assert classified["spots"][0]["track_code"] == "N4-A"
    # unfinished classification and pull planning remain on site
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-52",
            "route": "RAIL-52",
            "arrival_at": "2026-09-10T09:30:00Z",
            "cars": [
                {
                    "code": "C-N4-52",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    # an open intake for an unrelated destination must stay out of the freeze list
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-90",
            "route": "RAIL-90",
            "arrival_at": "2026-09-10T09:45:00Z",
            "cars": [
                {
                    "code": "C-E7-90",
                    "kind": "BOX",
                    "destination": "E7",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-51", "destination": "N4", "car_codes": ["C-N4-51"]},
    )
    scheduled = api.expect_ok(
        "POST",
        "/api/maintenance-windows",
        {
            "code": "MW-01",
            "track_code": "N4-A",
            "planned_start": "2026-09-15T00:00:00Z",
            "planned_end": "2026-09-16T00:00:00Z",
            "reason": "rail grinding",
            "owner": "LEE",
        },
    )
    assert scheduled["window"]["state"] == "SCHEDULED"
    assert scheduled["window"]["owner"] == "LEE"
    duplicate = api.expect_error(
        "POST",
        "/api/maintenance-windows",
        {
            "code": "MW-02",
            "track_code": "N4-A",
            "planned_start": "2026-09-17T00:00:00Z",
            "planned_end": "2026-09-18T00:00:00Z",
            "reason": "overlap",
            "owner": "LEE",
        },
    )
    assert duplicate["code"] == "CONFLICT"
    frozen = api.expect_ok("POST", "/api/maintenance-windows/MW-01/freeze", {})
    assert frozen["window"]["state"] == "FROZEN"
    assert frozen["window"]["prior_track_state"] == "OPERATIONAL"
    kinds = [plan["kind"] for plan in frozen["affected_plans"]]
    assert "intake" in kinds
    assert "outbound" in kinds
    assert "standing_car" in kinds
    codes = [plan["code"] for plan in frozen["affected_plans"]]
    assert "INT-52" in codes
    assert "OB-51" in codes
    assert "C-N4-51" in codes
    assert "INT-90" not in codes
    intake_entries = [plan for plan in frozen["affected_plans"] if plan["kind"] == "intake"]
    assert [plan["code"] for plan in intake_entries] == ["INT-52"]
    again = api.expect_error("POST", "/api/maintenance-windows/MW-01/freeze", {})
    assert again["code"] == "STATE_TRANSITION"
    yard = api.expect_ok("GET", "/api/yard")
    track_states = {item["code"]: item["state"] for item in yard["metrics"]["track_metrics"]}
    assert track_states["N4-A"] == "RESTRICTED"
    # the frozen track no longer receives new intake assignments
    classified2 = api.expect_ok("POST", "/api/intake-trains/INT-52/classify", {})
    assert classified2["spots"][0]["track_code"] == "MIX-1"
    # the unrelated intake is untouched by the freeze and classifies normally
    classified3 = api.expect_ok("POST", "/api/intake-trains/INT-90/classify", {})
    assert classified3["spots"][0]["track_code"] == "E7-A"
    # a frozen window blocks shift closure through the existing blocker rules
    blocked = api.expect_error("POST", "/api/shifts/SHIFT-10/close", {})
    assert blocked["code"] == "RESOURCE_BUSY"
    blocker_kinds = [item["kind"] for item in blocked["details"]["blockers"]]
    assert "maintenance_window" in blocker_kinds


if __name__ == "__main__":
    raise SystemExit(run_check("wf_maintenance_freeze", run))
