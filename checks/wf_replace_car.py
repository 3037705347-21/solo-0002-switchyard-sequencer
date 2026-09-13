"""Workflow check: safely replace a car in a PLANNED outbound consist."""

from __future__ import annotations

from support import ApiClient, run_check


def car(code: str, destination: str = "N4") -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": False,
        "length_m": 12,
        "danger_class": "NONE",
    }


def classify(api: ApiClient, intake_code: str, cars: list[dict[str, object]]) -> None:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": intake_code,
            "route": f"RAIL-{intake_code[-2:]}",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": cars,
        },
    )
    result = api.expect_ok("POST", f"/api/intake-trains/{intake_code}/classify", {})
    assert result["intake"]["state"] == "CLASSIFIED"


def replace(api: ApiClient, train_code: str, old_code: str, new_code: str) -> dict[str, object]:
    return api.expect_ok(
        "POST",
        f"/api/outbound-trains/{train_code}/replace-car",
        {"old_car_code": old_code, "new_car_code": new_code},
    )


def expect_replace_failed(api: ApiClient, train_code: str, old_code: str, new_code: str) -> None:
    error = api.expect_error(
        "POST",
        f"/api/outbound-trains/{train_code}/replace-car",
        {"old_car_code": old_code, "new_car_code": new_code},
    )
    assert error["code"] in {"VALIDATION_ERROR", "CONFLICT", "RESOURCE_BUSY"}


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-04", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )

    # Fill the N4 destination track. The next N4 intake therefore occupies
    # MIX-1 in the exact bottom-to-top order used below.
    classify(api, "INT-40", [car(f"C-N4-F{index:02d}") for index in range(1, 11)])

    n4_codes = [
        "C-N4-CAP",  # bottom: replacing with this needs 11 buffer slots below
        "C-N4-C1",
        "C-N4-C2",
        "C-N4-CSEC",
        "C-N4-B1",
        "C-N4-B2",
        "C-N4-BOLD",
        "C-N4-B3",
        "C-N4-F11",
        "C-N4-F12",
        "C-N4-QOLD",
        "C-N4-BX",
        "C-N4-QNEW",  # top
    ]
    classify(api, "INT-41", [car(code) for code in n4_codes])
    classify(api, "INT-42", [car("C-E7-01", "E7")])

    # An unexecuted PLANNED ticket.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-C", "destination": "N4", "car_codes": ["C-N4-CSEC", "C-N4-C1"]},
    )
    planned = api.expect_ok("POST", "/api/outbound-trains/OB-C/sequencer", {"transfer_code": "X1"})
    run_c = planned["pull_run"]
    assert run_c["state"] == "QUEUED"
    assert len([step for step in run_c["steps"] if step["verb"] == "BUFFER"]) == 17
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 2

    # Rejections must not alter the plan or the old/new reservations.
    expect_replace_failed(api, "OB-C", "C-N4-CSEC", "C-E7-01")
    expect_replace_failed(api, "OB-C", "C-N4-CSEC", "C-N4-CAP")  # later planned C1 violates LIFO
    failed = api.expect_ok("GET", "/api/yard")
    assert failed["metrics"]["car_state_counts"]["reserved"] == 2
    failed_train = api.expect_ok("GET", "/api/shifts/SHIFT-04")
    assert not any(event["kind"] == "PLANNED_CAR_REPLACED" for event in failed_train["events"])

    replaced = replace(api, "OB-C", "C-N4-C1", "C-N4-BX")
    assert replaced["outbound"]["planned_car_codes"] == ["C-N4-CSEC", "C-N4-BX"]
    assert replaced["pull_run"]["state"] == "QUEUED"
    assert replaced["pull_run"]["current_step"] == 0
    assert replaced["old_car"]["state"] == "STANDING"
    assert replaced["new_car"]["state"] == "RESERVED"
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 2

    total_c = len(replaced["pull_run"]["steps"])
    completed_c = api.expect_ok("POST", "/api/pull-runs/RUN-OB-C/advance", {"steps": total_c})
    assert completed_c["completed"] is True
    assert completed_c["outbound"]["state"] == "READY"
    assert completed_c["outbound"]["assembled_car_codes"] == ["C-N4-CSEC", "C-N4-BX"]

    # Leave three foreign cars in X1 so a later replacement has to include that
    # occupied transfer capacity in its LIFO simulation.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-D", "destination": "N4", "car_codes": ["C-N4-F01"]},
    )
    planned_d = api.expect_ok("POST", "/api/outbound-trains/OB-D/sequencer", {"transfer_code": "X1"})
    api.expect_ok("POST", "/api/pull-runs/RUN-OB-D/advance", {"steps": 3})
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["transfer_bays"][0]["cars"] == 3

    # A second ticket advanced into the buffer phase: four of BOLD's five
    # blockers are in X1 and its pull step has not executed.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-B", "destination": "N4", "car_codes": ["C-N4-BOLD"]},
    )
    planned_b = api.expect_ok("POST", "/api/outbound-trains/OB-B/sequencer", {"transfer_code": "X1"})
    run_b_code = planned_b["pull_run"]["code"]
    buffered = api.expect_ok("POST", f"/api/pull-runs/{run_b_code}/advance", {"steps": 4})
    assert buffered["completed"] is False
    assert buffered["pull_run"]["current_step"] == 4
    buffered_cars = [step["car_code"] for step in buffered["pull_run"]["steps"][:4]]
    assert buffered_cars == ["C-N4-QNEW", "C-N4-QOLD", "C-N4-F12", "C-N4-F11"]
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["transfer_bays"][0]["cars"] == 7

    # A failed replacement leaves the buffered ticket executable.
    expect_replace_failed(api, "OB-B", "C-N4-BOLD", "C-E7-01")
    expect_replace_failed(api, "OB-B", "C-N4-BOLD", "C-N4-BX")  # already assembled
    expect_replace_failed(api, "OB-B", "C-N4-BOLD", "C-N4-CAP")  # 8 blockers + 3 occupied slots
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 2
    assert yard["metrics"]["transfer_bays"][0]["cars"] == 7

    # Prove a rejected replacement left the original buffered ticket runnable.
    original_remaining = len(buffered["pull_run"]["steps"]) - 4
    continued_original = api.expect_ok(
        "POST",
        f"/api/pull-runs/{run_b_code}/advance",
        {"steps": original_remaining},
    )
    assert continued_original["completed"] is True
    assert continued_original["outbound"]["assembled_car_codes"] == ["C-N4-BOLD"]

    # Use a second buffered ticket for the successful replacement check.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-E", "destination": "N4", "car_codes": ["C-N4-B2"]},
    )
    planned_e = api.expect_ok("POST", "/api/outbound-trains/OB-E/sequencer", {"transfer_code": "X1"})
    run_e_code = planned_e["pull_run"]["code"]
    buffered_e = api.expect_ok("POST", f"/api/pull-runs/{run_e_code}/advance", {"steps": 4})
    assert buffered_e["pull_run"]["current_step"] == 4

    replaced_b = replace(api, "OB-E", "C-N4-B2", "C-N4-C2")
    run_b = replaced_b["pull_run"]
    assert run_b["state"] == "RUNNING"
    assert run_b["current_step"] == 4
    assert [step["verb"] for step in run_b["steps"][:4]] == ["BUFFER"] * 4
    assert run_b["steps"][4] == {
        "verb": "RETURN",
        "car_code": "C-N4-F11",
        "source_code": "X1",
        "target_code": "MIX-1",
    }
    assert replaced_b["outbound"]["planned_car_codes"] == ["C-N4-C2"]
    assert replaced_b["outbound"]["assembled_car_codes"] == []
    assert replaced_b["old_car"]["state"] == "STANDING"
    assert replaced_b["new_car"]["state"] == "RESERVED"
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 2

    completed_b = api.expect_ok(
        "POST",
        f"/api/pull-runs/{run_e_code}/advance",
        {"steps": len(run_b["steps"]) - 4},
    )
    assert completed_b["completed"] is True
    assert completed_b["outbound"]["state"] == "READY"
    assert completed_b["outbound"]["assembled_car_codes"] == ["C-N4-C2"]

    # Finish the other run after B; its three buffered cars must still be intact.
    d_total = len(planned_d["pull_run"]["steps"])
    completed_d = api.expect_ok("POST", "/api/pull-runs/RUN-OB-D/advance", {"steps": d_total - 3})
    assert completed_d["completed"] is True

    yard = api.expect_ok("GET", "/api/yard")
    counts = yard["metrics"]["car_state_counts"]
    assert counts["reserved"] == 0
    assert counts["assembled"] == 5
    assert counts["standing"] == 19
    assert yard["metrics"]["transfer_bays"][0]["cars"] == 0

    shift = api.expect_ok("GET", "/api/shifts/SHIFT-04")
    replacement_events = [event for event in shift["events"] if event["kind"] == "PLANNED_CAR_REPLACED"]
    assert len(replacement_events) == 2


if __name__ == "__main__":
    raise SystemExit(run_check("wf_replace_car", run))
