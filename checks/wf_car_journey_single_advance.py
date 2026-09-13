"""Workflow check: a multi-step ticket completed by a single advance.

A three-step pull run (buffer blocker, pull target, return blocker) is advanced
once with enough steps to finish the whole ticket. The journal then contains
only PULL_PLANNED, PULL_RUN_STARTED and PULL_RUN_COMPLETED events - there is no
PULL_RUN_ADVANCED event. The journey profile must still bind the unique plan and
start records, time every executed step from the run's own completion event, and
must not invent AMBIGUOUS / MISSING evidence gaps.
"""

from __future__ import annotations

from support import ApiClient, run_check


def journey(api: ApiClient, code: str) -> dict:
    status, body = api.get(f"/api/car-journeys/{code}")
    assert status == 200 and body.get("ok"), f"{status} {body}"
    return dict(body["data"])


def entry(profile: dict, phase: str) -> dict:
    return next(item for item in profile["entries"] if item["phase"] == phase)


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-SA", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-SA",
            "route": "RSA",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": [
                {
                    "code": "C-TGT-SA",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-BLK-SA",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-SA/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-SA", "destination": "N4", "car_codes": ["C-TGT-SA"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-SA/sequencer", {"transfer_code": "X1"})
    pull_run = sequenced["pull_run"]
    run_code = pull_run["code"]
    verbs = [step["verb"] for step in pull_run["steps"]]
    assert verbs == ["BUFFER", "PULL", "RETURN"], verbs
    # No PULL_RUN_ADVANCED event is emitted by this single finishing advance.
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})
    assert advanced["completed"] is True
    assert advanced["pull_run"]["state"] == "COMPLETED"
    api.expect_ok("POST", "/api/outbound-trains/OB-SA/depart", {})

    target = journey(api, "C-TGT-SA")
    blocker = journey(api, "C-BLK-SA")

    # Target: full causal trajectory with every entry event-backed.
    assert [item["phase"] for item in target["entries"]] == [
        "RECEIVED",
        "CLASSIFIED",
        "RESERVED",
        "ASSEMBLED",
        "DEPARTED",
    ]
    reserved = entry(target, "RESERVED")
    assembled = entry(target, "ASSEMBLED")
    assert reserved["evidence"] == "event", reserved
    assert reserved["event_sequence"] is not None
    assert assembled["evidence"] == "event", assembled
    assert assembled["run_code"] == run_code
    for item in target["entries"]:
        assert item["at"], item
    # No fabricated gaps: the unique plan/start bind even without ADVANCED.
    assert target["evidence_gaps"] == [], target["evidence_gaps"]
    assert target["flags"] == [], target["flags"]
    assert target["consistent"] is True

    # Blocker: buffer and return are both timed by the run's own completion.
    assert [item["phase"] for item in blocker["entries"]] == [
        "RECEIVED",
        "CLASSIFIED",
        "BUFFERED",
        "RETURNED",
    ]
    buffered = entry(blocker, "BUFFERED")
    returned = entry(blocker, "RETURNED")
    completion_seq = assembled["event_sequence"]
    assert buffered["evidence"] == "event"
    assert returned["evidence"] == "event"
    assert buffered["event_sequence"] == completion_seq
    assert returned["event_sequence"] == completion_seq
    assert buffered["run_code"] == run_code
    assert returned["run_code"] == run_code
    assert blocker["evidence_gaps"] == [], blocker["evidence_gaps"]
    assert blocker["flags"] == [], blocker["flags"]

    # The blocker is back standing on its track; the target has departed.
    assert blocker["current"]["state"] == "STANDING"
    assert target["current"]["state"] == "DEPARTED"
    assert target["current"]["location"] == "OB-SA"


if __name__ == "__main__":
    raise SystemExit(run_check("wf_car_journey_single_advance", run))
