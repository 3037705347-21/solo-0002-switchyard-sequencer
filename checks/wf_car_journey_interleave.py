"""Workflow check: globally consistent run-event matching under interleaving.

Run A is a three-step buffer/pull/return plan; run B is a one-step pull created
after A. Execution interleaves: A buffers its blocker, B starts and completes
in one call, then A pulls and returns. Run journal events carry no run code, so
every start/advance/completion must be matched to the run the actual pull
records belong to - including the B lifecycle inserted in the middle of A.
Events whose owner is not unique must be reported as evidence gaps rather than
borrowed from the other run.
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
        {"code": "SHIFT-INT", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )

    def intake(code: str, cars: list[dict]) -> None:
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {"code": code, "route": "R", "arrival_at": "2026-09-13T09:00:00Z", "cars": cars},
        )
        api.expect_ok("POST", f"/api/intake-trains/{code}/classify", {})

    # N4 stack: C-TA under blocker C-XA (3-step plan); E7 holds a lone top car.
    intake(
        "INT-A",
        [
            {"code": "C-TA-2", "kind": "BOX", "destination": "N4", "loaded": True, "length_m": 18, "danger_class": "NONE"},
            {"code": "C-XA-2", "kind": "BOX", "destination": "N4", "loaded": True, "length_m": 18, "danger_class": "NONE"},
        ],
    )
    intake(
        "INT-B",
        [
            {"code": "C-TB-2", "kind": "FLAT", "destination": "E7", "loaded": True, "length_m": 16, "danger_class": "NONE"},
        ],
    )

    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-A2", "destination": "N4", "car_codes": ["C-TA-2"]})
    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-B2", "destination": "E7", "car_codes": ["C-TB-2"]})
    seq_a = api.expect_ok("POST", "/api/outbound-trains/OB-A2/sequencer", {"transfer_code": "X1"})
    seq_b = api.expect_ok("POST", "/api/outbound-trains/OB-B2/sequencer", {"transfer_code": "X1"})
    run_a, run_b = seq_a["pull_run"]["code"], seq_b["pull_run"]["code"]

    # Interleave: A starts and buffers the blocker; B then starts, pulls and
    # completes in a single call; A resumes with pull then return+completion.
    api.expect_ok("POST", f"/api/pull-runs/{run_a}/advance", {"steps": 1})
    api.expect_ok("POST", f"/api/pull-runs/{run_b}/advance", {"steps": 10})
    api.expect_ok("POST", f"/api/pull-runs/{run_a}/advance", {"steps": 1})
    api.expect_ok("POST", f"/api/pull-runs/{run_a}/advance", {"steps": 10})
    api.expect_ok("POST", "/api/outbound-trains/OB-B2/depart", {})
    api.expect_ok("POST", "/api/outbound-trains/OB-A2/depart", {})

    car_a = journey(api, "C-TA-2")
    blocker = journey(api, "C-XA-2")
    car_b = journey(api, "C-TB-2")

    # Every move is attributed to the run that actually performed it. A's
    # blocker buffer/pulldown never borrows B's single-call completion event.
    buffered = entry(blocker, "BUFFERED")
    returned = entry(blocker, "RETURNED")
    assert buffered["run_code"] == run_a, buffered
    assert returned["run_code"] == run_a, returned
    assert entry(car_a, "ASSEMBLED")["run_code"] == run_a
    assembled_b = entry(car_b, "ASSEMBLED")
    assert assembled_b["run_code"] == run_b, assembled_b

    # B's assembly is timed by its own completion event; it must not be reused
    # to time A's buffered step, which happened earlier on A's own advance.
    assert buffered["event_sequence"] is not None
    assert assembled_b["event_sequence"] is not None
    assert buffered["event_sequence"] < assembled_b["event_sequence"]
    # A's buffer uses an ADVANCED event; B's lone pull uses its COMPLETED event.
    assert buffered["move_verb"] == "BUFFER"
    assert assembled_b["move_verb"] == "PULL"

    # No move ambiguity gaps in a cleanly interleaved real yard: the global
    # matcher binds every start/advance/completion uniquely.
    for profile in (car_a, blocker, car_b):
        codes = [gap["code"] for gap in profile["evidence_gaps"]]
        assert "AMBIGUOUS_MOVE_EVENT" not in codes, profile["evidence_gaps"]
        assert "AMBIGUOUS_START_EVENT" not in codes, profile["evidence_gaps"]
        assert [flag["code"] for flag in profile["flags"]] == [], profile["flags"]

    # Trajectories stay causally ordered.
    assert [item["phase"] for item in blocker["entries"]] == [
        "RECEIVED",
        "CLASSIFIED",
        "BUFFERED",
        "RETURNED",
    ]
    assert [item["phase"] for item in car_a["entries"]] == [
        "RECEIVED",
        "CLASSIFIED",
        "RESERVED",
        "ASSEMBLED",
        "DEPARTED",
    ]
    assert [item["phase"] for item in car_b["entries"]] == [
        "RECEIVED",
        "CLASSIFIED",
        "RESERVED",
        "ASSEMBLED",
        "DEPARTED",
    ]


if __name__ == "__main__":
    raise SystemExit(run_check("wf_car_journey_interleave", run))
