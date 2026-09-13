"""Workflow check: closure certificates prove a tamper-free shift handoff.

Scenarios:
1. A normal closure issues a stable certificate that verifies cleanly.
2. Editing a snapshot field in the state file is reported with the exact field.
3. Deleting one event is reported inside the certified event range.
4. Repeated verification is deterministic, nothing is auto-rewritten, and
   restoring the original state makes verification pass again.
A final scenario confirms that a certificate storage failure cannot break an
already committed closure.
"""

from __future__ import annotations

import json
from pathlib import Path

from support import RunningServer


def read_state(data_dir: Path) -> dict:
    return json.loads((data_dir / "yard-state.json").read_text(encoding="utf-8"))


def write_state(data_dir: Path, state: dict) -> None:
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    (data_dir / "yard-state.json").write_text(text + "\n", encoding="utf-8")


def car(code: str, kind: str, destination: str) -> dict:
    return {
        "code": code,
        "kind": kind,
        "destination": destination,
        "loaded": True,
        "length_m": 18,
        "danger_class": "NONE",
    }


def open_and_classify(api, shift_code: str, intake_code: str, cars: list) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": shift_code, "dispatcher": "KE", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": intake_code,
            "route": f"RAIL-{intake_code}",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": cars,
        },
    )
    api.expect_ok("POST", f"/api/intake-trains/{intake_code}/classify", {})


def verify(api, shift_code: str) -> dict:
    return api.expect_ok("POST", f"/api/shifts/{shift_code}/closure-certificate/verify", {})


def run(api, data_dir: Path) -> None:
    # Scenario 1: a normal closure issues a stable, verifiable certificate.
    open_and_classify(
        api,
        "SHIFT-07",
        "INT-71",
        [car("C-N4-71", "BOX", "N4"), car("C-S2-71", "HOPPER", "S2")],
    )
    closed = api.expect_ok("POST", "/api/shifts/SHIFT-07/close", {})
    assert closed["shift"]["state"] == "CLOSED"
    certificate = closed.get("certificate")
    assert certificate, f"closure did not return a certificate: {sorted(closed)}"
    assert "certificate_warning" not in closed
    assert certificate["shift_code"] == "SHIFT-07"
    assert certificate["shift"]["closed_at"] == closed["shift"]["closed_at"]
    assert certificate["snapshot_code"] == "SNAP-SHIFT-07"
    assert certificate["blockers"] == []
    assert certificate["metrics"]["car_state_counts"]["standing"] == 2
    event_range = certificate["event_range"]
    assert (event_range["first_sequence"], event_range["last_sequence"], event_range["count"]) == (1, 4, 4)
    assert len(event_range["events"]) == 4
    assert certificate["content_digest"].startswith("sha256:")

    fetched = api.expect_ok("GET", "/api/shifts/SHIFT-07/closure-certificate")
    assert fetched == certificate, "certificate content changed between reads"

    report = verify(api, "SHIFT-07")
    assert report["verified"] is True and report["mismatches"] == []

    certificate_path = data_dir / "certificates" / "SHIFT-07.json"
    certificate_bytes = certificate_path.read_bytes()
    state_backup_text = (data_dir / "yard-state.json").read_text(encoding="utf-8")

    # Scenario 2: editing a snapshot field is reported with the exact field.
    state = read_state(data_dir)
    state["closure_snapshots"][0]["metrics"]["car_state_counts"]["standing"] = 999
    write_state(data_dir, state)
    report = verify(api, "SHIFT-07")
    assert report["verified"] is False
    hits = [item for item in report["mismatches"] if item["field"] == "metrics.car_state_counts.standing"]
    assert hits, f"no field-level mismatch for the edited metric: {report['mismatches']}"
    assert hits[0]["scope"] == "metrics"
    assert hits[0]["expected"] == 2 and hits[0]["actual"] == 999
    assert certificate_path.read_bytes() == certificate_bytes, "verification rewrote the certificate"

    # Scenario 3: deleting one event is reported inside the certified range.
    (data_dir / "yard-state.json").write_text(state_backup_text, encoding="utf-8")
    state = read_state(data_dir)
    removed = [event for event in state["events"] if event["kind"] == "TRAIN_CLASSIFIED"]
    assert len(removed) == 1
    state["events"] = [event for event in state["events"] if event["kind"] != "TRAIN_CLASSIFIED"]
    write_state(data_dir, state)
    report = verify(api, "SHIFT-07")
    assert report["verified"] is False
    fields = {item["field"] for item in report["mismatches"]}
    assert "event_range.count" in fields, f"no count mismatch: {report['mismatches']}"
    count_hit = next(item for item in report["mismatches"] if item["field"] == "event_range.count")
    assert count_hit["expected"] == 4 and count_hit["actual"] == 3
    missing_hit = next(item for item in report["mismatches"] if item["field"] == "event_range.missing_sequences")
    assert removed[0]["sequence"] in missing_hit["expected"]
    assert "event_range.event_digest" in fields
    still_tampered = read_state(data_dir)
    assert len(still_tampered["events"]) == 3, "verification rewrote the state file"
    assert certificate_path.read_bytes() == certificate_bytes

    # Scenario 4: repeated verification is deterministic, and restoring the
    # original state makes the certificate verify cleanly again.
    repeated = verify(api, "SHIFT-07")
    assert repeated["verified"] is False
    assert repeated["mismatches"] == report["mismatches"], "repeated verification reported different errors"
    (data_dir / "yard-state.json").write_text(state_backup_text, encoding="utf-8")
    restored = verify(api, "SHIFT-07")
    assert restored["verified"] is True and restored["mismatches"] == []
    assert verify(api, "SHIFT-07")["verified"] is True
    fetched_again = api.expect_ok("GET", "/api/shifts/SHIFT-07/closure-certificate")
    assert fetched_again == certificate, "certificate content drifted after tamper and restore"
    assert certificate_path.read_bytes() == certificate_bytes

    # A certificate storage failure must not break an already committed closure.
    blocker = data_dir / "certificates" / "SHIFT-08.json"
    blocker.mkdir()  # a directory where the certificate file should be
    open_and_classify(api, "SHIFT-08", "INT-81", [car("C-N4-81", "BOX", "N4")])
    closed_again = api.expect_ok("POST", "/api/shifts/SHIFT-08/close", {})
    assert closed_again["shift"]["state"] == "CLOSED"
    assert closed_again["certificate"] is None
    assert "certificate_warning" in closed_again
    missing = api.expect_error("GET", "/api/shifts/SHIFT-08/closure-certificate")
    assert missing["code"] == "NOT_FOUND"
    blocker.rmdir()
    shift_view = api.expect_ok("GET", "/api/shifts/SHIFT-08")
    kinds = [event["kind"] for event in shift_view["events"]]
    assert "SHIFT_CLOSED" in kinds, "closure was rolled back with the certificate failure"
    assert verify(api, "SHIFT-07")["verified"] is True


def main() -> int:
    server = RunningServer()
    try:
        server.wait_ready()
        run(server.api, Path(server.temp_dir.name) / "data")
        print("OK wf_closure_certificate")
        return 0
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
