"""Workflow check: a partially executed ticket keeps its full peak hold.

Regression for the transfer capacity bug where a peak-2 ticket that had only
executed its first BUFFER dropped its hold to 1 slot, letting a competing
ticket be admitted onto the same 2-slot line and deadlock the ground
operation. The hold must instead be the full peak replayed from the cars
currently on the line: after the first BUFFER the next action is another
BUFFER, so both slots stay reserved.

It also verifies the service can be restarted mid-run and continue execution
with the same conservative hold.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from support import ApiClient, RunningServer


def _car(code: str, destination: str = "N4") -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": False,
        "length_m": 18,
        "danger_class": "NONE",
    }


def _lines(api: ApiClient) -> dict[str, dict[str, object]]:
    view = api.expect_ok("GET", "/api/transfer-lines")
    return {line["code"]: line for line in view["transfer_lines"]}


def setup(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-80", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )
    # Two independent source stacks so the competing ticket is feasible from
    # the car point of view and only blocked by transfer-line capacity.
    n4 = [_car(f"C-N4-{i:02d}") for i in range(1, 5)]  # 4 deep
    s2 = [_car(f"C-S2-{i:02d}", "S2") for i in range(1, 4)]  # 3 deep
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-80",
            "route": "RAIL-80",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": n4 + s2,
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-80/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"
    registered = api.expect_ok("POST", "/api/transfer-lines", {"code": "X2", "capacity_cars": 2})
    assert registered["transfer_line"]["available_cars"] == 2


def _draft(api: ApiClient, code: str, cars: list[str], destination: str) -> None:
    created = api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": code, "destination": destination, "car_codes": cars},
    )
    assert created["state"] == "DRAFT"


def _sequence(api: ApiClient, code: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return api.expect_ok("POST", f"/api/outbound-trains/{code}/sequencer", payload or {})


def run(api: ApiClient, data_dir: Path) -> None:
    setup(api)

    # Primary ticket pulls the second car of the 4-deep N4 stack: two blockers
    # sit above it, so it needs a peak of exactly 2 slots on a transfer line.
    _draft(api, "OB-81", ["C-N4-02"], "N4")
    primary = _sequence(api, "OB-81", {"transfer_code": "X2"})
    run81 = primary["pull_run"]["code"]
    assert run81 == "RUN-OB-81"
    assert primary["pull_run"]["transfer_code"] == "X2"
    verbs = [step["verb"] for step in primary["pull_run"]["steps"]]
    assert verbs == ["BUFFER", "BUFFER", "PULL", "RETURN", "RETURN"], verbs
    assert primary["transfer"]["required_slots"] == 2

    lines = _lines(api)
    assert lines["X2"]["committed_cars"] == 2
    assert lines["X2"]["available_cars"] == 0

    # Execute ONLY the first BUFFER: one car is physically on X2 and another
    # BUFFER is still pending, so the run must keep holding both slots.
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run81}/advance", {"steps": 1})
    assert advanced["completed"] is False
    assert advanced["pull_run"]["current_step"] == 1
    lines = _lines(api)
    x2 = lines["X2"]
    assert x2["physical_cars"] == 1, x2
    held = {entry["run_code"]: entry for entry in x2["held_by"]}
    assert held[run81]["physical_slots"] == 1
    assert held[run81]["held_slots"] == 2, held  # regression: was wrongly 1
    assert x2["committed_cars"] == 2, x2
    assert x2["available_cars"] == 0, x2

    # A competing peak-2 ticket on a different source track must be refused at
    # the planning stage: X2 is fully held, X1 (10 slots) is a different line
    # and AUTO must not split the ticket. Requesting X2 explicitly reports
    # existing occupancy with zero free slots.
    _draft(api, "OB-82", ["C-S2-01"], "S2")  # two S2 blockers, needs 2
    error = api.expect_error("POST", "/api/outbound-trains/OB-82/sequencer", {"transfer_code": "X2"})
    assert error["code"] == "TRANSFER_CAPACITY", error
    x2_eval = next(line for line in error["details"]["lines"] if line["code"] == "X2")
    assert x2_eval["reason"] == "existing_occupancy"
    assert x2_eval["available_cars"] == 0
    assert x2_eval["required_slots"] == 2

    # ---- Restart the service while the run is one BUFFER in. The hold must
    # be recomputed from the persisted run and live car positions and remain
    # the full peak of 2. ----
    running = RunningServer(data_dir=data_dir)
    try:
        running.wait_ready()
        api2 = running.api
        lines = _lines(api2)
        x2 = lines["X2"]
        assert x2["physical_cars"] == 1
        assert x2["committed_cars"] == 2, x2
        assert x2["available_cars"] == 0
        held = {entry["run_code"]: entry for entry in x2["held_by"]}
        assert held[run81]["run_state"] == "RUNNING"
        assert held[run81]["held_slots"] == 2
        assert held[run81]["physical_slots"] == 1

        # The competing ticket is still rejected after restart.
        error = api2.expect_error("POST", "/api/outbound-trains/OB-82/sequencer", {"transfer_code": "X2"})
        assert error["code"] == "TRANSFER_CAPACITY"
        assert error["details"]["lines"][0]["reason"] == "existing_occupancy"

        # Continue the interrupted run: second BUFFER fills the line to its
        # peak of 2, then PULL and the two RETURNs release the slots.
        advanced = api2.expect_ok("POST", f"/api/pull-runs/{run81}/advance", {"steps": 1})
        lines = _lines(api2)
        assert lines["X2"]["physical_cars"] == 2
        held = {entry["run_code"]: entry for entry in lines["X2"]["held_by"]}
        assert held[run81]["held_slots"] == 2
        assert lines["X2"]["available_cars"] == 0

        finished = api2.expect_ok("POST", f"/api/pull-runs/{run81}/advance", {"steps": 10})
        assert finished["completed"] is True
        assert finished["outbound"]["state"] == "READY"
        departed = api2.expect_ok("POST", "/api/outbound-trains/OB-81/depart", {})
        assert departed["departed_car_count"] == 1
        lines = _lines(api2)
        assert lines["X2"]["physical_cars"] == 0
        assert lines["X2"]["committed_cars"] == 0
        assert lines["X2"]["available_cars"] == 2

        # The line is fully released only after the primary run completes;
        # now the competing peak-2 ticket is admissible and runs cleanly.
        admitted = api2.expect_ok("POST", "/api/outbound-trains/OB-82/sequencer", {"transfer_code": "X2"})
        assert admitted["pull_run"]["transfer_code"] == "X2"
        run82 = admitted["pull_run"]["code"]
        done = api2.expect_ok("POST", f"/api/pull-runs/{run82}/advance", {"steps": 10})
        assert done["completed"] is True
        api2.expect_ok("POST", "/api/outbound-trains/OB-82/depart", {})
        lines = _lines(api2)
        assert lines["X2"]["physical_cars"] == 0
        assert lines["X2"]["committed_cars"] == 0
    finally:
        running.stop()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="switchyard-partial-hold-") as temp:
        data_dir = Path(temp) / "data"
        server = RunningServer(data_dir=data_dir)
        try:
            server.wait_ready()
            run(server.api, data_dir)
            print("OK wf_transfer_partial_hold")
            return 0
        finally:
            server.stop()


if __name__ == "__main__":
    sys.exit(main())
