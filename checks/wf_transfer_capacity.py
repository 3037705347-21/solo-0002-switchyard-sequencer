"""Workflow check: transfer line capacity scheduling across two lines.

Exercises the public HTTP API end to end:

* register a second transfer line and view capacity of both;
* plan several pull tickets and assert the deterministic best-fit choice
  (pack the line with the least slack first; ties resolved by committed
  load, physical load, capacity, stable registration order, then code);
* force the three planning-stage failure reasons: existing occupancy,
  single-line limit, and line unavailable;
* partially execute a run (cars physically on a line, some blockers already
  returned), restart the service, and verify occupancy is recomputed from
  persisted runs and vehicle positions so the run can complete;
* cancel a queued run and replan it deterministically;
* confirm reservations are left as a traceable audit trail.

The pre-existing single transfer line (seed X1) keeps behaving as before:
omitting ``transfer_code`` schedules automatically and still picks X1 when
it is the only registered line.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from support import ApiClient, RunningServer

SHIFT = {"code": "SHIFT-70", "dispatcher": "HUA", "opened_at": "2026-09-13T08:00:00Z"}


def open_shift(api: ApiClient) -> None:
    api.expect_ok("POST", "/api/shifts", SHIFT)


def _car(code: str, destination: str = "N4") -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": False,
        "length_m": 18,
        "danger_class": "NONE",
    }


def classify_cars(api: ApiClient) -> None:
    n4 = [_car(f"C-N4-{i:02d}") for i in range(1, 11)]
    e7 = [_car(f"C-E7-{i:02d}", "E7") for i in range(1, 5)]
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-70",
            "route": "RAIL-70",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": n4 + e7,
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-70/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"


def _line_map(view: dict[str, object]) -> dict[str, dict[str, object]]:
    return {line["code"]: line for line in view["transfer_lines"]}


def _require_line(lines: dict[str, dict[str, object]], code: str) -> dict[str, object]:
    assert code in lines, f"{code} missing from {sorted(lines)}"
    return lines[code]


def run_registration_and_capacity(api: ApiClient) -> None:
    yard = api.expect_ok("GET", "/api/yard")
    seed_bays = {bay["code"]: bay for bay in yard["metrics"]["transfer_bays"]}
    x1 = seed_bays["X1"]
    assert x1["capacity_cars"] == 10 and x1["physical_cars"] == 0
    assert x1["committed_cars"] == 0 and x1["available_cars"] == 10

    registered = api.expect_ok(
        "POST", "/api/transfer-lines", {"code": "X2", "capacity_cars": 2}
    )
    x2 = registered["transfer_line"]
    assert x2["code"] == "X2" and x2["registered_order"] == 2
    assert x2["state"] == "OPERATIONAL" and x2["available_cars"] == 2

    view = api.expect_ok("GET", "/api/transfer-lines")
    lines = _line_map(view)
    assert [line["code"] for line in view["transfer_lines"]] == ["X1", "X2"]
    assert _require_line(lines, "X1")["registered_order"] == 1
    assert _require_line(lines, "X2")["committed_cars"] == 0

    duplicate = api.expect_error("POST", "/api/transfer-lines", {"code": "X2", "capacity_cars": 5})
    assert duplicate["code"] == "CONFLICT"


def _sequence(api: ApiClient, code: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return api.expect_ok("POST", f"/api/outbound-trains/{code}/sequencer", payload or {})


def _draft(api: ApiClient, code: str, cars: list[str], destination: str = "N4") -> None:
    created = api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": code, "destination": destination, "car_codes": cars},
    )
    assert created["state"] == "DRAFT"


def run_multi_plan_selection(api: ApiClient) -> None:
    # Stack after classification, bottom -> top:
    # N4-A: 01 02 03 04 05 06 07 08 09 10
    # E7-A: 01 02 03 04

    # Deepest N4 car buffers 9 blockers: X2 (cap 2) cannot take it, X1 can.
    _draft(api, "OB-71", ["C-N4-01"])
    seq = _sequence(api, "OB-71")
    assert seq["pull_run"]["transfer_code"] == "X1", seq
    transfer = seq["transfer"]
    assert transfer["required_slots"] == 9
    assert transfer["slack_slots"] == 1
    assert transfer["selection"] == "best_fit"
    eval_map = {item["code"]: item for item in transfer["evaluations"]}
    assert eval_map["X1"]["feasible"] is True
    assert eval_map["X2"]["feasible"] is False
    assert eval_map["X2"]["reason"] == "single_line_limit"

    lines = _line_map(api.expect_ok("GET", "/api/transfer-lines"))
    assert _require_line(lines, "X1")["committed_cars"] == 9
    assert _require_line(lines, "X1")["available_cars"] == 1
    x1_held = {entry["run_code"]: entry for entry in lines["X1"]["held_by"]}
    assert x1_held["RUN-OB-71"]["held_slots"] == 9
    assert x1_held["RUN-OB-71"]["physical_slots"] == 0


def run_capacity_failure_reasons(api: ApiClient) -> None:
    # Deepest E7 car needs 3 slots. X1 has only 1 free (existing occupancy);
    # X2 cap is 2 (single-line limit). Both reasons come back at once.
    _draft(api, "OB-72", ["C-E7-01"], destination="E7")
    error = api.expect_error("POST", "/api/outbound-trains/OB-72/sequencer", {})
    assert error["code"] == "TRANSFER_CAPACITY", error
    details = error["details"]
    assert details["required_slots"] == 3
    per_line = {item["code"]: item for item in details["lines"]}
    assert per_line["X1"]["reason"] == "existing_occupancy"
    assert per_line["X1"]["available_cars"] == 1
    assert per_line["X2"]["reason"] == "single_line_limit"
    assert per_line["X2"]["capacity_cars"] == 2
    # A rejected plan reserves nothing and stays in DRAFT (only OB-71's
    # single planned car is reserved; blocker cars remain standing).
    yard = api.expect_ok("GET", "/api/yard")
    assert "OB-72" in yard["metrics"]["active_outbounds"]
    assert yard["metrics"]["car_state_counts"]["reserved"] == 1
    assert yard["metrics"]["car_state_counts"]["standing"] == 13

    # Take X1's last slot and both X2 slots with feasible tickets.
    _draft(api, "OB-73", ["C-E7-02"], destination="E7")  # needs 2 -> X2 best fit
    seq73 = _sequence(api, "OB-73")
    assert seq73["pull_run"]["transfer_code"] == "X2"
    assert seq73["transfer"]["slack_slots"] == 0

    _draft(api, "OB-74", ["C-E7-03"], destination="E7")  # needs 1 -> X1 last slot
    seq74 = _sequence(api, "OB-74")
    assert seq74["pull_run"]["transfer_code"] == "X1"
    assert seq74["transfer"]["slack_slots"] == 0

    # A 0-buffer top car fits on both with slack 0; the committed-load
    # tiebreak selects X1 (10 committed) over X2 (2 committed), never name.
    _draft(api, "OB-75", ["C-E7-04"], destination="E7")  # needs 0
    seq75 = _sequence(api, "OB-75")
    assert seq75["pull_run"]["transfer_code"] == "X1", seq75
    assert seq75["transfer"]["required_slots"] == 0

    # Force "line unavailable": take X2 out of service (only after its active
    # run is cancelled), then an explicit request for X2 is refused even for a
    # ticket that fits its capacity.
    cancel = api.expect_ok("POST", "/api/pull-runs/RUN-OB-73/cancel", {})
    assert cancel["pull_run"]["state"] == "CANCELLED"
    assert cancel["outbound"]["state"] == "DRAFT"
    api.expect_ok("POST", "/api/transfer-lines/X2/state", {"state": "MAINTENANCE"})
    # C-N4-10 is the N4 top car (no blockers), so this is purely a line
    # question: requesting the dead line fails with line_unavailable.
    _draft(api, "OB-77", ["C-N4-10"], destination="N4")
    error = api.expect_error("POST", "/api/outbound-trains/OB-77/sequencer", {"transfer_code": "X2"})
    assert error["code"] == "TRANSFER_CAPACITY", error
    assert error["details"]["requested_transfer_code"] == "X2"
    assert error["details"]["lines"][0]["reason"] == "line_unavailable"
    # A request for a line that was never registered is a 404.
    missing = api.expect_error("POST", "/api/outbound-trains/OB-77/sequencer", {"transfer_code": "X9"})
    assert missing["code"] == "NOT_FOUND"
    # AUTO with X2 dead reports every line's reason at once. A fresh feasible
    # deep ticket (8 standing blockers) cannot fit X1's single free slot and
    # cannot use the maintained X2 either.
    _draft(api, "OB-78", ["C-N4-02"], destination="N4")
    auto_error = api.expect_error("POST", "/api/outbound-trains/OB-78/sequencer", {"transfer_code": "AUTO"})
    assert auto_error["code"] == "TRANSFER_CAPACITY"
    assert auto_error["details"]["required_slots"] == 8
    auto_lines = {item["code"]: item for item in auto_error["details"]["lines"]}
    assert auto_lines["X1"]["reason"] == "existing_occupancy"
    assert auto_lines["X2"]["reason"] == "line_unavailable"
    api.expect_ok("POST", "/api/transfer-lines/X2/state", {"state": "OPERATIONAL"})
    # Rejected plans reserve nothing and stay in DRAFT.
    for code in ("OB-72", "OB-77", "OB-78"):
        assert code in api.expect_ok("GET", "/api/yard")["metrics"]["active_outbounds"]
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 3  # OB-71, OB-74, OB-75

    # Taking a line with active runs out of service is rejected.
    blocked = api.expect_error("POST", "/api/transfer-lines/X1/state", {"state": "MAINTENANCE"})
    assert blocked["code"] == "RESOURCE_BUSY"
    assert "RUN-OB-71" in blocked["details"]["queued"] or "RUN-OB-71" in blocked["details"]["running"]


def run_partial_restart_cancel(api: ApiClient, data_dir: Path) -> None:
    # Complete OB-71 on X1: advances return blockers physically to N4-A and
    # release its 9-slot hold at completion.
    advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-71/advance", {"steps": 200})
    assert advanced["completed"] is True
    assert advanced["pull_run"]["state"] == "COMPLETED"
    assert advanced["outbound"]["state"] == "READY"
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-71/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"

    lines = _line_map(api.expect_ok("GET", "/api/transfer-lines"))
    x1 = lines["X1"]
    # X1 still carries the 1-slot OB-74 queued hold plus the 0-slot OB-75.
    assert x1["physical_cars"] == 0
    assert x1["committed_cars"] == 1
    held = {entry["run_code"]: entry for entry in x1["held_by"]}
    assert held["RUN-OB-74"]["held_slots"] == 1

    # Release the queued E7 tickets (top-down) so their cars are standing
    # again and the cancelled OB-73 ticket can be replanned.
    for run_code in ("RUN-OB-75", "RUN-OB-74"):
        cancelled = api.expect_ok("POST", f"/api/pull-runs/{run_code}/cancel", {})
        assert cancelled["pull_run"]["state"] == "CANCELLED"
        assert cancelled["outbound"]["state"] == "DRAFT"
    lines = _line_map(api.expect_ok("GET", "/api/transfer-lines"))
    assert lines["X1"]["committed_cars"] == 0
    assert lines["X2"]["committed_cars"] == 0

    # Replan OB-73 (needs 2): X1 has 10 free (slack 8), X2 has 2 free
    # (slack 0), so best-fit deterministically packs X2. Execute only part of
    # it: both blockers buffer onto X2, the target pulls, one blocker returns,
    # leaving exactly one car physically on the line when the process stops.
    seq73 = _sequence(api, "OB-73", {"transfer_code": "AUTO"})
    assert seq73["pull_run"]["transfer_code"] == "X2"
    assert seq73["pull_run"]["code"] == "RUN-OB-73-2"
    steps73 = seq73["pull_run"]["steps"]
    assert [step["verb"] for step in steps73] == ["BUFFER", "BUFFER", "PULL", "RETURN", "RETURN"]
    partial = api.expect_ok("POST", "/api/pull-runs/RUN-OB-73-2/advance", {"steps": 4})
    assert partial["completed"] is False
    assert partial["pull_run"]["current_step"] == 4

    lines = _line_map(api.expect_ok("GET", "/api/transfer-lines"))
    x2 = lines["X2"]
    # One blocker returned to track, one still physically on the line.
    assert x2["physical_cars"] == 1
    held = {entry["run_code"]: entry for entry in x2["held_by"]}
    assert held["RUN-OB-73-2"]["physical_slots"] == 1
    assert held["RUN-OB-73-2"]["held_slots"] == 1

    # ---- Simulated crash: start a fresh process over the same data
    # directory. Occupancy must be rebuilt from the persisted run and the
    # live car positions. ----
    running = RunningServer(data_dir=data_dir)
    try:
        running.wait_ready()
        api2 = running.api
        lines = _line_map(api2.expect_ok("GET", "/api/transfer-lines"))
        x2 = lines["X2"]
        assert x2["physical_cars"] == 1
        assert x2["committed_cars"] == 1
        held = {entry["run_code"]: entry for entry in x2["held_by"]}
        assert held["RUN-OB-73-2"]["run_state"] == "RUNNING"
        assert held["RUN-OB-73-2"]["held_slots"] == 1
        assert held["RUN-OB-73-2"]["physical_slots"] == 1

        state_file = data_dir / "yard-state.json"
        state_doc = json.loads(state_file.read_text(encoding="utf-8"))
        reservations = {item["run_code"]: item for item in state_doc["transfer_reservations"]}
        # The completed run's reservation stays on file as released audit.
        assert reservations["RUN-OB-71"]["state"] == "RELEASED"
        assert reservations["RUN-OB-71"]["required_slots"] == 9

        # Finish the run on the restarted server; the last RETURN clears X2.
        finished = api2.expect_ok("POST", "/api/pull-runs/RUN-OB-73-2/advance", {"steps": 10})
        assert finished["completed"] is True
        departed = api2.expect_ok("POST", "/api/outbound-trains/OB-73/depart", {})
        assert departed["departed_car_count"] == 1
        lines = _line_map(api2.expect_ok("GET", "/api/transfer-lines"))
        assert lines["X2"]["physical_cars"] == 0
        assert lines["X2"]["committed_cars"] == 0

        state_doc = json.loads(state_file.read_text(encoding="utf-8"))
        reservations = {item["run_code"]: item for item in state_doc["transfer_reservations"]}
        done = reservations["RUN-OB-73-2"]
        assert done["state"] == "RELEASED"
        # Planned guarantee carried across the restart; observed_peak reflects
        # the peak actually witnessed after the (simulated) crash: one blocker
        # was already returned, leaving one car on the line.
        assert done["required_slots"] == 2
        assert done["observed_peak_slots"] == 1
        assert done["released_at"]
        # Completion reconciliation removed it from the live hold view.
        completed_view = api2.expect_ok("GET", "/api/transfer-lines")
        assert all(
            entry["run_code"] != "RUN-OB-73-2"
            for line in completed_view["transfer_lines"]
            for entry in line["held_by"]
        )

        # A cancelled queued run replans with a deterministic new run code;
        # X2 is fully free (slack 1 for 1 slot) so best-fit packs it before
        # touching X1 (slack 9).
        replanned = api2.expect_ok("POST", "/api/outbound-trains/OB-74/sequencer", {})
        assert replanned["pull_run"]["code"] == "RUN-OB-74-2"
        assert replanned["pull_run"]["transfer_code"] == "X2"
        assert replanned["transfer"]["slack_slots"] == 1

        # Running runs cannot be cancelled: their cars are physically split
        # between the source track and the transfer line.
        api2.expect_ok("POST", "/api/pull-runs/RUN-OB-74-2/advance", {"steps": 1})
        reject = api2.expect_error("POST", "/api/pull-runs/RUN-OB-74-2/cancel", {})
        assert reject["code"] == "RESOURCE_BUSY"
        api2.expect_ok("POST", "/api/pull-runs/RUN-OB-74-2/advance", {"steps": 200})
        api2.expect_ok("POST", "/api/outbound-trains/OB-74/depart", {})

        # OB-75 (top car, 0 buffers) closes out the remaining queued ticket.
        last = api2.expect_ok("POST", "/api/outbound-trains/OB-75/sequencer", {})
        assert last["pull_run"]["code"] == "RUN-OB-75-2"
        api2.expect_ok("POST", "/api/pull-runs/RUN-OB-75-2/advance", {"steps": 200})
        api2.expect_ok("POST", "/api/outbound-trains/OB-75/depart", {})
    finally:
        running.stop()


def run(api: ApiClient, data_dir: Path) -> None:
    open_shift(api)
    classify_cars(api)
    run_registration_and_capacity(api)
    run_multi_plan_selection(api)
    run_capacity_failure_reasons(api)
    run_partial_restart_cancel(api, data_dir)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="switchyard-transfer-") as temp:
        data_dir = Path(temp) / "data"
        server = RunningServer(data_dir=data_dir)
        try:
            server.wait_ready()
            run(server.api, data_dir)
            print("OK wf_transfer_capacity")
            return 0
        finally:
            server.stop()


if __name__ == "__main__":
    sys.exit(main())
