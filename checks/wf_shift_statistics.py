"""Workflow check: per-shift operation statistics, failure retry, and freeze.

Drives three shifts through the live HTTP API:

* SHIFT-S1 completes the full intake -> plan -> buffer/pull -> depart flow.
* SHIFT-S2 stays partially open with an unclassified intake and a queued run;
  its statistics must move live while never treating the missing terminal
  timestamps as zero.
* SHIFT-S3 injects a mid-run failure by corrupting the persisted yard state,
  verifies rollback and PULL_RUN_FAILED accounting, then retries and departs.

Every statistic is recomputed directly from the raw JSONL journal and compared
with the live numbers and, after closure, with the frozen closure snapshot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from support import PROJECT_ROOT, ApiClient, run_check

SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from switchyard.report.shift_stats import shift_statistics_from_events  # noqa: E402

STATE_FILE = "yard-state.json"
JOURNAL_FILE = "events.jsonl"


def _car(code: str, destination: str, kind: str = "BOX", danger: str = "NONE", length: int = 18) -> dict[str, Any]:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": danger,
    }


def open_shift(api: ApiClient, code: str) -> None:
    opened = api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": code, "dispatcher": "STATS", "opened_at": "2026-09-13T08:00:00Z"},
    )
    assert opened["state"] == "OPEN"


def receive(api: ApiClient, intake: str, route: str, arrival: str, cars: list[dict[str, Any]]) -> None:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {"code": intake, "route": route, "arrival_at": arrival, "cars": cars},
    )


def stats(api: ApiClient, shift: str) -> dict[str, Any]:
    body = api.expect_ok("GET", f"/api/shifts/{shift}/statistics")
    return body["shift_statistics"]


def journal_stats(data_dir: Path, shift: str) -> dict[str, Any]:
    events = [json.loads(line) for line in (data_dir / JOURNAL_FILE).read_text(encoding="utf-8").splitlines() if line.strip()]
    return shift_statistics_from_events(events, shift)


def assert_stats_equal(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if actual != expected:
        raise AssertionError(
            "statistics mismatch\nactual=" + json.dumps(actual, indent=2, sort_keys=True)
            + "\nexpected=" + json.dumps(expected, indent=2, sort_keys=True)
        )


def strip_wrapper(view_stats: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in view_stats.items() if key != "frozen"}


# ---------------------------------------------------------------------------
# SHIFT-S1: a fully completed shift
# ---------------------------------------------------------------------------


def drive_complete_shift(api: ApiClient) -> str:
    open_shift(api, "SHIFT-S1")
    receive(
        api,
        "INT-S11",
        "RAIL-S11",
        "2026-09-13T08:10:00Z",
        [
            _car("C-S1-N4-1", "N4"),
            _car("C-S1-N4-2", "N4"),
            _car("C-S1-N4-3", "N4", kind="REEFER", length=20),
            _car("C-S1-N4-4", "N4"),
            _car("C-S1-E7-1", "E7", kind="FLAT", length=16),
        ],
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-S11/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"

    mid = stats(api, "SHIFT-S1")
    assert mid["closed"] is False and mid["frozen"] is False
    intake_stage = mid["stages"]["intake_handling_s"]
    assert intake_stage["completed"] == 1 and intake_stage["samples"] == 1
    assert isinstance(intake_stage["average_s"], (int, float))
    assert mid["stages"]["pull_execution_s"]["completed"] == 0
    assert mid["destination_distribution"] == [
        {"destination": "E7", "received": 1, "classified": 1, "assembled": 0, "departed": 0},
        {"destination": "N4", "received": 4, "classified": 4, "assembled": 0, "departed": 0},
    ]
    turnover = {item["track_code"]: item for item in mid["track_turnover"]}
    assert turnover["N4-A"]["placements"] == 4 and turnover["N4-A"]["releases"] == 0
    assert turnover["E7-A"]["placements"] == 1

    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-S11", "destination": "N4", "car_codes": ["C-S1-N4-3", "C-S1-N4-1"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-S11/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    planned = {step["verb"]: 0 for step in sequenced["pull_run"]["steps"]}
    for step in sequenced["pull_run"]["steps"]:
        planned[step["verb"]] += 1
    assert planned["BUFFER"] >= 1 and planned["RETURN"] >= 1 and planned["PULL"] == 2

    live_before_advance = stats(api, "SHIFT-S1")
    assert live_before_advance["stages"]["pull_execution_s"]["in_progress"] == 1

    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 50})
    assert advanced["completed"] is True
    api.expect_ok("POST", "/api/outbound-trains/OB-S11/depart", {})

    # A second outbound with multi-advance execution proves that committed
    # ADVANCED segments accumulate into the run totals.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-S12", "destination": "N4", "car_codes": ["C-S1-N4-4", "C-S1-N4-2"]},
    )
    sequenced2 = api.expect_ok("POST", "/api/outbound-trains/OB-S12/sequencer", {"transfer_code": "X1"})
    run2 = sequenced2["pull_run"]["code"]
    second_steps = {"BUFFER": 0, "RETURN": 0, "PULL": 0}
    for step in sequenced2["pull_run"]["steps"]:
        second_steps[step["verb"]] += 1
    partial = api.expect_ok("POST", f"/api/pull-runs/{run2}/advance", {"steps": 1})
    assert partial["completed"] is False
    running = stats(api, "SHIFT-S1")
    run_rows = {item["run_code"]: item for item in running["details"]["pull_runs"]}
    assert run_rows[run2]["state"] == "RUNNING"
    assert run_rows[run2]["execution_s"] is None
    assert running["stages"]["pull_execution_s"]["completed"] == 1
    assert running["stages"]["pull_execution_s"]["in_progress"] == 1
    finished = api.expect_ok("POST", f"/api/pull-runs/{run2}/advance", {"steps": 50})
    assert finished["completed"] is True
    api.expect_ok("POST", "/api/outbound-trains/OB-S12/depart", {})

    live = strip_wrapper(stats(api, "SHIFT-S1"))
    assert live["stages"]["pull_execution_s"]["completed"] == 2
    assert live["stages"]["pull_execution_s"]["samples"] == 2
    assert live["stages"]["pull_execution_s"]["failed"] == 0
    assert live["stages"]["car_to_assembly_s"]["samples"] == 4
    assert live["stages"]["car_to_departure_s"]["samples"] == 4
    assert live["buffering"]["buffer_moves"] == planned["BUFFER"] + second_steps["BUFFER"]
    assert live["buffering"]["return_moves"] == planned["RETURN"] + second_steps["RETURN"]
    assert live["buffering"]["pull_moves"] == 4
    assert live["retries"]["failed_runs"] == 0
    turnover = {item["track_code"]: item for item in live["track_turnover"]}
    assert turnover["N4-A"] == {
        "track_code": "N4-A",
        "placements": 4,
        "releases": 4,
        "turnovers": 4,
        "cars_remaining": 0,
    }
    assert live["destination_distribution"] == [
        {"destination": "E7", "received": 1, "classified": 1, "assembled": 0, "departed": 0},
        {"destination": "N4", "received": 4, "classified": 4, "assembled": 4, "departed": 4},
    ]
    assert live["issues"] == []

    closed = api.expect_ok("POST", "/api/shifts/SHIFT-S1/close", {})
    frozen = closed["snapshot"]["shift_statistics"]
    assert frozen["closed"] is True
    assert frozen["stages"]["pull_execution_s"]["completed"] == 2
    served = stats(api, "SHIFT-S1")
    assert served["frozen"] is True
    assert_stats_equal(strip_wrapper(served), frozen)
    return "SHIFT-S1"


# ---------------------------------------------------------------------------
# SHIFT-S2: partial shift, stays open
# ---------------------------------------------------------------------------


def drive_partial_shift(api: ApiClient) -> str:
    open_shift(api, "SHIFT-S2")
    receive(
        api,
        "INT-S21",
        "RAIL-S21",
        "2026-09-13T10:00:00Z",
        [_car("C-S2-S2-1", "S2", kind="HOPPER", length=20)],
    )
    api.expect_ok("POST", "/api/intake-trains/INT-S21/classify", {})
    receive(
        api,
        "INT-S22",
        "RAIL-S22",
        "2026-09-13T10:30:00Z",
        [_car("C-S2-W9-1", "W9", kind="TANK", length=22, danger="D1")],
    )
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-S21", "destination": "S2", "car_codes": ["C-S2-S2-1"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-S21/sequencer", {"transfer_code": "X1"})
    queued_run = sequenced["pull_run"]["code"]

    live = strip_wrapper(stats(api, "SHIFT-S2"))
    assert live["closed"] is False
    intake_stage = live["stages"]["intake_handling_s"]
    assert intake_stage["completed"] == 1
    assert intake_stage["in_progress"] == 1
    intake_detail = {item["intake_code"]: item for item in live["details"]["intakes"]}
    assert intake_detail["INT-S21"]["status"] == "CLASSIFIED"
    assert intake_detail["INT-S22"]["status"] == "OPEN_OR_PARTIAL"
    assert intake_detail["INT-S22"]["duration_s"] is None
    assert intake_detail["INT-S22"]["classified_at"] is None
    exec_stage = live["stages"]["pull_execution_s"]
    assert exec_stage["completed"] == 0 and exec_stage["failed"] == 0 and exec_stage["in_progress"] == 1
    assert exec_stage["average_s"] is None and exec_stage["maximum_s"] is None
    run_detail = {item["run_code"]: item for item in live["details"]["pull_runs"]}
    assert run_detail[queued_run]["state"] == "QUEUED"
    assert run_detail[queued_run]["execution_s"] is None
    car_stage = live["stages"]["car_to_assembly_s"]
    assert car_stage["completed"] == 0 and car_stage["in_progress"] == 2
    assert {item["destination"] for item in live["destination_distribution"]} == {"S2", "W9"}

    # closure must be blocked while work is open, and must not freeze anything
    blocked = api.expect_error("POST", "/api/shifts/SHIFT-S2/close", {})
    assert blocked["code"] == "RESOURCE_BUSY"
    after = stats(api, "SHIFT-S2")
    assert after["frozen"] is False and after["closed"] is False
    return "SHIFT-S2"


def finish_partial_shift(api: ApiClient, data_dir: Path) -> dict[str, Any]:
    """Resolve the open work, close S2, and return its frozen statistics."""
    api.expect_ok("POST", "/api/intake-trains/INT-S22/classify", {})
    advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-S21/advance", {"steps": 10})
    assert advanced["completed"] is True
    api.expect_ok("POST", "/api/outbound-trains/OB-S21/depart", {})
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-S2/close", {})
    frozen = closed["snapshot"]["shift_statistics"]
    assert frozen["closed"] is True
    journal = journal_stats(data_dir, "SHIFT-S2")
    assert_stats_equal(frozen, journal)
    served = stats(api, "SHIFT-S2")
    assert served["frozen"] is True
    assert_stats_equal(strip_wrapper(served), frozen)
    return frozen


# ---------------------------------------------------------------------------
# SHIFT-S3: failed attempt, rollback, retry, departure, closure freeze
# ---------------------------------------------------------------------------


def tamper_car_state(data_dir: Path, car_code: str, state: str) -> None:
    state_path = data_dir / STATE_FILE
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    for car in raw["cars"]:
        if car["code"] == car_code:
            car["state"] = state
    state_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def drive_failed_retry_shift(api: ApiClient, data_dir: Path) -> None:
    open_shift(api, "SHIFT-S3")
    receive(
        api,
        "INT-S31",
        "RAIL-S31",
        "2026-09-13T11:00:00Z",
        [
            _car("C-S3-N4-1", "N4"),
            _car("C-S3-N4-2", "N4"),
            _car("C-S3-N4-3", "N4"),
            _car("C-S3-N4-4", "N4"),
        ],
    )
    api.expect_ok("POST", "/api/intake-trains/INT-S31/classify", {})
    # Stack bottom->top is N4-1, N4-2, N4-3, N4-4. Planning [N4-4, N4-2]
    # derives: PULL N4-4, BUFFER N4-3, PULL N4-2, RETURN N4-3. The first two
    # advances commit a pull and, crucially, a buffer before the later failure.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-S31", "destination": "N4", "car_codes": ["C-S3-N4-4", "C-S3-N4-2"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-S31/sequencer", {"transfer_code": "X1"})
    first_run = sequenced["pull_run"]["code"]
    first_steps = sequenced["pull_run"]["steps"]
    verbs = [(step["verb"], step["car_code"]) for step in first_steps]
    assert verbs == [
        ("PULL", "C-S3-N4-4"),
        ("BUFFER", "C-S3-N4-3"),
        ("PULL", "C-S3-N4-2"),
        ("RETURN", "C-S3-N4-3"),
    ]

    # Commit the pull and the buffer over two advances. After this the blocker
    # C-S3-N4-3 is parked in the transfer bay and the first car is assembled.
    first = api.expect_ok("POST", f"/api/pull-runs/{first_run}/advance", {"steps": 1})
    assert first["completed"] is False
    second = api.expect_ok("POST", f"/api/pull-runs/{first_run}/advance", {"steps": 1})
    assert second["completed"] is False
    bay_view = {bay["code"]: bay for bay in api.expect_ok("GET", "/api/yard")["metrics"]["transfer_bays"]}
    assert bay_view["X1"]["cars"] == 1 and bay_view["X1"]["top_car"] == "C-S3-N4-3"

    # Make the next PULL of C-S3-N4-2 impossible while a car is still buffered.
    tamper_car_state(data_dir, "C-S3-N4-2", "REMOVED")
    failed = api.expect_error("POST", f"/api/pull-runs/{first_run}/advance", {"steps": 50})
    assert failed["code"] == "STATE_TRANSITION"

    # Full attempt recovery: the assembled car is returned to the track, the
    # buffered blocker is given back, the bay is empty, and the yard state is
    # back to the pre-attempt standing arrangement.
    yard_after = api.expect_ok("GET", "/api/yard")["metrics"]
    assert yard_after["car_state_counts"]["assembled"] == 0
    bay_after = {bay["code"]: bay for bay in yard_after["transfer_bays"]}
    assert bay_after["X1"]["cars"] == 0 and bay_after["X1"]["top_car"] is None
    track = {item["code"]: item for item in yard_after["track_metrics"]}
    assert track["N4-A"]["cars"] == 4 and track["N4-A"]["top_car"] == "C-S3-N4-4"

    run_view = api.expect_ok("GET", "/api/shifts/SHIFT-S3")
    assert any(event["kind"] == "PULL_RUN_FAILED" for event in run_view["events"])
    failed_payload = next(
        event for event in run_view["events"] if event["kind"] == "PULL_RUN_FAILED"
    )["payload"]
    rolled = [(step["verb"], step["car_code"]) for step in failed_payload["rolled_back_steps"]]
    assert rolled == [("PULL", "C-S3-N4-4"), ("BUFFER", "C-S3-N4-3")]

    failed_stats = strip_wrapper(stats(api, "SHIFT-S3"))
    assert failed_stats["retries"]["failed_runs"] == 1
    assert failed_stats["stages"]["pull_execution_s"]["failed"] == 1
    assert failed_stats["stages"]["pull_execution_s"]["average_s"] is None
    assert failed_stats["buffering"]["pull_moves"] == 0
    assert failed_stats["buffering"]["buffer_moves"] == 0
    assert failed_stats["buffering"]["moves_in_failed_attempts"] == {"buffer": 1, "return": 0, "pull": 1}
    assert failed_stats["buffering"]["buffer_moves_in_failed_attempts"] == 1
    run_detail = {item["run_code"]: item for item in failed_stats["details"]["pull_runs"]}
    failed_run = run_detail[first_run]
    assert failed_run["state"] == "FAILED"
    assert failed_run["execution_s"] is None
    assert failed_run["moves"] == {"buffer": 0, "return": 0, "pull": 0}
    assert failed_run["rolled_back_moves"] == {"buffer": 1, "return": 0, "pull": 1}
    assert failed_run["rolled_back_move_count"] == 2
    assert failed_run["error"]
    # the failed attempt left no net track turnover
    turnover = {item["track_code"]: item for item in failed_stats["track_turnover"]}
    assert turnover["N4-A"]["releases"] == 0
    assert turnover["N4-A"]["placements"] == 4
    # no car timing is counted as completed from a rolled-back attempt
    car_stage = failed_stats["stages"]["car_to_assembly_s"]
    assert car_stage["completed"] == 0 and car_stage["average_s"] is None

    # The failed run cannot be advanced again...
    again = api.expect_error("POST", f"/api/pull-runs/{first_run}/advance", {"steps": 1})
    assert again["code"] == "CONFLICT"

    # ...restore the target car and retry with a fresh attempt run. The retry
    # re-buffers the blocker, returns it, and completes both planned pulls.
    tamper_car_state(data_dir, "C-S3-N4-2", "STANDING")
    retried = api.expect_ok("POST", "/api/outbound-trains/OB-S31/retry", {"transfer_code": "X1"})
    second_run = retried["pull_run"]["code"]
    assert second_run == "RUN-OB-S31-R2"
    assert retried["pull_run"]["attempt"] == 2
    assert retried["outbound"]["state"] == "PLANNED"
    advanced = api.expect_ok("POST", f"/api/pull-runs/{second_run}/advance", {"steps": 50})
    assert advanced["completed"] is True
    bay_ok = {bay["code"]: bay for bay in api.expect_ok("GET", "/api/yard")["metrics"]["transfer_bays"]}
    assert bay_ok["X1"]["cars"] == 0
    api.expect_ok("POST", "/api/outbound-trains/OB-S31/depart", {})

    recovered = strip_wrapper(stats(api, "SHIFT-S3"))
    assert recovered["retries"] == {
        "failed_runs": 1,
        "retry_runs_planned": 1,
        "outbounds_with_retries": 1,
        "outbounds_departed": 1,
    }
    assert recovered["stages"]["pull_execution_s"]["completed"] == 1
    assert recovered["stages"]["pull_execution_s"]["failed"] == 1
    # only the completed attempt counts effective yard work
    assert recovered["buffering"]["buffer_moves"] == 1
    assert recovered["buffering"]["return_moves"] == 1
    assert recovered["buffering"]["pull_moves"] == 2
    assert recovered["buffering"]["moves_in_failed_attempts"] == {"buffer": 1, "return": 0, "pull": 1}
    outbound_detail = {item["outbound_code"]: item for item in recovered["details"]["outbounds"]}
    outbound = outbound_detail["OB-S31"]
    assert outbound["attempts"] == 2
    assert outbound["retry_count"] == 1
    assert outbound["failed_attempts"] == 1
    assert outbound["departed"] is True
    turnover = {item["track_code"]: item for item in recovered["track_turnover"]}
    assert turnover["N4-A"]["turnovers"] == 2
    assert turnover["N4-A"]["cars_remaining"] == 1

    closed = api.expect_ok("POST", "/api/shifts/SHIFT-S3/close", {})
    frozen = closed["snapshot"]["shift_statistics"]
    assert frozen["closed"] is True
    journal = journal_stats(data_dir, "SHIFT-S3")
    assert_stats_equal(frozen, journal)

    served = stats(api, "SHIFT-S3")
    assert served["frozen"] is True
    assert_stats_equal(strip_wrapper(served), frozen)

    recompute = api.expect_ok("GET", "/api/shifts/SHIFT-S3/statistics/recompute")["shift_statistics"]
    assert_stats_equal(recompute, frozen)


def run(api: ApiClient, data_dir: Path | None = None) -> None:
    complete_shift = drive_complete_shift(api)
    partial_shift = drive_partial_shift(api)

    # The closed complete shift serves a frozen snapshot. A raw journal
    # rebuild must reproduce it exactly.
    if data_dir is not None:
        s1_frozen = strip_wrapper(stats(api, complete_shift))
        assert s1_frozen["closed"] is True
        assert_stats_equal(journal_stats(data_dir, complete_shift), s1_frozen)

    if data_dir is None:
        return

    # Capture the partial shift's live numbers while it is still open.
    partial_live = strip_wrapper(stats(api, partial_shift))
    partial_journal = journal_stats(data_dir, partial_shift)
    assert_stats_equal(partial_live, partial_journal)
    assert partial_live["stages"]["intake_handling_s"]["in_progress"] == 1
    partial_codes = {car["car_code"] for car in partial_live["details"]["cars"]}
    assert partial_live["closed"] is False

    # Resolve the open work and freeze S2 so S3 can open.
    partial_frozen = finish_partial_shift(api, data_dir)
    assert partial_frozen["stages"]["intake_handling_s"]["completed"] == 2
    assert partial_frozen["stages"]["pull_execution_s"]["completed"] == 1

    drive_failed_retry_shift(api, data_dir)

    # SHIFT-S1 journal recomputation after later shifts must be unchanged and
    # must not contain S2/S3 cars; its frozen snapshot keeps serving.
    s1_final = journal_stats(data_dir, complete_shift)
    codes = {car["car_code"] for car in s1_final["details"]["cars"]}
    assert codes == {"C-S1-N4-1", "C-S1-N4-2", "C-S1-N4-3", "C-S1-N4-4", "C-S1-E7-1"}
    assert s1_final["retries"]["failed_runs"] == 0
    s1_served = stats(api, complete_shift)
    assert s1_served["frozen"] is True
    assert_stats_equal(strip_wrapper(s1_served), s1_final)

    # S2's frozen snapshot covers both intakes and the completed run only.
    s2_final = journal_stats(data_dir, partial_shift)
    assert s2_final["event_count"] == partial_frozen["event_count"]
    assert {car["car_code"] for car in s2_final["details"]["cars"]} == partial_codes
    assert s2_final["retries"]["failed_runs"] == 0
    s2_served = stats(api, partial_shift)
    assert s2_served["frozen"] is True
    assert_stats_equal(strip_wrapper(s2_served), s2_final)


def main() -> int:
    # run_check does not expose the data directory, so drive the server here
    # when invoked directly so failure injection can target the state file.
    from support import RunningServer

    server = RunningServer()
    try:
        server.wait_ready()
        run(server.api, server.data_dir)
    finally:
        server.stop()
    print("OK wf_shift_statistics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
