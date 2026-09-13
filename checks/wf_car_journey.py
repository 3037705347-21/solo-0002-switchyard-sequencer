"""Workflow check: per-car journey profiles over the HTTP API.

Drives one car through the full intake -> classify -> reserve -> buffer/pull
(of another car) -> assemble -> depart journey and one car that is received and
left parked. Verifies trajectory order, associations, consistency hints,
read-only deterministic queries, noise-event filtering, and the 404 path.
"""

from __future__ import annotations

import json

from support import ApiClient, run_check

JOURNEY_PATH = "/api/car-journeys"


def journey(api: ApiClient, code: str) -> dict:
    status, body = api.get(f"{JOURNEY_PATH}/{code}")
    assert status == 200 and body.get("ok"), f"journey {code} failed: {status} {body}"
    return dict(body["data"])


def setup_yard(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-09", "dispatcher": "LIN", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-91",
            "route": "RAIL-91",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": [
                {
                    "code": "C-FULL-91",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-BLK-91",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-IDLE-91",
                    "kind": "HOPPER",
                    "destination": "E7",
                    "loaded": True,
                    "length_m": 20,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-91/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-91", "destination": "N4", "car_codes": ["C-FULL-91"]},
    )
    sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-91/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    # First advance only buffers the blocker C-BLK-91; final advance pulls C-FULL-91.
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 1})
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})
    api.expect_ok("POST", "/api/outbound-trains/OB-91/depart", {})

    # Second intake: every destination track is full would be too brittle, so
    # force a partial classification by sending a car whose destination has no
    # receiving capacity state. Instead use a second car on an intake left
    # OPEN by never classifying it: it stays RECEIVED at INTAKE.
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-92",
            "route": "RAIL-92",
            "arrival_at": "2026-09-13T10:30:00Z",
            "cars": [
                {
                    "code": "C-WAIT-92",
                    "kind": "BOX",
                    "destination": "E7",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    # INT-92 is deliberately never classified, so C-WAIT-92 remains received.
    # The open intake plus the RECEIVED car keep closure blocked; CLOSURE_BLOCKED
    # is a plain operational message and must never become a trajectory phase.
    api.expect_error("POST", "/api/shifts/SHIFT-09/close")


def check_full_journey(api: ApiClient) -> None:
    full = journey(api, "C-FULL-91")
    assert full["current"]["state"] == "DEPARTED"
    assert full["current"]["location"] == "OB-91"

    phases = [entry["phase"] for entry in full["entries"]]
    assert phases == ["RECEIVED", "CLASSIFIED", "RESERVED", "ASSEMBLED", "DEPARTED"], phases
    assert full["expected_terminal"] == {"state": "DEPARTED", "location": "OB-91"}
    assert full["flags"] == [], full["flags"]
    assert full["evidence_gaps"] == [], full["evidence_gaps"]
    assert full["consistent"] is True

    received, classified, reserved, assembled, departed = full["entries"]
    assert received["intake_code"] == "INT-91"
    assert received["location"] == "INTAKE"
    assert classified["intake_code"] == "INT-91"
    assert classified["track_code"].startswith("N4-")
    assert reserved["run_code"] == "RUN-OB-91"
    assert reserved["outbound_code"] == "OB-91"
    assert reserved["track_code"] == classified["track_code"]
    assert assembled["run_code"] == "RUN-OB-91"
    assert assembled["outbound_code"] == "OB-91"
    assert assembled["from_location"] == classified["track_code"]
    assert assembled["location"] == "OB-91"
    assert assembled["assembly_position"] == 1
    assert assembled["move_verb"] == "PULL"
    assert departed["outbound_code"] == "OB-91"
    assert departed["location"] == "OB-91"

    # Every phase is journal-backed and carries an increasing event sequence.
    for entry in full["entries"]:
        assert entry["evidence"] == "event", entry
        assert entry["at"]
        assert entry["shift_code"] == "SHIFT-09"
    sequences = [entry["event_sequence"] for entry in full["entries"]]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences), sequences
    assert [entry["index"] for entry in full["entries"]] == list(range(len(full["entries"])))

    associations = full["associations"]
    assert associations["intake"] == {
        "code": "INT-91",
        "route": "RAIL-91",
        "arrival_at": "2026-09-13T09:00:00Z",
    }
    assert [item["code"] for item in associations["outbounds"]] == ["OB-91"]
    outbound = associations["outbounds"][0]
    assert outbound["state"] == "DEPARTED"
    assert outbound["planned_position"] == 1
    assert outbound["assembled_position"] == 1
    assert outbound["departed_at"]
    assert [item["code"] for item in associations["pull_runs"]] == ["RUN-OB-91"]
    run = associations["pull_runs"][0]
    assert run["car_role"] == "PLANNED"
    assert associations["shifts"] == ["SHIFT-09"]


def check_blocker_journey(api: ApiClient) -> None:
    blocker = journey(api, "C-BLK-91")
    assert blocker["current"]["state"] == "STANDING"
    phases = [entry["phase"] for entry in blocker["entries"]]
    assert phases == ["RECEIVED", "CLASSIFIED", "BUFFERED", "RETURNED"], phases
    assert blocker["flags"] == [], blocker["flags"]
    assert blocker["evidence_gaps"] == [], blocker["evidence_gaps"]
    buffered, returned = blocker["entries"][2], blocker["entries"][3]
    assert buffered["move_verb"] == "BUFFER"
    assert buffered["from_location"].startswith("N4-")
    assert buffered["location"] == "X1"
    assert buffered["transfer_code"] == "X1"
    assert buffered["run_code"] == "RUN-OB-91"
    assert returned["move_verb"] == "RETURN"
    assert returned["from_location"] == "X1"
    assert returned["location"] == buffered["from_location"]
    run = blocker["associations"]["pull_runs"][0]
    assert run["car_role"] == "BLOCKER"
    assert blocker["associations"]["outbounds"] == []


def check_idle_journey(api: ApiClient) -> None:
    # C-WAIT-92 was received with INT-92 and never classified: it stays at
    # RECEIVED / INTAKE. Only the surviving intake fragment is reported.
    idle = journey(api, "C-WAIT-92")
    assert idle["current"]["state"] == "RECEIVED"
    assert idle["current"]["location"] == "INTAKE"
    phases = [entry["phase"] for entry in idle["entries"]]
    assert phases == ["RECEIVED"], phases
    assert idle["expected_terminal"] == {"state": "RECEIVED", "location": "INTAKE"}
    assert idle["flags"] == [], idle["flags"]
    assert idle["evidence_gaps"] == [], idle["evidence_gaps"]
    assert idle["consistent"] is True
    received = idle["entries"][0]
    assert received["intake_code"] == "INT-92"
    assert received["location"] == "INTAKE"
    assert received["evidence"] == "event"
    assert idle["associations"]["intake"] == {
        "code": "INT-92",
        "route": "RAIL-92",
        "arrival_at": "2026-09-13T10:30:00Z",
    }
    assert idle["associations"]["outbounds"] == []
    assert idle["associations"]["pull_runs"] == []
    # The CLOSURE_BLOCKED noise event must not surface as a phase.
    assert "SHIFT_CLOSED" not in phases and "CLOSURE_BLOCKED" not in phases

    # The classified-but-undeparted C-IDLE-91 stays standing on its track.
    parked = journey(api, "C-IDLE-91")
    assert parked["current"]["state"] == "STANDING"
    assert [entry["phase"] for entry in parked["entries"]] == ["RECEIVED", "CLASSIFIED"]
    assert parked["flags"] == []
    assert parked["evidence_gaps"] == []


def check_index_and_stability(api: ApiClient) -> None:
    status, body = api.get(JOURNEY_PATH)
    assert status == 200 and body.get("ok"), body
    index = body["data"]
    assert index["car_count"] == 4
    rows = {item["code"]: item for item in index["cars"]}
    assert rows["C-FULL-91"]["phases"] == ["RECEIVED", "CLASSIFIED", "RESERVED", "ASSEMBLED", "DEPARTED"]
    assert rows["C-FULL-91"]["consistent"] is True
    assert rows["C-WAIT-92"]["phases"] == ["RECEIVED"]
    assert rows["C-WAIT-92"]["state"] == "RECEIVED"
    assert [item["code"] for item in index["cars"]] == sorted(rows)

    # Repeated queries return byte-identical documents and do not change state.
    before = journey(api, "C-FULL-91")
    yard_before = api.expect_ok("GET", "/api/yard")
    again = journey(api, "C-FULL-91")
    once_more = journey(api, "C-FULL-91")
    yard_after = api.expect_ok("GET", "/api/yard")
    assert json.dumps(before, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert json.dumps(again, sort_keys=True) == json.dumps(once_more, sort_keys=True)
    assert yard_before["metrics"] == yard_after["metrics"]
    assert yard_before["metrics"]["event_count"] == yard_after["metrics"]["event_count"]


def check_unknown_car(api: ApiClient) -> None:
    status, body = api.get(f"{JOURNEY_PATH}/C-NOPE-99")
    assert status == 404, body
    assert body["ok"] is False
    assert body["error"]["code"] == "NOT_FOUND"


def run(api: ApiClient) -> None:
    setup_yard(api)
    check_full_journey(api)
    check_blocker_journey(api)
    check_idle_journey(api)
    check_index_and_stability(api)
    check_unknown_car(api)


if __name__ == "__main__":
    raise SystemExit(run_check("wf_car_journey", run))
