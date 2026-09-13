"""Workflow check: assembly reconciliation between pull execution and departure.

Four scenarios, each on an isolated server:

1. aligned: every planned position matches the assembled consist, so the run
   completes and the train departs.
2. early_wrong: the crew pulls a planned car before its turn; reconciliation
   flags the sequence deviation at the position where it starts and departure
   stays blocked while the physical action is preserved.
3. duplicate: a pull is recorded twice on the consist; reconciliation flags
   the duplicate, the run cannot complete, and a ready train cannot depart.
4. backfill: a pulled car turns out to be missing, is reported, and is pulled
   again before departure; reconciliation recomputes clean and the train
   departs.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from support import ApiClient, RunningServer

SHIFT_AT = "2026-09-08T08:00:00Z"
ARRIVAL_AT = "2026-09-08T09:00:00Z"


def open_shift(api: ApiClient, code: str) -> None:
    api.expect_ok("POST", "/api/shifts", {"code": code, "dispatcher": "MA", "opened_at": SHIFT_AT})


def classify(api: ApiClient, intake_code: str, car_codes: list[str], hazard: str = "NONE") -> None:
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": intake_code,
            "route": f"RAIL-{intake_code}",
            "arrival_at": ARRIVAL_AT,
            "cars": [
                {
                    "code": code,
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": hazard,
                }
                for code in car_codes
            ],
        },
    )
    api.expect_ok("POST", f"/api/intake-trains/{intake_code}/classify", {})


def plan(api: ApiClient, outbound_code: str, car_codes: list[str]) -> str:
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": outbound_code, "destination": "N4", "car_codes": car_codes},
    )
    sequenced = api.expect_ok("POST", f"/api/outbound-trains/{outbound_code}/sequencer", {"transfer_code": "X1"})
    return str(sequenced["pull_run"]["code"])


def mutate_state(data_dir: Path, mutate) -> None:
    path = data_dir / "yard-state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def set_assembled(data_dir: Path, outbound_code: str, car_codes: list[str]) -> None:
    def apply(raw: dict) -> None:
        for outbound in raw["outbounds"]:
            if outbound["code"] == outbound_code:
                outbound["assembled_car_codes"] = car_codes

    mutate_state(data_dir, apply)


def scenario_aligned(api: ApiClient, data_dir: Path) -> None:
    open_shift(api, "SHIFT-A1")
    classify(api, "INT-A1", ["C-AL-01", "C-AL-02", "C-AL-03"])
    run_code = plan(api, "OB-A1", ["C-AL-03", "C-AL-01"])
    first = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    report = first["reconciliation"]
    assert report["status"] == "PENDING", report
    assert report["discrepancies"] == []
    assert [entry["status"] for entry in report["positions"]] == ["MATCH", "PENDING"]
    assert report["positions"][0]["planned_source"] == "N4-A"
    assert report["positions"][0]["actual_source"] == "N4-A"
    rest = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 3})
    assert rest["completed"] is True
    report = rest["reconciliation"]
    assert report["status"] == "ALIGNED", report
    assert [entry["status"] for entry in report["positions"]] == ["MATCH", "MATCH"]
    view = api.expect_ok("GET", f"/api/pull-runs/{run_code}/reconciliation")
    assert view["reconciliation"]["status"] == "ALIGNED"
    moves = view["pull_run"]["actual_moves"]
    assert [move["verb"] for move in moves] == ["PULL", "BUFFER", "PULL", "RETURN"]
    assert all(move["origin"] == "plan" for move in moves)
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-A1/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    assert departed["departed_car_count"] == 2


def scenario_early_wrong(api: ApiClient, data_dir: Path) -> None:
    open_shift(api, "SHIFT-B1")
    # C-EW-B1 is hazardous, so it is classified onto HAZ-1 while C-EW-A1 goes
    # to N4-A; both planned cars sit on a track top at the same time.
    classify(api, "INT-B1", ["C-EW-A1"])
    classify(api, "INT-B2", ["C-EW-B1"], hazard="D1")
    run_code = plan(api, "OB-B1", ["C-EW-A1", "C-EW-B1"])
    # The crew pulls C-EW-B1 onto the outbound before its planned turn.
    recorded = api.expect_ok(
        "POST",
        f"/api/pull-runs/{run_code}/moves",
        {"verb": "PULL", "car_code": "C-EW-B1", "source_code": "HAZ-1", "target_code": "OB-B1"},
    )
    report = recorded["reconciliation"]
    assert report["status"] == "DIVERGED", report
    assert report["first_deviation_position"] == 0
    sequence = [item for item in report["discrepancies"] if item["kind"] == "SEQUENCE"]
    assert sequence and sequence[0]["car_code"] == "C-EW-B1" and sequence[0]["position"] == 0
    position = report["positions"][0]
    assert position["planned_car"] == "C-EW-A1" and position["actual_car"] == "C-EW-B1"
    assert position["planned_source"] == "N4-A" and position["actual_source"] == "HAZ-1"
    # The physical action is preserved and the planned cursor is untouched.
    assert recorded["outbound"]["assembled_car_codes"] == ["C-EW-B1"]
    assert recorded["pull_run"]["current_step"] == 0
    moves = recorded["pull_run"]["actual_moves"]
    assert len(moves) == 1 and moves[0]["origin"] == "report" and moves[0]["car_code"] == "C-EW-B1"
    view = api.expect_ok("GET", f"/api/pull-runs/{run_code}/reconciliation")
    assert view["reconciliation"]["status"] == "DIVERGED"
    blocked = api.expect_error("POST", "/api/outbound-trains/OB-B1/depart", {})
    assert blocked["code"] in {"VALIDATION_ERROR", "CONFLICT"}


def scenario_duplicate(api: ApiClient, data_dir: Path) -> None:
    open_shift(api, "SHIFT-C1")
    classify(api, "INT-C1", ["C-DP-B1", "C-DP-A1"])
    run_code = plan(api, "OB-C1", ["C-DP-A1", "C-DP-B1"])
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    # A double scan records the same pull twice on the assembled consist.
    set_assembled(data_dir, "OB-C1", ["C-DP-A1", "C-DP-A1"])
    stuck = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    assert stuck["completed"] is False
    assert stuck["pull_run"]["state"] == "RUNNING"
    report = stuck["reconciliation"]
    assert report["status"] == "DIVERGED", report
    duplicates = [item for item in report["discrepancies"] if item["kind"] == "DUPLICATE"]
    assert duplicates and duplicates[0]["car_code"] == "C-DP-A1" and duplicates[0]["position"] == 1
    # Completed physical actions are kept for manual review, not rolled back.
    assert stuck["pull_run"]["current_step"] == 2
    assert len(stuck["pull_run"]["actual_moves"]) == 2
    assert stuck["outbound"]["assembled_car_codes"] == ["C-DP-A1", "C-DP-A1", "C-DP-B1"]
    api.expect_error("POST", "/api/outbound-trains/OB-C1/depart", {})
    # A second train completes cleanly, then a duplicate scan appears on the
    # consist before departure; the departure gate rejects it while READY.
    classify(api, "INT-C2", ["C-DP-B2", "C-DP-A2"])
    run_code_2 = plan(api, "OB-C2", ["C-DP-A2", "C-DP-B2"])
    done = api.expect_ok("POST", f"/api/pull-runs/{run_code_2}/advance", {"steps": 2})
    assert done["completed"] is True and done["reconciliation"]["status"] == "ALIGNED"
    set_assembled(data_dir, "OB-C2", ["C-DP-A2", "C-DP-B2", "C-DP-A2"])
    view = api.expect_ok("GET", f"/api/pull-runs/{run_code_2}/reconciliation")
    assert view["reconciliation"]["status"] == "DIVERGED"
    blocked = api.expect_error("POST", "/api/outbound-trains/OB-C2/depart", {})
    assert blocked["code"] == "CONFLICT"
    kinds = [item["kind"] for item in blocked["details"]["discrepancies"]]
    assert "DUPLICATE" in kinds
    # Reordering the plan to match the wrong consist cannot mask the problem:
    # reconciliation compares against the run's immutable pull steps.
    def tamper_plan(raw: dict) -> None:
        for outbound in raw["outbounds"]:
            if outbound["code"] == "OB-C2":
                outbound["planned_car_codes"] = ["C-DP-A2", "C-DP-B2", "C-DP-A2"]

    mutate_state(data_dir, tamper_plan)
    view = api.expect_ok("GET", f"/api/pull-runs/{run_code_2}/reconciliation")
    assert view["reconciliation"]["status"] == "DIVERGED"
    still_blocked = api.expect_error("POST", "/api/outbound-trains/OB-C2/depart", {})
    assert still_blocked["code"] == "CONFLICT"


def scenario_backfill(api: ApiClient, data_dir: Path) -> None:
    open_shift(api, "SHIFT-D1")
    classify(api, "INT-D1", ["C-BF-B1", "C-BF-A1"])
    run_code = plan(api, "OB-D1", ["C-BF-A1", "C-BF-B1"])
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    # The pulled car never made it onto the outbound; inspection finds it back
    # on the source track, still reserved for this train.
    def inject_miss(raw: dict) -> None:
        for outbound in raw["outbounds"]:
            if outbound["code"] == "OB-D1":
                outbound["assembled_car_codes"] = []
        for car in raw["cars"]:
            if car["code"] == "C-BF-A1":
                car["state"] = "RESERVED"
                car["location"] = "N4-A"
        for track in raw["tracks"]:
            if track["code"] == "N4-A":
                track["stack"] = ["C-BF-B1", "C-BF-A1"]

    mutate_state(data_dir, inject_miss)
    view = api.expect_ok("GET", f"/api/pull-runs/{run_code}/reconciliation")
    report = view["reconciliation"]
    assert report["status"] == "DIVERGED", report
    missing = [item for item in report["discrepancies"] if item["kind"] == "MISSING"]
    assert missing and missing[0]["car_code"] == "C-BF-A1" and missing[0]["position"] == 0
    assert report["positions"][0]["status"] == "MISSING"
    # The crew pulls the missing car again; the correction is a recorded move.
    refilled = api.expect_ok(
        "POST",
        f"/api/pull-runs/{run_code}/moves",
        {"verb": "PULL", "car_code": "C-BF-A1", "source_code": "N4-A", "target_code": "OB-D1"},
    )
    assert refilled["reconciliation"]["status"] == "PENDING", refilled["reconciliation"]
    assert refilled["outbound"]["assembled_car_codes"] == ["C-BF-A1"]
    finished = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    assert finished["completed"] is True
    assert finished["reconciliation"]["status"] == "ALIGNED"
    moves = finished["pull_run"]["actual_moves"]
    assert [(move["car_code"], move["origin"]) for move in moves] == [
        ("C-BF-A1", "plan"),
        ("C-BF-A1", "report"),
        ("C-BF-B1", "plan"),
    ]
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-D1/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    assert departed["departed_car_count"] == 2


SCENARIOS = [
    ("aligned", scenario_aligned),
    ("early_wrong", scenario_early_wrong),
    ("duplicate", scenario_duplicate),
    ("backfill", scenario_backfill),
]


def run_scenario(name: str, fn) -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-reconcile-") as tmp:
        server = RunningServer(data_dir=Path(tmp) / "data")
        try:
            server.wait_ready()
            fn(server.api, Path(tmp) / "data")
            print(f"OK wf_assembly_reconcile:{name}")
        finally:
            server.stop()


if __name__ == "__main__":
    for scenario_name, scenario_fn in SCENARIOS:
        run_scenario(scenario_name, scenario_fn)
