"""Workflow check: transfer line selection on the sequencer.

Covers the four requested sequencer inputs:

1. no transfer_code (automatic selection by current available capacity);
2. explicit transfer_code X1 (strictly honoured);
3. explicit unknown transfer code (rejected, retriable without state change);
4. multiple transfer lines with different capacities (existing plan
   reservations are never silently moved or reduced).

The yard is seeded with two transfer lines (X1:5, X2:6). The check also
restarts the service against the same data directory to prove the selected
line and its capacity occupancy survive execution, retry, and restart.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from support import ApiClient, RunningServer, start_server

EXTRA_ENV = {"SWITCHYARD_TRANSFER_BAYS": "X1:5,X2:6"}
SHIFT = {"code": "SHIFT-TX", "dispatcher": "RUI", "opened_at": "2026-09-13T08:00:00Z"}


def _intake(api: ApiClient, code: str, cars: list[dict[str, object]]) -> None:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": code,
            "route": f"RAIL-{code}",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": cars,
        },
    )
    classified = api.expect_ok("POST", f"/api/intake-trains/{code}/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"


def _car(code: str, destination: str, kind: str = "BOX", length: int = 18) -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": False,
        "length_m": length,
        "danger_class": "NONE",
    }


def _yard_bays(api: ApiClient) -> dict[str, dict[str, object]]:
    metrics = api.expect_ok("GET", "/api/yard")["metrics"]
    return {bay["code"]: bay for bay in metrics["transfer_bays"]}


def _open_yard(api: ApiClient) -> None:
    api.expect_ok("POST", "/api/shifts", SHIFT)
    # N4: bottom N1 then blockers NB1, NB2 -> pulling N1 needs 2 buffered.
    _intake(
        api,
        "INT-N1",
        [_car("C-N1-01", "N4"), _car("C-NB1-01", "N4"), _car("C-NB2-01", "N4")],
    )
    # S2: bottom T1, T2 then blockers B1, B2 -> pulling T1 needs 3 buffered.
    _intake(
        api,
        "INT-T1",
        [
            _car("C-T1-01", "S2"),
            _car("C-T2-01", "S2"),
            _car("C-B1-01", "S2"),
            _car("C-B2-01", "S2"),
        ],
    )
    # W9: W1 buried under one blocker -> pulling W1 needs 1 buffered.
    _intake(api, "INT-W1", [_car("C-W1-01", "W9"), _car("C-WB1-01", "W9")])
    # E7: bottom E1 then blockers EB1..EB4 -> pulling E1 needs 4 buffered.
    _intake(
        api,
        "INT-E1",
        [
            _car("C-E1-01", "E7"),
            _car("C-EB1-01", "E7"),
            _car("C-EB2-01", "E7"),
            _car("C-EB3-01", "E7"),
            _car("C-EB4-01", "E7"),
        ],
    )


def _create_outbound(api: ApiClient, code: str, destination: str, cars: list[str]) -> None:
    created = api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": code, "destination": destination, "car_codes": cars},
    )
    assert created["state"] == "DRAFT"


def run(api: ApiClient) -> None:
    _open_yard(api)

    # --- input 1: no transfer_code -> automatic, capacity-driven selection ---
    _create_outbound(api, "OB-A", "N4", ["C-N1-01"])
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-A/sequencer", {})
    selection = sequenced["transfer_selection"]
    run_a = sequenced["pull_run"]
    assert selection["mode"] == "AUTO", selection
    assert selection["required_cars"] == 2, selection
    assert selection["transfer_code"] == "X2", selection
    assert selection["capacity_cars"] == 6
    assert selection["available_cars"] == 6
    assert "X1" in selection["considered"] and "X2" in selection["considered"]
    assert run_a["transfer_code"] == "X2"
    assert run_a["transfer_mode"] == "AUTO"
    assert run_a["required_cars"] == 2
    assert run_a["transfer_capacity_cars"] == 6
    bays = _yard_bays(api)
    assert bays["X2"]["reserved_cars"] == 2
    assert bays["X2"]["available_cars"] == 4
    assert bays["X1"]["reserved_cars"] == 0

    # --- input 4: multiple lines, different capacities, existing plan fixed ---
    # X2 now has 4 free slots, X1 still 5; automatic selection must prefer X1
    # and must not silently move OB-A's reservation off X2.
    _create_outbound(api, "OB-B", "S2", ["C-T1-01"])
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-B/sequencer", {})
    selection = sequenced["transfer_selection"]
    assert selection["mode"] == "AUTO"
    assert selection["required_cars"] == 3, selection
    assert selection["transfer_code"] == "X1", selection
    assert selection["available_cars"] == 5
    assert run_a["transfer_code"] == "X2", "existing plan must not be rebound"
    bays = _yard_bays(api)
    assert bays["X1"]["reserved_cars"] == 3
    assert bays["X1"]["available_cars"] == 2
    assert bays["X2"]["reserved_cars"] == 2
    assert bays["X2"]["available_cars"] == 4

    # --- input 2: explicit X1 is honoured even when X2 has more room ----------
    _create_outbound(api, "OB-C", "W9", ["C-W1-01"])
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-C/sequencer",
        {"transfer_code": "X1"},
    )
    selection = sequenced["transfer_selection"]
    assert selection["mode"] == "EXPLICIT", selection
    assert selection["transfer_code"] == "X1", selection
    assert selection["required_cars"] == 1
    bays = _yard_bays(api)
    assert bays["X1"]["reserved_cars"] == 4
    assert bays["X1"]["available_cars"] == 1

    # --- input 3: unknown line is rejected, and the plan stays a draft --------
    _create_outbound(api, "OB-E", "E7", ["C-E1-01"])
    error = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-E/sequencer",
        {"transfer_code": "X9"},
    )
    assert error["code"] == "VALIDATION_ERROR", error
    assert error["fields"]["transfer_code"] == ["not found"], error
    yard = api.expect_ok("GET", "/api/yard")
    assert "RUN-OB-E" not in yard["metrics"]["active_runs"], yard
    # The dispatcher retries after correcting the form; the failed request did
    # not consume the plan.
    error = api.expect_error("POST", "/api/outbound-trains/OB-E/sequencer", {"transfer_code": "X9"})
    assert error["fields"]["transfer_code"] == ["not found"]

    # E7 needs 4 buffered: X1 has only 1 free, so explicit X1 is rejected on
    # capacity even though the line exists; the automatic retry lands on X2.
    error = api.expect_error(
        "POST",
        "/api/outbound-trains/OB-E/sequencer",
        {"transfer_code": "X1"},
    )
    assert error["fields"]["transfer_code"][0].startswith("capacity 5"), error
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-E/sequencer", {})
    selection = sequenced["transfer_selection"]
    assert selection["mode"] == "AUTO"
    assert selection["transfer_code"] == "X2", selection
    assert selection["required_cars"] == 4
    assert selection["available_cars"] == 4
    bays = _yard_bays(api)
    assert bays["X1"]["reserved_cars"] == 4
    assert bays["X2"]["reserved_cars"] == 6
    assert bays["X2"]["available_cars"] == 0

    # --- execution keeps the selected line and its occupancy traceable -------
    run_code = sequenced["pull_run"]["code"]
    assert run_code == "RUN-OB-E"
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 3})
    assert advanced["completed"] is False
    assert advanced["pull_run"]["transfer_code"] == "X2"
    bays = _yard_bays(api)
    assert bays["X2"]["cars"] == 3, "three blocker cars now physically buffered"
    assert bays["X2"]["reserved_cars"] == 6, "reservation accounting is unchanged"


def restart_tracking(data_dir: Path) -> None:
    server = start_server(data_dir=data_dir, extra_env=EXTRA_ENV)
    try:
        api = server.api
        bays = _yard_bays(api)
        assert bays["X2"]["cars"] == 3, "physical buffer occupancy survives restart"
        assert bays["X2"]["reserved_cars"] == 6, "plan reservations survive restart"
        run_view = api.expect_ok("GET", "/api/yard")
        assert "RUN-OB-E" in run_view["metrics"]["active_runs"]
        # Resume execution of the partially run plan after restart.
        advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-E/advance", {"steps": 200})
        assert advanced["completed"] is True
        pull_run = advanced["pull_run"]
        assert pull_run["state"] == "COMPLETED"
        assert pull_run["transfer_code"] == "X2"
        assert pull_run["transfer_mode"] == "AUTO"
        assert pull_run["required_cars"] == 4
        assert advanced["outbound"]["state"] == "READY"
        bays = _yard_bays(api)
        assert bays["X2"]["cars"] == 0, "buffered cars returned to their source track"
        # OB-E released its 4 slots; OB-A's earlier X2 reservation is intact.
        assert bays["X2"]["reserved_cars"] == 2
        assert bays["X1"]["reserved_cars"] == 4, "X1 plans are untouched by X2 work"
    finally:
        server.stop()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="switchyard-transfer-") as tmp:
        data_dir = Path(tmp) / "data"
        server: RunningServer | None = None
        try:
            server = start_server(data_dir=data_dir, extra_env=EXTRA_ENV)
            run(server.api)
            server.stop()
            server = None
            restart_tracking(data_dir)
        finally:
            if server is not None:
                server.stop()
    print("OK wf_transfer_selection")
    return 0


if __name__ == "__main__":
    sys.exit(main())
