"""Workflow check: depart with optional time, note, and on-site confirmation.

Covers four departure situations and verifies every response stays consistent
with later yard, shift-event, and closure-snapshot queries:

1. normal departure with an empty body (server records the time);
2. noted departure carrying an explicit time, late reason, and confirmer;
3. duplicate departure which must stay a conflict, never a second success;
4. obviously unreasonable supplied times which must be rejected without state
   change, while assembled-but-incomplete trains still cannot depart.
"""

from __future__ import annotations

from support import ApiClient, run_check
from switchyard.domain.timeutil import iso_plus_seconds, now_iso, parse_iso

SHIFT = "SHIFT-05"
INTAKE = "INT-51"
CAR_CODES = ["C-N4-51", "C-N4-52", "C-N4-53", "C-N4-54"]
OUTBOUND_CODES = ["OB-51", "OB-52", "OB-53", "OB-54"]


def _open_and_classify(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": SHIFT, "dispatcher": "MA", "opened_at": "2026-09-08T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": INTAKE,
            "route": "RAIL-51",
            "arrival_at": "2026-09-08T09:20:00Z",
            "cars": [
                {
                    "code": code,
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
                for code in CAR_CODES
            ],
        },
    )
    api.expect_ok("POST", f"/api/intake-trains/{INTAKE}/classify", {})


def _build_ready_train(api: ApiClient, outbound_code: str, car_code: str) -> str:
    """Create, sequence, and fully advance a one-car outbound; return ready time."""
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": outbound_code, "destination": "N4", "car_codes": [car_code]},
    )
    sequenced = api.expect_ok(
        "POST",
        f"/api/outbound-trains/{outbound_code}/sequencer",
        {"transfer_code": "X1"},
    )
    run_code = sequenced["pull_run"]["code"]
    total_steps = len(sequenced["pull_run"]["steps"])
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": total_steps})
    assert advanced["completed"] is True
    assert advanced["outbound"]["state"] == "READY"
    completed_at = advanced["pull_run"]["completed_at"]
    assert completed_at
    return str(completed_at)


def _yard_metrics(api: ApiClient) -> dict:
    return api.expect_ok("GET", "/api/yard")["metrics"]


def _yard_departed(api: ApiClient) -> dict[str, dict]:
    return {item["code"]: item for item in _yard_metrics(api)["departed_trains"]}


def _departed_events(api: ApiClient) -> list[dict]:
    shift = api.expect_ok("GET", f"/api/shifts/{SHIFT}")
    return [event for event in shift["events"] if event["kind"] == "TRAIN_DEPARTED"]


def run(api: ApiClient) -> None:
    _open_and_classify(api)

    # ---- case 1: normal departure, empty body keeps the default server time ----
    _build_ready_train(api, "OB-51", "C-N4-51")
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-51/depart", {})
    outbound = departed["outbound"]
    assert outbound["state"] == "DEPARTED"
    assert outbound["departed_at"]
    assert outbound["note"] == ""
    assert outbound["late_reason"] == ""
    assert outbound["confirmed_by"] == ""
    assert departed["departed_car_count"] == 1
    yard_entry = _yard_departed(api)["OB-51"]
    assert yard_entry["departed_at"] == outbound["departed_at"]
    assert yard_entry["note"] == ""
    assert yard_entry["car_count"] == 1

    # ---- case 2: noted departure with explicit actual time and confirmation ----
    completed_at = _build_ready_train(api, "OB-52", "C-N4-52")
    actual_at = iso_plus_seconds(completed_at, 45)
    noted = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-52/depart",
        {
            "departed_at": actual_at,
            "note": "waiting on signal clearance",
            "late_reason": "downstream signal fault",
            "confirmed_by": "YARD-FOREMAN-KE",
        },
    )
    noted_outbound = noted["outbound"]
    assert noted_outbound["state"] == "DEPARTED"
    assert noted_outbound["departed_at"] == actual_at
    assert noted_outbound["note"] == "waiting on signal clearance"
    assert noted_outbound["late_reason"] == "downstream signal fault"
    assert noted_outbound["confirmed_by"] == "YARD-FOREMAN-KE"
    assert _yard_departed(api)["OB-52"] == {
        "code": "OB-52",
        "destination": "N4",
        "departed_at": actual_at,
        "car_count": 1,
        "note": "waiting on signal clearance",
        "late_reason": "downstream signal fault",
        "confirmed_by": "YARD-FOREMAN-KE",
    }
    ob52_events = [event for event in _departed_events(api) if "OB-52" in event["message"]]
    assert len(ob52_events) == 1
    assert ob52_events[0]["payload"]["departed_at"] == actual_at
    assert ob52_events[0]["payload"]["note"] == "waiting on signal clearance"
    assert ob52_events[0]["payload"]["late_reason"] == "downstream signal fault"
    assert ob52_events[0]["payload"]["confirmed_by"] == "YARD-FOREMAN-KE"

    # ---- case 3: duplicate departure is a conflict, never a second success ----
    metrics_before = _yard_metrics(api)
    duplicate = api.expect_error("POST", "/api/outbound-trains/OB-52/depart", {})
    assert duplicate["code"] == "CONFLICT"
    assert duplicate["details"]["departed_at"] == actual_at
    metrics_after = _yard_metrics(api)
    assert metrics_after["departed_train_count"] == metrics_before["departed_train_count"] == 2
    assert metrics_after["event_count"] == metrics_before["event_count"]
    assert _yard_departed(api)["OB-52"]["departed_at"] == actual_at
    assert len(_departed_events(api)) == 2
    duplicate_again = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-52/depart",
        {"departed_at": iso_plus_seconds(actual_at, 300), "note": "retry"},
    )
    assert duplicate_again["code"] == "CONFLICT"
    assert _yard_departed(api)["OB-52"]["note"] == "waiting on signal clearance"

    # ---- case 4a: unreasonable supplied times are rejected without state change ----
    completed_at = _build_ready_train(api, "OB-53", "C-N4-53")
    bad_format = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-53/depart",
        {"departed_at": "yesterday morning"},
    )
    assert bad_format["code"] == "VALIDATION_ERROR"
    assert "departed_at" in bad_format["fields"]
    before_assembly = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-53/depart",
        {"departed_at": iso_plus_seconds(completed_at, -600)},
    )
    assert before_assembly["code"] == "VALIDATION_ERROR"
    assert "departed_at" in before_assembly["fields"]
    far_future = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-53/depart",
        {"departed_at": "2030-01-01T00:00:00Z", "note": "impossible"},
    )
    assert far_future["code"] == "VALIDATION_ERROR"
    assert "departed_at" in far_future["fields"]
    metrics = _yard_metrics(api)
    assert "OB-53" in metrics["active_outbounds"]
    assert metrics["car_state_counts"]["assembled"] == 1
    assert metrics["departed_train_count"] == 2
    assert "OB-53" not in _yard_departed(api)

    # rejected timestamps must not block a later valid departure
    recovered = api.expect_ok("POST", "/api/outbound-trains/OB-53/depart", {})
    recovered_outbound = recovered["outbound"]
    assert recovered_outbound["state"] == "DEPARTED"
    assert recovered_outbound["note"] == ""
    assert recovered_outbound["late_reason"] == ""
    skew = abs((parse_iso(recovered_outbound["departed_at"]) - parse_iso(now_iso())).total_seconds())
    assert skew <= 120
    assert _yard_departed(api)["OB-53"]["departed_at"] == recovered_outbound["departed_at"]

    # ---- case 4b: formed but not meeting assembly conditions still cannot depart ----
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-54", "destination": "N4", "car_codes": ["C-N4-54"]},
    )
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-54/sequencer",
        {"transfer_code": "X1"},
    )
    planned_run = sequenced["pull_run"]["code"]
    assert sequenced["outbound"]["state"] == "PLANNED"
    not_ready = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-54/depart",
        {"departed_at": now_iso(), "confirmed_by": "YARD-FOREMAN-KE"},
    )
    assert not_ready["code"] == "VALIDATION_ERROR"
    metrics = _yard_metrics(api)
    assert "OB-54" in metrics["active_outbounds"]
    assert metrics["car_state_counts"]["reserved"] == 1
    assert metrics["car_state_counts"]["assembled"] == 0
    assert metrics["departed_train_count"] == 3
    blocked_close = api.expect_error("POST", f"/api/shifts/{SHIFT}/close", {})
    assert blocked_close["code"] == "RESOURCE_BUSY"

    # once assembly actually completes, departure succeeds and closure is clean
    advanced = api.expect_ok("POST", f"/api/pull-runs/{planned_run}/advance", {"steps": 10})
    assert advanced["completed"] is True
    last = api.expect_ok("POST", "/api/outbound-trains/OB-54/depart", {})
    assert last["outbound"]["state"] == "DEPARTED"

    metrics = _yard_metrics(api)
    assert metrics["car_state_counts"]["departed"] == 4
    assert metrics["departed_train_count"] == 4
    departed_rows = {row["code"]: row for row in metrics["departed_trains"]}
    assert set(departed_rows) == set(OUTBOUND_CODES)

    closed = api.expect_ok("POST", f"/api/shifts/{SHIFT}/close", {})
    snapshot_metrics = closed["snapshot"]["metrics"]
    assert snapshot_metrics["car_state_counts"]["departed"] == 4
    assert snapshot_metrics["departed_train_count"] == 4
    assert snapshot_metrics["departed_trains"] == metrics["departed_trains"]

    events = _departed_events(api)
    assert len(events) == 4
    by_train = {}
    for event in events:
        for code in OUTBOUND_CODES:
            if code in event["message"]:
                by_train[code] = event["payload"]
    assert by_train["OB-51"]["note"] == ""
    assert by_train["OB-51"]["confirmed_by"] == ""
    assert by_train["OB-52"]["departed_at"] == actual_at
    assert by_train["OB-52"]["late_reason"] == "downstream signal fault"
    for code in OUTBOUND_CODES:
        assert by_train[code]["departed_at"] == departed_rows[code]["departed_at"]
        assert by_train[code]["note"] == departed_rows[code]["note"]
        assert by_train[code]["confirmed_by"] == departed_rows[code]["confirmed_by"]


if __name__ == "__main__":
    raise SystemExit(run_check("wf_depart_details", run))
