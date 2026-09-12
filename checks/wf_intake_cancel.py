"""Workflow check: cancel inbound trains before classification completes.

Walks three cancellation situations against the real HTTP API:

1. OPEN intake, never classified (fully unclassified): every car is removed.
2. PARTIAL intake after one classify attempt: unplaced cars stop blocking yard
   work, standing cars are popped from their stacks, while a car that already
   entered outbound planning is retained untouched.
3. CLASSIFIED intake: cancellation is rejected.

It also verifies yard metrics, the shift closure blocker list, uniqueness on
re-creation, and full history survival across a service restart.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from support import ApiClient, PROJECT_ROOT, RunningServer


def box(code: str, destination: str, *, length: int = 18, danger: str = "NONE") -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": True,
        "length_m": length,
        "danger_class": danger,
    }


def intake(code: str, route: str, cars: list[dict[str, object]]) -> dict[str, object]:
    return {
        "code": code,
        "route": route,
        "arrival_at": "2026-09-12T09:00:00Z",
        "cars": cars,
    }


def state_counts(api: ApiClient) -> dict[str, int]:
    return api.expect_ok("GET", "/api/yard")["metrics"]["car_state_counts"]


def track_map(api: ApiClient) -> dict[str, dict[str, object]]:
    yard = api.expect_ok("GET", "/api/yard")
    return {item["code"]: item for item in yard["metrics"]["track_metrics"]}


def blockers(api: ApiClient) -> list[dict[str, object]]:
    return api.expect_ok("GET", "/api/yard")["blockers"]


def run(api: ApiClient, data_dir: Path) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-70", "dispatcher": "CHENG", "opened_at": "2026-09-12T08:00:00Z"},
    )

    # ------------------------------------------------------------------
    # Scenario 1: fully unclassified OPEN intake is cancelled.
    # ------------------------------------------------------------------
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        intake(
            "INT-71",
            "RAIL-71",
            [box("C-N4-71A", "N4"), box("C-E7-71A", "E7"), box("C-S2-71A", "S2"), box("C-W9-71A", "W9")],
        ),
    )
    before = state_counts(api)
    assert before["received"] == 4
    n4 = track_map(api)["N4-A"]
    assert n4["cars"] == 0

    missing_reason = api.expect_error("POST", "/api/intake-trains/INT-71/cancel", {"reason": "   "})
    assert missing_reason["code"] == "VALIDATION_ERROR"

    cancel_71 = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-71/cancel",
        {"reason": "wrong train code entered"},
    )
    intake_71 = cancel_71["intake"]
    assert intake_71["state"] == "CANCELLED"
    assert intake_71["cancel_reason"] == "wrong train code entered"
    assert intake_71["cancelled_at"]
    assert intake_71["unplaced"] == []
    assert sorted(cancel_71["removed_car_codes"]) == ["C-E7-71A", "C-N4-71A", "C-S2-71A", "C-W9-71A"]
    assert cancel_71["retained_car_codes"] == []
    outcomes = {item["car_code"]: item for item in cancel_71["dispositions"]}
    assert outcomes["C-N4-71A"]["outcome"] == "REMOVED"
    assert outcomes["C-N4-71A"]["prior_state"] == "RECEIVED"
    assert outcomes["C-N4-71A"]["final_location"] == "REMOVED"

    after_71 = state_counts(api)
    assert after_71["removed"] == 4
    assert after_71["received"] == 0
    yard = api.expect_ok("GET", "/api/yard")
    assert "INT-71" not in yard["metrics"]["active_intakes"]

    # Cancelling twice is rejected.
    again = api.expect_error(
        "POST",
        "/api/intake-trains/INT-71/cancel",
        {"reason": "second attempt"},
    )
    assert again["code"] == "CONFLICT"

    # ------------------------------------------------------------------
    # Scenario 2: PARTIAL intake - mix of unplaced, standing, and a car
    # already committed to an outbound consist.
    # ------------------------------------------------------------------
    # Pre-load HAZ-1 (capacity 8) with seven hazardous cars so the two
    # tankers of INT-72 cannot both find hazard-rated space.
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        intake(
            "INT-72P",
            "RAIL-72P",
            [
                box("C-D1-P1", "W9", danger="D1"),
                box("C-D1-P2", "W9", danger="D1"),
                box("C-D1-P3", "W9", danger="D1"),
                box("C-D1-P4", "W9", danger="D1"),
                box("C-D1-P5", "W9", danger="D1"),
                box("C-D1-P6", "W9", danger="D1"),
                box("C-D1-P7", "W9", danger="D1"),
            ],
        ),
    )
    preload = api.expect_ok("POST", "/api/intake-trains/INT-72P/classify", {})
    assert preload["intake"]["state"] == "CLASSIFIED"
    assert len(preload["spots"]) == 7

    api.expect_ok(
        "POST",
        "/api/intake-trains",
        intake(
            "INT-72",
            "RAIL-72",
            [
                box("C-N4-72A", "N4"),
                box("C-N4-72B", "N4"),
                box("C-D1-73", "W9", danger="D1"),
                box("C-D1-74", "W9", danger="D1"),
            ],
        ),
    )
    partial = api.expect_ok("POST", "/api/intake-trains/INT-72/classify", {})
    assert partial["intake"]["state"] == "PARTIAL"
    # HAZ-1 holds 8 cars; the preload occupies 7, so exactly one D1 car spots
    # and the last one has nowhere hazard-rated to go.
    assert set(partial["unplaced"]) == {"C-D1-74"}
    spots = {item["car_code"]: item["track_code"] for item in partial["spots"]}
    assert spots["C-N4-72A"].startswith("N4-")
    assert spots["C-N4-72B"].startswith("N4-")
    assert spots["C-D1-73"] == "HAZ-1"

    # Commit C-N4-72A to an outbound plan before cancelling the intake.
    ob = api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-72", "destination": "N4", "car_codes": ["C-N4-72A"]},
    )
    assert ob["state"] == "DRAFT"
    api.expect_ok("POST", "/api/outbound-trains/OB-72/sequencer", {"transfer_code": "X1"})
    while True:
        advanced = api.expect_ok("POST", "/api/pull-runs/RUN-OB-72/advance", {"steps": 1})
        if advanced["completed"]:
            break

    cancel_72 = api.expect_ok(
        "POST",
        "/api/intake-trains/INT-72/cancel",
        {"reason": "destination E7 was recorded instead of W9"},
    )
    assert cancel_72["intake"]["state"] == "CANCELLED"
    assert cancel_72["intake"]["unplaced"] == []
    assert sorted(cancel_72["removed_car_codes"]) == ["C-D1-73", "C-D1-74", "C-N4-72B"]
    assert cancel_72["retained_car_codes"] == ["C-N4-72A"]
    dispositions = {item["car_code"]: item for item in cancel_72["dispositions"]}
    assert dispositions["C-N4-72A"]["outcome"] == "RETAINED"
    assert dispositions["C-N4-72B"]["prior_state"] == "STANDING"
    assert dispositions["C-D1-74"]["prior_state"] == "RECEIVED"

    metrics_72 = api.expect_ok("GET", "/api/yard")["metrics"]
    counts = metrics_72["car_state_counts"]
    # 4 from INT-71 + 3 removed here (incl. two placed and one unplaced).
    assert counts["removed"] == 7
    # C-N4-72A was pulled onto the outbound during the run -> ASSEMBLED.
    assert counts["assembled"] == 1
    assert counts["received"] == 0
    tracks = {item["code"]: item for item in metrics_72["track_metrics"]}
    # C-N4-72A was the pull target and C-N4-72B sat beneath it as a blocker;
    # both leave N4-A (one assembled, one removed), so the track is empty.
    assert tracks["N4-A"]["cars"] == 0
    assert tracks["N4-A"]["top_car"] is None
    haz = tracks["HAZ-1"]
    assert haz["cars"] == 7  # seven preload cars remain, C-D1-73 popped
    assert "INT-72" not in metrics_72["active_intakes"]
    # The completed outbound remains a live yard object.
    assert "OB-72" in metrics_72["active_outbounds"]

    # ---------------------------------------------------------------
    # Shift blocker list right after cancellation: the cancelled intakes
    # and their removed/unplaced cars are gone; only the ready outbound
    # still blocks the shift.
    # ---------------------------------------------------------------
    mid_block = blockers(api)
    mid_keys = {(item["kind"], item["code"]) for item in mid_block}
    assert not any(kind in {"intake", "unclassified_car"} for kind, _ in mid_keys)
    assert ("outbound", "OB-72") in mid_keys

    # Outbound work tied to the cancelled intake completes normally: the
    # retained car is assembled and can depart.
    departed = api.expect_ok("POST", "/api/outbound-trains/OB-72/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    assert state_counts(api)["departed"] == 1
    assert blockers(api) == []

    # Re-creation still obeys the uniqueness rules.
    dup_train = api.expect_error("POST", "/api/intake-trains", intake("INT-71", "RAIL-X", [box("C-N4-90A", "N4")]))
    assert dup_train["code"] == "CONFLICT"
    dup_car = api.expect_error(
        "POST",
        "/api/intake-trains",
        intake("INT-74", "RAIL-74", [box("C-N4-72A", "N4")]),
    )
    assert dup_car["code"] == "CONFLICT"

    # Unknown intake code.
    not_found = api.expect_error(
        "POST",
        "/api/intake-trains/INT-NOPE/cancel",
        {"reason": "ghost train"},
    )
    assert not_found["code"] == "NOT_FOUND"

    # ------------------------------------------------------------------
    # Scenario 3: a fully CLASSIFIED intake cannot be cancelled.
    # ------------------------------------------------------------------
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        intake("INT-73", "RAIL-73", [box("C-N4-73A", "N4"), box("C-N4-73B", "N4")]),
    )
    classified = api.expect_ok("POST", "/api/intake-trains/INT-73/classify", {})
    assert classified["intake"]["state"] == "CLASSIFIED"
    rejected = api.expect_error(
        "POST",
        "/api/intake-trains/INT-73/cancel",
        {"reason": "too late"},
    )
    assert rejected["code"] == "VALIDATION_ERROR"
    still = api.expect_ok("GET", "/api/yard")["metrics"]
    # A classified intake is terminal, so it is not in the active intake list;
    # its cars must still be standing and the train was not turned CANCELLED.
    assert "INT-73" not in still["active_intakes"]
    assert still["car_state_counts"]["standing"] == 9
    assert still["car_state_counts"]["removed"] == 7

    # ---------------------------------------------------------------
    # Final closure: cancelled trains leave no blockers; a classified
    # intake with all cars standing is not a blocker either.
    # ---------------------------------------------------------------
    final_block = blockers(api)
    keys = {(item["kind"], item["code"]) for item in final_block}
    assert not any(code.startswith("INT-71") or code.startswith("INT-72") for _, code in keys)
    assert not any(kind in {"intake", "unclassified_car"} for kind, _ in keys)

    closed = api.expect_ok("POST", "/api/shifts/SHIFT-70/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    assert closed["metrics"]["car_state_counts"]["removed"] == 7
    assert closed["metrics"]["car_state_counts"]["standing"] == 9  # 7 preload + 2 INT-73
    assert closed["metrics"]["car_state_counts"]["departed"] == 1

    shift = api.expect_ok("GET", "/api/shifts/SHIFT-70")
    event_kinds = [event["kind"] for event in shift["events"]]
    assert event_kinds.count("TRAIN_CANCELLED") == 2
    cancel_events = [event for event in shift["events"] if event["kind"] == "TRAIN_CANCELLED"]
    payloads = {event["message"]: event["payload"] for event in cancel_events}
    assert any("INT-71" in message for message in payloads)
    int72_payload = next(payload for message, payload in payloads.items() if "INT-72" in message)
    assert int72_payload["reason"] == "destination E7 was recorded instead of W9"
    assert int72_payload["retained_car_codes"] == ["C-N4-72A"]

    # ------------------------------------------------------------------
    # Restart: history, dispositions and terminal states survive reload.
    # ------------------------------------------------------------------
    journal_lines = (data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    journal = [json.loads(line) for line in journal_lines if line.strip()]
    assert [entry["kind"] for entry in journal].count("TRAIN_CANCELLED") == 2


def main() -> int:
    temp_dir = tempfile.TemporaryDirectory(prefix="switchyard-cancel-check-")
    data_dir = Path(temp_dir.name) / "data"
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        run(server.api, data_dir)

        server.stop()

        restarted = RunningServer(data_dir=data_dir)
        try:
            restarted.wait_ready()
            api = restarted.api
            yard = api.expect_ok("GET", "/api/yard")
            assert yard["active_shift"] == "NONE"
            counts = yard["metrics"]["car_state_counts"]
            assert counts["removed"] == 7
            assert counts["standing"] == 9
            assert counts["departed"] == 1
            assert yard["metrics"]["active_intakes"] == []

            shift = api.expect_ok("GET", "/api/shifts/SHIFT-70")
            assert shift["shift"]["state"] == "CLOSED"
            kinds = [event["kind"] for event in shift["events"]]
            assert kinds.count("TRAIN_CANCELLED") == 2

            cancel_events = [event for event in shift["events"] if event["kind"] == "TRAIN_CANCELLED"]
            messages = " ".join(event["message"] for event in cancel_events)
            assert "INT-71" in messages and "INT-72" in messages

            # Journal file from both processes is intact.
            journal_lines = (data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            assert len([line for line in journal_lines if '"TRAIN_CANCELLED"' in line]) == 2
        finally:
            restarted.stop()
    finally:
        temp_dir.cleanup()

    print(f"OK wf_intake_cancel (project root {PROJECT_ROOT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
