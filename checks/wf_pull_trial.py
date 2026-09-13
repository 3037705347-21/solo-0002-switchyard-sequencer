"""Workflow check: what-if pull plan trials.

The check drives the independent ``POST /api/pull-trials`` endpoint with the
four required candidate shapes:

1. a list pullable without any reversal (no buffer moves),
2. a feasible list that needs a reversal (buffer/return moves),
3. an illegal assembly order (a later car blocks an earlier car), and
4. a car already held by another formal plan.

It also verifies that trials never mutate state, agree with the formal
planning entry on the same snapshot, highlight conflicts that are new or gone
relative to the formal plan, and survive a service restart against the same
data directory without having left any run, reservation, or event behind.

The yard holds ten N4 cars stacked C-N4-01 (bottom) .. C-N4-10 (top). The
battery only touches C-N4-01/09/10, while every formal plan uses cars
C-N4-02 .. C-N4-08, so battery verdicts stay identical before and after the
restart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support import ApiClient, run_check_with_data_dir

TRIAL = "/api/pull-trials"

CARS = [f"C-N4-{index:02d}" for index in range(1, 11)]


def trial(api: ApiClient, car_codes: list[str], **extra: Any) -> dict[str, Any]:
    # Explicit baseline_code null suppresses automatic comparison with the
    # current formal plan; the battery checks raw feasibility.
    payload: dict[str, Any] = {
        "candidate_code": "TRIAL-BATTERY",
        "destination": "N4",
        "transfer_code": "X1",
        "car_codes": car_codes,
        "baseline_code": None,
    }
    payload.update(extra)
    data = api.expect_ok("POST", TRIAL, payload)
    return dict(data["trial"])


def conflict_sigs(result: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(conflict["code"], conflict["car_code"]) for conflict in result["conflicts"]]


def classify_ten_cars(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-T", "dispatcher": "RUI", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-T1",
            "route": "RAIL-T",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": [
                {
                    "code": code,
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
                for code in CARS
            ],
        },
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-T1/classify", {})
    spots = {spot["car_code"]: (spot["track_code"], spot["index"]) for spot in classified["spots"]}
    # Destination affinity puts every N4 car on N4-A in intake order, so the
    # stack bottom-to-top is C-N4-01 .. C-N4-10.
    assert all(spots[code] == ("N4-A", index) for index, code in enumerate(CARS)), spots


def run_battery(api: ApiClient) -> dict[str, Any]:
    """Run the four required trials and return their results."""
    results: dict[str, Any] = {}

    # 1. No reversal: the top two cars come straight off in order.
    no_reverse = trial(api, ["C-N4-10", "C-N4-09"])
    assert no_reverse["feasible"] is True, no_reverse
    assert no_reverse["needs_reverse"] is False
    assert no_reverse["reverse_count"] == 0
    assert no_reverse["buffer_move_count"] == 0
    assert no_reverse["max_transfer_occupancy"] == 0
    assert no_reverse["blocked_cars"] == []
    assert [step["verb"] for step in no_reverse["steps"]] == ["PULL", "PULL"]
    assert no_reverse["final_assembly_order"] == ["C-N4-10", "C-N4-09"]
    results["no_reverse"] = no_reverse

    # 2. Feasible but needs a reversal: C-N4-10 on top must be buffered to X1
    #    so C-N4-09 can come out, then C-N4-10 is returned.
    needs_reverse = trial(api, ["C-N4-09"])
    assert needs_reverse["feasible"] is True, needs_reverse
    assert needs_reverse["needs_reverse"] is True
    assert needs_reverse["reverse_count"] == 1
    assert needs_reverse["buffer_move_count"] == 1
    assert needs_reverse["max_transfer_occupancy"] == 1
    verbs = [(step["verb"], step["car_code"]) for step in needs_reverse["steps"]]
    assert verbs == [
        ("BUFFER", "C-N4-10"),
        ("PULL", "C-N4-09"),
        ("RETURN", "C-N4-10"),
    ], verbs
    assert needs_reverse["blocked_cars"] == [
        {
            "car_code": "C-N4-09",
            "track_code": "N4-A",
            "depth_from_top": 1,
            "blocker_car_codes": ["C-N4-10"],
        }
    ], needs_reverse["blocked_cars"]
    assert needs_reverse["final_assembly_order"] == ["C-N4-09"]
    results["needs_reverse"] = needs_reverse

    # 3. Illegal order: C-N4-10 must come off before C-N4-09, yet the
    #    candidate asks for C-N4-09 first.
    illegal_order = trial(api, ["C-N4-09", "C-N4-10"])
    assert illegal_order["feasible"] is False
    assert ("blocked-sequence", "C-N4-10") in conflict_sigs(illegal_order)
    results["illegal_order"] = illegal_order

    # 4. A car already held by another formal plan (OB-HOLD, created in run()).
    occupied = trial(api, ["C-N4-01"])
    assert occupied["feasible"] is False
    sigs = conflict_sigs(occupied)
    assert ("car-occupied", "C-N4-01") in sigs, sigs
    owner = next(conflict for conflict in occupied["conflicts"] if conflict["code"] == "car-occupied")
    assert owner["owner_outbound"] == "OB-HOLD"
    results["occupied"] = occupied

    return results


def run_conflict_diff(api: ApiClient) -> None:
    """New vs. resolved conflicts compared against the formal plan."""
    # Formal draft OB-BASE is illegally ordered: C-N4-05 sits above C-N4-03.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-BASE", "destination": "N4", "car_codes": ["C-N4-03", "C-N4-05"]},
    )

    # A legal, free single-car candidate removes the formal ordering conflict.
    payload = {
        "candidate_code": "TRIAL-DIFF",
        "destination": "N4",
        "transfer_code": "X1",
        "car_codes": ["C-N4-08"],
        "baseline_code": "OB-BASE",
    }
    resolved = dict(api.expect_ok("POST", TRIAL, payload)["trial"])
    assert resolved["compared_to"] == "OB-BASE"
    baseline_sigs = {(c["code"], c["car_code"]) for c in resolved["baseline_conflicts"]}
    assert baseline_sigs == {("blocked-sequence", "C-N4-05")}, baseline_sigs
    assert resolved["feasible"] is True
    assert {(c["code"], c["car_code"]) for c in resolved["resolved_conflicts"]} == baseline_sigs
    assert resolved["new_conflicts"] == []

    # A stack-legal candidate that borrows the formal plan's cars adds occupied
    # conflicts while the original ordering conflict disappears.
    payload["car_codes"] = ["C-N4-05", "C-N4-03"]
    mixed = dict(api.expect_ok("POST", TRIAL, payload)["trial"])
    new_sigs = {(c["code"], c["car_code"]) for c in mixed["new_conflicts"]}
    gone_sigs = {(c["code"], c["car_code"]) for c in mixed["resolved_conflicts"]}
    assert ("car-occupied", "C-N4-03") in new_sigs, new_sigs
    assert ("car-occupied", "C-N4-05") in new_sigs
    assert ("blocked-sequence", "C-N4-05") in gone_sigs, gone_sigs
    assert mixed["feasible"] is False

    # Auto-selection: omitting baseline_code compares against the earliest
    # active formal plan for the destination (OB-HOLD was created first).
    auto = dict(
        api.expect_ok(
            "POST",
            TRIAL,
            {
                "candidate_code": "TRIAL-AUTO",
                "destination": "N4",
                "transfer_code": "X1",
                "car_codes": ["C-N4-08"],
            },
        )["trial"]
    )
    assert auto["compared_to"] == "OB-HOLD"

    # A missing explicit baseline is reported rather than silently skipped.
    error = api.expect_error(
        "POST",
        TRIAL,
        {
            "candidate_code": "TRIAL-X",
            "destination": "N4",
            "transfer_code": "X1",
            "car_codes": ["C-N4-08"],
            "baseline_code": "OB-GHOST",
        },
    )
    assert error["code"] == "NOT_FOUND", error


def run_formal_parity(api: ApiClient) -> None:
    """The formal planning entry must reach the same verdict as the trial."""
    # Illegal order: formal sequencing rejects the same LIFO conflict.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-BAD", "destination": "N4", "car_codes": ["C-N4-02", "C-N4-04"]},
    )
    bad_error = api.expect_error("POST", "/api/outbound-trains/OB-BAD/sequencer", {"transfer_code": "X1"})
    assert bad_error["code"] == "VALIDATION_ERROR", bad_error
    assert bad_error["details"]["sequencer"] == ["blocked-sequence"], bad_error

    # Feasible reversal: capture the trial before formal planning reserves
    # the car, then confirm the formal run produces the exact same moves.
    # C-N4-06 is buried under 07..10, so four blockers are buffered.
    pre_plan_trial = trial(api, ["C-N4-06"])
    assert pre_plan_trial["feasible"] is True
    assert pre_plan_trial["reverse_count"] == 4
    assert pre_plan_trial["max_transfer_occupancy"] == 4
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-REV", "destination": "N4", "car_codes": ["C-N4-06"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-REV/sequencer", {"transfer_code": "X1"})
    formal_steps = [
        (s["verb"], s["car_code"], s["source_code"], s["target_code"])
        for s in sequenced["pull_run"]["steps"]
    ]
    trial_steps = [
        (
            s["verb"],
            s["car_code"],
            s["source_code"],
            s["target_code"].replace("TRIAL-BATTERY", "OB-REV"),
        )
        for s in pre_plan_trial["steps"]
    ]
    assert formal_steps == trial_steps, (formal_steps, trial_steps)
    assert sequenced["pull_run"]["code"] == "RUN-OB-REV"
    assert sequenced["outbound"]["state"] == "PLANNED"

    # Once formal planning reserved C-N4-06, a formal reuse is rejected
    # because the car is no longer standing; the trial reports the same car
    # as occupied by OB-REV on that snapshot.
    claim_error = api.expect_error(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-CLAIM", "destination": "N4", "car_codes": ["C-N4-06"]},
    )
    assert claim_error["code"] == "VALIDATION_ERROR", claim_error
    assert claim_error["details"]["car_codes"] == ["C-N4-06 is RESERVED"], claim_error
    reserved_trial = trial(api, ["C-N4-06"])
    assert reserved_trial["feasible"] is False
    assert ("car-occupied", "C-N4-06") in conflict_sigs(reserved_trial)
    assert reserved_trial["conflicts"][0]["owner_outbound"] == "OB-REV"

    # A car drafted but not yet reserved by another plan stays standing, so
    # formal creation rejects the reuse with RESOURCE_BUSY, the same occupied
    # verdict the trial produces.
    draft_error = api.expect_error(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-CLAIM-DRAFT", "destination": "N4", "car_codes": ["C-N4-03"]},
    )
    assert draft_error["code"] == "RESOURCE_BUSY", draft_error
    draft_trial = trial(api, ["C-N4-03"])
    assert draft_trial["feasible"] is False
    occupied_hits = [c for c in draft_trial["conflicts"] if c["code"] == "car-occupied"]
    assert occupied_hits, draft_trial["conflicts"]
    assert occupied_hits[0]["owner_outbound"] == "OB-BASE"


def run(api: ApiClient, data_dir: Path) -> None:
    classify_ten_cars(api)

    # Formal plan owning C-N4-01 before any trial runs.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-HOLD", "destination": "N4", "car_codes": ["C-N4-01"]},
    )

    before = api.expect_ok("GET", "/api/yard")["metrics"]
    battery = run_battery(api)

    # Repeat the whole battery: trials must not influence one another and the
    # verdicts stay identical no matter how many trials ran in between.
    repeated = run_battery(api)
    for name, result in battery.items():
        again = repeated[name]
        assert again["feasible"] == result["feasible"]
        assert again["steps"] == result["steps"]
        assert conflict_sigs(again) == conflict_sigs(result)

    after_trials = api.expect_ok("GET", "/api/yard")["metrics"]
    assert after_trials == before, "trials mutated yard state"

    run_conflict_diff(api)
    run_formal_parity(api)

    # Capture battery anchors only after every formal plan has landed. The
    # restarted service must reproduce exactly these verdicts against the same
    # persisted snapshot (C-N4-06 is reserved by then, which is a genuine
    # blocker-state change reflected in the simulated moves).
    final_metrics = api.expect_ok("GET", "/api/yard")["metrics"]
    final_battery = run_battery(api)
    assert api.expect_ok("GET", "/api/yard")["metrics"] == final_metrics, "trials mutated yard state"

    # Persist comparison anchors reused after the restart.
    anchors = {
        "metrics": final_metrics,
        "battery": {
            name: {
                "feasible": result["feasible"],
                "steps": result["steps"],
                "conflicts": conflict_sigs(result),
            }
            for name, result in final_battery.items()
        },
    }
    (data_dir.parent / "trial-anchors.json").write_text(json.dumps(anchors), encoding="utf-8")


def after_restart(api: ApiClient, data_dir: Path) -> None:
    anchors = json.loads((data_dir.parent / "trial-anchors.json").read_text(encoding="utf-8"))
    yard = api.expect_ok("GET", "/api/yard")["metrics"]
    assert yard == anchors["metrics"], "persisted state changed across restart"

    # Only the formal run produced by the planner exists; trials created
    # neither runs nor reservations.
    assert yard["active_runs"] == ["RUN-OB-REV"], yard["active_runs"]
    counts = yard["car_state_counts"]
    # OB-REV reserved C-N4-06; OB-HOLD/OB-BASE/OB-BAD remain drafts whose
    # cars were never reserved.
    assert counts["reserved"] == 1, counts
    assert counts["standing"] == 9, counts

    state_text = (data_dir / "yard-state.json").read_text(encoding="utf-8")
    journal_text = (data_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "TRIAL" not in state_text, "trial label leaked into persisted state"
    assert "TRIAL" not in journal_text, "trial left an event behind"
    assert "RUN-OB-REV" in state_text

    # The same verdicts hold on the restarted, persisted snapshot.
    restarted = run_battery(api)
    for name, anchor in anchors["battery"].items():
        result = restarted[name]
        assert result["feasible"] == anchor["feasible"], name
        assert result["steps"] == anchor["steps"], name
        # JSON round-trips tuples as lists; compare normalized signatures.
        assert [list(sig) for sig in conflict_sigs(result)] == [list(sig) for sig in anchor["conflicts"]], name

    # Re-running trials after restart still mutates nothing.
    assert api.expect_ok("GET", "/api/yard")["metrics"] == yard


run.after_restart = after_restart


if __name__ == "__main__":
    raise SystemExit(run_check_with_data_dir("wf_pull_trial", run))
