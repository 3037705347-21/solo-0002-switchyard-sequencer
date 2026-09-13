"""Workflow check: run event attribution when a later plan executes first.

Two outbound plans are created in order A then B, but run B is fully executed
before run A. Run journal events carry no run code, so the journey profile must
attribute every move to the run the actual pull records belong to. Events that
cannot be uniquely tied to a run (the two identical plan/departure events) must
be reported as evidence gaps instead of being guessed onto another car.
"""

from __future__ import annotations

from support import ApiClient, run_check


def journey(api: ApiClient, code: str) -> dict:
    status, body = api.get(f"/api/car-journeys/{code}")
    assert status == 200 and body.get("ok"), f"{status} {body}"
    return dict(body["data"])


def event_seq(profile: dict, phase: str) -> int | None:
    for entry in profile["entries"]:
        if entry["phase"] == phase:
            return entry["event_sequence"]
    return None


def run(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-OOO", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )

    def intake(code: str, route: str, cars: list[dict]) -> None:
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {"code": code, "route": route, "arrival_at": "2026-09-13T09:00:00Z", "cars": cars},
        )
        api.expect_ok("POST", f"/api/intake-trains/{code}/classify", {})

    # Each target sits below one blocker so every run has identical shape:
    # BUFFER blocker, PULL target, RETURN blocker (3 steps, transfer bay X1).
    intake(
        "INT-A",
        "RA",
        [
            {"code": "C-TA-1", "kind": "BOX", "destination": "N4", "loaded": True, "length_m": 18, "danger_class": "NONE"},
            {"code": "C-XA-1", "kind": "BOX", "destination": "N4", "loaded": True, "length_m": 18, "danger_class": "NONE"},
        ],
    )
    intake(
        "INT-B",
        "RB",
        [
            {"code": "C-TB-1", "kind": "BOX", "destination": "E7", "loaded": True, "length_m": 18, "danger_class": "NONE"},
            {"code": "C-XB-1", "kind": "BOX", "destination": "E7", "loaded": True, "length_m": 18, "danger_class": "NONE"},
        ],
    )

    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-A", "destination": "N4", "car_codes": ["C-TA-1"]})
    api.expect_ok("POST", "/api/outbound-trains", {"code": "OB-B", "destination": "E7", "car_codes": ["C-TB-1"]})
    seq_a = api.expect_ok("POST", "/api/outbound-trains/OB-A/sequencer", {"transfer_code": "X1"})
    seq_b = api.expect_ok("POST", "/api/outbound-trains/OB-B/sequencer", {"transfer_code": "X1"})
    run_a, run_b = seq_a["pull_run"]["code"], seq_b["pull_run"]["code"]

    # B (created second) executes fully first, one step at a time, then A.
    for _ in range(3):
        api.expect_ok("POST", f"/api/pull-runs/{run_b}/advance", {"steps": 1})
    for _ in range(3):
        api.expect_ok("POST", f"/api/pull-runs/{run_a}/advance", {"steps": 1})
    api.expect_ok("POST", "/api/outbound-trains/OB-B/depart", {})
    api.expect_ok("POST", "/api/outbound-trains/OB-A/depart", {})

    # Journal layout (sequence numbers): plan events 8,9; B lifecycle 10..13;
    # A lifecycle 14..17; departures 18,19.
    car_a = journey(api, "C-TA-1")
    car_b = journey(api, "C-TB-1")

    # The decisive assertion: A's assembly must come from A's completion window
    # (sequence 16/17), never from B's earlier advance at sequence 12.
    assert event_seq(car_a, "ASSEMBLED") == 16, car_a["entries"]
    assert event_seq(car_b, "ASSEMBLED") == 12, car_b["entries"]
    for entry in car_a["entries"]:
        if entry["phase"] == "ASSEMBLED":
            assert entry["run_code"] == run_a
    for entry in car_b["entries"]:
        if entry["phase"] == "ASSEMBLED":
            assert entry["run_code"] == run_b

    # Blockers are tied to the run that actually moved them.
    blocker_a = journey(api, "C-XA-1")
    blocker_b = journey(api, "C-XB-1")
    assert event_seq(blocker_a, "BUFFERED") == 15, blocker_a["entries"]
    assert event_seq(blocker_a, "RETURNED") == 17, blocker_a["entries"]
    assert event_seq(blocker_b, "BUFFERED") == 11, blocker_b["entries"]
    assert event_seq(blocker_b, "RETURNED") == 13, blocker_b["entries"]

    def gap_codes(profile: dict) -> list[str]:
        return [gap["code"] for gap in profile["evidence_gaps"]]

    # Identical same-second plan and departure events are genuinely ambiguous,
    # so reservation/departure times fall back to entity records and are
    # explicitly flagged rather than borrowed from the other car's events.
    for profile in (car_a, car_b):
        codes = gap_codes(profile)
        assert "AMBIGUOUS_PLAN_EVENT" in codes, profile["evidence_gaps"]
        assert "AMBIGUOUS_DEPARTURE_EVENT" in codes, profile["evidence_gaps"]
        reserved = next(entry for entry in profile["entries"] if entry["phase"] == "RESERVED")
        departed = next(entry for entry in profile["entries"] if entry["phase"] == "DEPARTED")
        assert reserved["evidence"] == "entity"
        assert reserved["event_sequence"] is None
        assert departed["evidence"] == "entity"
        assert departed["event_sequence"] is None
        # But the entity-backed times still come from the car's own run/train.
        assert reserved["run_code"] in {run_a, run_b}
        assert departed["outbound_code"] in {"OB-A", "OB-B"}

    # No state/location contradictions: entity records still agree with the
    # trajectory end even though two journal events could not be attributed.
    assert car_a["current"] == {"state": "DEPARTED", "location": "OB-A"}
    assert car_b["current"] == {"state": "DEPARTED", "location": "OB-B"}
    assert [flag["code"] for flag in car_a["flags"]] == [], car_a["flags"]
    assert [flag["code"] for flag in car_b["flags"]] == [], car_b["flags"]


if __name__ == "__main__":
    raise SystemExit(run_check("wf_car_journey_event_attribution", run))
