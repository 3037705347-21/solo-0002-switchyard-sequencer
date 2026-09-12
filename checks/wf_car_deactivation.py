"""Workflow check: vehicle deactivation (hold/retire) and recovery.

Drives the real HTTP API and covers the four required car situations:

1. Deep-position standing car on a standing track: HOLD succeeds, the car is
   excluded from new outbound plans, the decision survives a service restart,
   and recovery restores the original stack slot.
2. Buffered car (mid pull run): deactivation is rejected with an executable
   conflict list; the references stay intact and the original run completes.
3. Reserved / assembled cars: rejected while planned; the conflict actions
   (abandon outbound) release the cars, after which HOLD/RETIRE succeed.
4. Departed car: rejected permanently.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from support import ApiClient, RunningServer


def box(code: str, destination: str = "N4", length: int = 18) -> dict:
    return {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": "NONE",
    }


def deactivate_payload(car_code: str, reason: str, kind: str = "HOLD", operator: str = "LJ") -> dict:
    return {"car_code": car_code, "reason": reason, "kind": kind, "operator": operator}


def classify(api: ApiClient, intake_code: str, cars: list[dict]) -> None:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": intake_code,
            "route": "RAIL-41",
            "arrival_at": "2026-09-12T09:00:00Z",
            "cars": cars,
        },
    )
    classified = api.expect_ok("POST", f"/api/intake-trains/{intake_code}/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"


def assert_blocked(status: int, body: dict, kinds: set[str]) -> dict:
    assert status == 409 and not body.get("ok"), body
    error = body["error"]
    assert error["code"] == "RESOURCE_BUSY"
    conflicts = error["details"]["conflicts"]
    actual = {item["kind"] for item in conflicts}
    assert kinds <= actual, f"expected {kinds}, got {actual}"
    return error


def restart_with_data(data_dir: Path, old: RunningServer) -> RunningServer:
    old.stop()
    server = RunningServer(data_dir=data_dir)
    server.wait_ready()
    return server


def run_full() -> None:
    temp = tempfile.TemporaryDirectory(prefix="switchyard-dea-")
    data_dir = Path(temp.name) / "data"
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        api = server.api

        api.expect_ok(
            "POST",
            "/api/shifts",
            {"code": "SHIFT-41", "dispatcher": "LJ", "opened_at": "2026-09-12T08:00:00Z"},
        )

        # payload validation: reason/operator are mandatory
        status, body = api.request("POST", "/api/car-deactivations", {"car_code": "C-N4-41", "kind": "HOLD"})
        assert status == 422 and not body["ok"]

        # ---- 1. deep-position standing car --------------------------------
        classify(api, "INT-41", [box("C-N4-41"), box("C-N4-42"), box("C-N4-43")])
        yard = api.expect_ok("GET", "/api/yard")
        n4a = next(item for item in yard["metrics"]["track_metrics"] if item["code"] == "N4-A")
        assert n4a["top_car"] == "C-N4-43"

        result = api.expect_ok(
            "POST",
            "/api/car-deactivations",
            deactivate_payload("C-N4-41", "wheel defect found during inspection"),
        )
        record = result["deactivation"]
        assert record["code"] == "DEA-0001"
        assert record["kind"] == "HOLD" and record["status"] == "ACTIVE"
        assert record["prior_location"] == "N4-A" and record["restore_index"] == 0
        assert result["car"]["state"] == "REMOVED"
        assert result["car"]["location"] == "OUT_OF_SERVICE"
        # attribution was confirmed against every yard structure
        assert result["attribution"]["standing_track"]["track_code"] == "N4-A"
        assert result["attribution"]["standing_track"]["depth_from_top"] == 2
        assert result["conflicts"] == []
        # deep slot removed without disturbing the cars above it
        yard = api.expect_ok("GET", "/api/yard")
        n4a = next(item for item in yard["metrics"]["track_metrics"] if item["code"] == "N4-A")
        assert n4a["cars"] == 2 and n4a["top_car"] == "C-N4-43"
        assert yard["metrics"]["car_state_counts"]["removed"] == 1
        assert yard["metrics"]["deactivated_cars"] == ["C-N4-41"]

        # cannot be selected for new outbound plans, and cannot be re-held
        status, body = api.request("POST",
            "/api/outbound-trains",
            {"code": "OB-41", "destination": "N4", "car_codes": ["C-N4-41"]},
        )
        assert status == 409 and body["error"]["code"] == "RESOURCE_BUSY"
        assert "out of service" in body["error"]["message"]
        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-41", "again")
        )
        assert status == 409 and body["error"]["code"] == "CONFLICT"

        # persistence across service restart
        server = restart_with_data(data_dir, server)
        api = server.api
        listing = api.expect_ok("GET", "/api/car-deactivations")
        assert listing["active_car_codes"] == ["C-N4-41"]
        detail = api.expect_ok("GET", "/api/car-deactivations/DEA-0001")
        assert detail["deactivation"]["status"] == "ACTIVE"
        assert detail["deactivation"]["restore_index"] == 0
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["deactivated_cars"] == ["C-N4-41"]
        status, _ = api.request("POST",
            "/api/outbound-trains",
            {"code": "OB-41", "destination": "N4", "car_codes": ["C-N4-41"]},
        )
        assert status == 409

        # recovery puts the car back at its original deep slot
        recovered = api.expect_ok(
            "POST",
            "/api/car-recoveries",
            {"car_code": "C-N4-41", "reason": "wheel repaired and certified", "operator": "HX"},
        )
        assert recovered["deactivation"]["status"] == "RECOVERED"
        assert recovered["restored"] == {"track_code": "N4-A", "stack_index": 0}
        yard = api.expect_ok("GET", "/api/yard")
        n4a = next(item for item in yard["metrics"]["track_metrics"] if item["code"] == "N4-A")
        assert n4a["cars"] == 3 and n4a["top_car"] == "C-N4-43"
        assert yard["metrics"]["car_state_counts"]["removed"] == 0
        # selectable again; this draft references 41 for a later test
        created = api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-41", "destination": "N4", "car_codes": ["C-N4-41"]},
        )
        assert created["state"] == "DRAFT"
        status, body = api.request("POST",
            "/api/car-recoveries",
            {"car_code": "C-N4-41", "reason": "x", "operator": "HX"},
        )
        assert status == 409 and body["error"]["code"] == "CONFLICT"

        # ---- 2. buffered car rejected; original run still finishes --------
        classify(api, "INT-42", [box("C-N4-51"), box("C-N4-52")])
        # N4-A bottom->top: [41, 42, 43, 51, 52]
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-42", "destination": "N4", "car_codes": ["C-N4-51"]},
        )
        sequenced = api.expect_ok(
            "POST", "/api/outbound-trains/OB-42/sequencer", {"transfer_code": "X1"}
        )
        run_code = sequenced["pull_run"]["code"]
        api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
        yard = api.expect_ok("GET", "/api/yard")
        bay = yard["metrics"]["transfer_bays"][0]
        assert bay["code"] == "X1" and bay["top_car"] == "C-N4-52"

        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-52", "brake fault")
        )
        error = assert_blocked(status, body, {"pull_run"})
        conflict_kinds = {item["kind"] for item in error["details"]["conflicts"]}
        # a single executable resolution: no competing abandon/bay actions
        assert conflict_kinds == {"pull_run"}, conflict_kinds
        run_conflict = next(
            item for item in error["details"]["conflicts"] if item["kind"] == "pull_run"
        )
        assert run_conflict["action_method"] == "POST"
        assert run_conflict["action_path"] == f"/api/pull-runs/{run_code}/advance"
        assert run_conflict["action_payload"]["steps"] >= 1
        assert error["details"]["attribution"]["buffer_bay"]["bay_code"] == "X1"
        # nothing moved: bay still occupied, no car removed
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["removed"] == 0
        assert yard["metrics"]["transfer_bays"][0]["cars"] == 1

        # original plan still completes and departs
        advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})
        assert advanced["completed"] is True
        departed = api.expect_ok("POST", "/api/outbound-trains/OB-42/depart", {})
        assert departed["outbound"]["state"] == "DEPARTED"
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["transfer_bays"][0]["cars"] == 0
        # the returned blocker can now be held and the block is on record
        held = api.expect_ok(
            "POST",
            "/api/car-deactivations",
            deactivate_payload("C-N4-52", "brake fault confirmed after shift"),
        )
        assert held["deactivation"]["code"] == "DEA-0002"
        assert held["deactivation"]["blocked_attempts"] == 1
        api.expect_ok(
            "POST",
            "/api/car-recoveries",
            {"car_code": "C-N4-52", "reason": "brakes fixed", "operator": "HX"},
        )

        # ---- 3a. reserved (PLANNED) car rejected; plan still runs ---------
        classify(api, "INT-43", [box("C-N4-61")])
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-43", "destination": "N4", "car_codes": ["C-N4-61"]},
        )
        api.expect_ok("POST", "/api/outbound-trains/OB-43/sequencer", {"transfer_code": "X1"})
        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-61", "sideframe crack")
        )
        error = assert_blocked(status, body, {"outbound_plan"})
        plan_conflict = next(
            item for item in error["details"]["conflicts"] if item["kind"] == "outbound_plan"
        )
        assert plan_conflict["reference"] == "OB-43"
        assert plan_conflict["action_path"] == "/api/outbound-trains/OB-43/abandon"
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["reserved"] == 1
        # rejected deactivation leaves the plan fully executable
        advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-43/advance", {"steps": 10})
        assert advanced["completed"] is True
        assert advanced["outbound"]["state"] == "READY"
        departed = api.expect_ok("POST", "/api/outbound-trains/OB-43/depart", {})
        assert departed["departed_car_count"] == 1

        # ---- 3b. assembled car rejected; the conflict action releases it ---
        classify(api, "INT-44", [box("C-N4-71")])
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-44", "destination": "N4", "car_codes": ["C-N4-71"]},
        )
        api.expect_ok("POST", "/api/outbound-trains/OB-44/sequencer", {"transfer_code": "X1"})
        api.expect_ok("POST", "/api/pull-runs/RUN-OB-44/advance", {"steps": 10})
        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-71", "hot box alarm")
        )
        error = assert_blocked(status, body, {"outbound_assembly"})
        assembly = next(
            item for item in error["details"]["conflicts"] if item["kind"] == "outbound_assembly"
        )
        assert assembly["action_path"] == "/api/outbound-trains/OB-44/abandon"
        # execute the advertised resolution action
        abandoned = api.expect_ok("POST", "/api/outbound-trains/OB-44/abandon", {})
        assert abandoned["outbound"]["state"] == "ABANDONED"
        assert abandoned["returned_assembled"] == [{"car_code": "C-N4-71", "track_code": "N4-A"}]
        held = api.expect_ok(
            "POST",
            "/api/car-deactivations",
            deactivate_payload("C-N4-71", "hot box alarm confirmed"),
        )
        assert held["deactivation"]["code"] == "DEA-0003"
        assert held["deactivation"]["blocked_attempts"] == 1
        api.expect_ok(
            "POST",
            "/api/car-recoveries",
            {"car_code": "C-N4-71", "reason": "bearing replaced", "operator": "HX"},
        )

        # ---- 3c. draft reference blocks; abandon; RETIRE is permanent ------
        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-41", "customer hold")
        )
        error = assert_blocked(status, body, {"outbound_plan"})
        draft_conflict = next(
            item
            for item in error["details"]["conflicts"]
            if item["kind"] == "outbound_plan" and item["reference"] == "OB-41"
        )
        assert draft_conflict["message"].endswith("(DRAFT)")
        abandoned = api.expect_ok("POST", "/api/outbound-trains/OB-41/abandon", {})
        assert abandoned["outbound"]["state"] == "ABANDONED"
        retired = api.expect_ok(
            "POST",
            "/api/car-deactivations",
            deactivate_payload("C-N4-41", "scrapped after derailment damage", kind="RETIRE"),
        )
        assert retired["deactivation"]["code"] == "DEA-0004"
        assert retired["deactivation"]["kind"] == "RETIRE"
        assert retired["deactivation"]["blocked_attempts"] == 1
        # permanent withdrawal cannot be recovered
        status, body = api.request("POST",
            "/api/car-recoveries",
            {"car_code": "C-N4-41", "reason": "tried to revive", "operator": "HX"},
        )
        assert status == 409 and body["error"]["code"] == "RESOURCE_BUSY"
        assert body["error"]["details"]["blocker"] == "permanent_retirement"
        detail = api.expect_ok("GET", "/api/car-deactivations/DEA-0004")
        assert detail["deactivation"]["status"] == "ACTIVE"

        # ---- 4. departed car rejected -------------------------------------
        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-61", "late fault report")
        )
        error = assert_blocked(status, body, {"departed_train"})
        departed_conflict = next(
            item for item in error["details"]["conflicts"] if item["kind"] == "departed_train"
        )
        assert departed_conflict["reference"] == "OB-43"
        assert departed_conflict["action_path"] is None

        # ---- 5. unclassified (received, not allocated) car rejected --------
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {
                "code": "INT-45",
                "route": "RAIL-41",
                "arrival_at": "2026-09-12T10:00:00Z",
                "cars": [box("C-N4-81")],
            },
        )
        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-81", "arrival damage")
        )
        error = assert_blocked(status, body, {"intake"})
        intake_conflict = next(
            item for item in error["details"]["conflicts"] if item["kind"] == "intake"
        )
        assert intake_conflict["reference"] == "INT-45"
        assert intake_conflict["action_path"] == "/api/intake-trains/INT-45/cancel"
        # execute the advertised action: cancel removes the dangling car
        cancelled = api.expect_ok("POST", "/api/intake-trains/INT-45/cancel", {})
        assert cancelled["released_car_codes"] == ["C-N4-81"]
        status, body = api.request("POST", "/api/car-deactivations", deactivate_payload("C-N4-81", "x")
        )
        assert status == 404

        # ---- 6. withdrawn car cannot be re-admitted by a new intake --------
        api.expect_error(
            "POST",
            "/api/intake-trains",
            {
                "code": "INT-46",
                "route": "RAIL-41",
                "arrival_at": "2026-09-12T11:00:00Z",
                "cars": [box("C-N4-41")],
            },
        )

        # ---- audit trail: decisions, blocks and recoveries are traceable ---
        shift = api.expect_ok("GET", "/api/shifts/SHIFT-41")
        kinds = {event["kind"] for event in shift["events"]}
        assert {
            "CAR_DEACTIVATED",
            "CAR_DEACTIVATION_BLOCKED",
            "CAR_RECOVERED",
            "CAR_RECOVERY_BLOCKED",
            "OUTBOUND_ABANDONED",
        } <= kinds

        # final yard accounting
        yard = api.expect_ok("GET", "/api/yard")
        counts = yard["metrics"]["car_state_counts"]
        assert counts["removed"] == 1          # only the retired car
        assert counts["departed"] == 2         # OB-42 (51) and OB-43 (61)
        assert "C-N4-41" in yard["metrics"]["deactivated_cars"]
        # bays empty and every active run finished
        assert all(bay["cars"] == 0 for bay in yard["metrics"]["transfer_bays"])
        assert yard["metrics"]["active_runs"] == []
    finally:
        server.stop()
        temp.cleanup()


if __name__ == "__main__":
    run_full()
    print("OK wf_car_deactivation")
    raise SystemExit(0)
