"""Workflow check: event audit and reconciliation.

Three independent data directories are driven through the API:

1. a full departure flow, which must reconcile cleanly;
2. the same flow with one journal line deliberately deleted, which must be
   reported as a one-sided change plus a sequence gap without blocking service;
3. the same flow with a duplicate event mixed into the journal, which must be
   reported as a duplicate sequence.

Every audit read is repeated and the state/journal files are hashed before and
after, proving the auditor is strictly read-only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from support import RunningServer

STATE_FILE = "yard-state.json"
JOURNAL_FILE = "events.jsonl"


def drive_full_departure_flow(api: ApiClient, shift: str, intake: str, outbound: str) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": shift, "dispatcher": "AUD", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": intake,
            "route": "RAIL-AUD",
            "arrival_at": "2026-09-13T09:00:00Z",
            "cars": [
                {
                    "code": "C-AUD-1",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-AUD-BLK",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": False,
                    "length_m": 18,
                    "danger_class": "NONE",
                },
                {
                    "code": "C-AUD-2",
                    "kind": "REEFER",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 20,
                    "danger_class": "NONE",
                },
            ],
        },
    )
    api.expect_ok("POST", f"/api/intake-trains/{intake}/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": outbound, "destination": "N4", "car_codes": ["C-AUD-2", "C-AUD-1"]},
    )
    sequenced = api.expect_ok("POST", f"/api/outbound-trains/{outbound}/sequencer", {"transfer_code": "X1"})
    run_code = sequenced["pull_run"]["code"]
    total_steps = len(sequenced["pull_run"]["steps"])
    advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": total_steps})
    assert advanced["completed"] is True
    assert advanced["pull_run"]["state"] == "COMPLETED"
    departed = api.expect_ok("POST", f"/api/outbound-trains/{outbound}/depart", {})
    assert departed["outbound"]["state"] == "DEPARTED"
    api.expect_ok("POST", f"/api/shifts/{shift}/close", {})


def issue_codes(document: dict) -> set[str]:
    return {issue["code"] for issue in document["issues"]}


def audit(api: ApiClient, **params: object) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    path = "/api/audit/reconciliation" + (f"?{query}" if query else "")
    status, body = api.get(path)
    assert status == 200 and body.get("ok"), f"audit endpoint failed: {status} {body}"
    return dict(body["data"])


def hash_files(data_dir: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for name in (STATE_FILE, JOURNAL_FILE):
        path = data_dir / name
        digest[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    return digest


def assert_reads_are_idempotent(api: ApiClient, data_dir: Path) -> dict:
    before = hash_files(data_dir)
    first = audit(api)
    second = audit(api)
    # generated_at is the only field allowed to differ between repeated reads.
    first["generated_at"] = ""
    second["generated_at"] = ""
    assert first == second, "repeated audit runs produced different documents"
    after = hash_files(data_dir)
    assert before == after, f"audit run changed files on disk: {before} -> {after}"
    return first


def scenario_clean(server: RunningServer, data_dir: Path) -> None:
    api = server.api
    drive_full_departure_flow(api, "SHIFT-AUD-1", "INT-AUD-1", "OB-AUD-1")
    document = assert_reads_are_idempotent(api, data_dir)
    summary = document["summary"]
    assert summary["status"] == "RECONCILED", document["issues"]
    assert summary["error_count"] == 0, document["issues"]
    assert summary["paired_events"] == summary["journal_records"] == summary["state_events"]
    assert summary["journal_only_events"] == 0
    assert summary["state_only_events"] == 0
    assert summary["duplicate_journal_records"] == 0
    assert summary["journal_gap_sequences"] == 0
    assert summary["trace"]["inconsistent"] == 0
    assert summary["trace"]["object_missing"] == 0

    # Every event traces to a current object (YARD_VIEWED is never emitted here).
    for event in document["events"]:
        assert event["paired"] is True
        assert event["trace"]["verdict"] == "CONSISTENT", event
        assert event["trace"]["current_state"] in {"CLOSED", "CLASSIFIED", "DEPARTED", "COMPLETED"}, event

    # Filtered views: by shift, kind, car, and pull run.
    by_shift = audit(api, shift="SHIFT-AUD-1")
    assert by_shift["filtered_event_count"] == summary["journal_records"]
    other_shift = audit(api, shift="SHIFT-NOPE")
    assert other_shift["filtered_event_count"] == 0
    by_kind = audit(api, kind="TRAIN_DEPARTED")
    assert by_kind["filtered_event_count"] == 1
    assert by_kind["events"][0]["object"]["object_type"] == "OUTBOUND"
    assert by_kind["events"][0]["trace"]["current_state"] == "DEPARTED"
    by_car = audit(api, car="C-AUD-2")
    kinds = {event["kind"] for event in by_car["events"]}
    assert {"TRAIN_RECEIVED", "TRAIN_CLASSIFIED", "PULL_RUN_COMPLETED", "TRAIN_DEPARTED"} <= kinds
    by_run = audit(api, pull_run="RUN-OB-AUD-1")
    run_kinds = {event["kind"] for event in by_run["events"]}
    assert {"PULL_PLANNED", "PULL_RUN_STARTED", "PULL_RUN_COMPLETED"} <= run_kinds
    assert all(event["object"]["object_code"] == "RUN-OB-AUD-1" for event in by_run["events"])
    assert all(event["trace"]["current_state"] == "COMPLETED" for event in by_run["events"])
    # Filtering the view must never hide issues.
    assert by_run["summary"]["status"] == "RECONCILED"
    assert_reads_are_idempotent(api, data_dir)


def remove_journal_line(data_dir: Path, predicate) -> str:
    journal_path = data_dir / JOURNAL_FILE
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    target_index = next(index for index, line in enumerate(lines) if predicate(json.loads(line)))
    removed = lines.pop(target_index)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return removed


def scenario_missing_line(server: RunningServer, data_dir: Path) -> None:
    api = server.api
    drive_full_departure_flow(api, "SHIFT-AUD-2", "INT-AUD-2", "OB-AUD-2")

    # Delete the outbound creation event (sequence 5) from the journal only.
    removed = json.loads(
        remove_journal_line(
            data_dir,
            lambda record: record.get("kind") == "TRAIN_CREATED" and record.get("shift_code") == "SHIFT-AUD-2",
        )
    )
    missing_sequence = removed["sequence"]

    # A damaged journal must not block normal yard service.
    yard_status, yard_body = api.get("/api/yard")
    assert yard_status == 200 and yard_body.get("ok"), yard_body

    document = assert_reads_are_idempotent(api, data_dir)
    summary = document["summary"]
    assert summary["status"] == "DISCREPANCY"
    assert summary["error_count"] > 0
    codes = issue_codes(document)
    assert "journal_sequence_gap" in codes
    assert "state_only_event" in codes
    gap_issue = next(issue for issue in document["issues"] if issue["code"] == "journal_sequence_gap")
    assert missing_sequence in gap_issue["details"]["missing_sequences"]
    state_only = [issue for issue in document["issues"] if issue["code"] == "state_only_event"]
    assert any(issue["sequence"] == missing_sequence for issue in state_only)
    assert summary["state_only_events"] == 1
    assert summary["journal_gap_sequences"] >= 1

    # The state-only event row still traces to the current outbound object.
    state_rows = [event for event in document["events"] if event["source"] == "state"]
    row = next(event for event in state_rows if event["sequence"] == missing_sequence)
    assert row["paired"] is False
    assert row["object"]["object_type"] == "OUTBOUND"
    assert row["object"]["object_code"] == "OB-AUD-2"
    assert row["trace"]["verdict"] == "CONSISTENT"
    assert row["trace"]["current_state"] == "DEPARTED"
    assert_reads_are_idempotent(api, data_dir)


def scenario_duplicate_line(server: RunningServer, data_dir: Path) -> None:
    api = server.api
    drive_full_departure_flow(api, "SHIFT-AUD-3", "INT-AUD-3", "OB-AUD-3")

    # Duplicate the TRAIN_RECEIVED line right next to the original: same
    # sequence, same content, so the only defect is the repeated record.
    journal_path = data_dir / JOURNAL_FILE
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    target_index = next(
        index
        for index, line in enumerate(lines)
        if json.loads(line).get("kind") == "TRAIN_RECEIVED"
        and json.loads(line).get("shift_code") == "SHIFT-AUD-3"
    )
    duplicate = lines[target_index]
    duplicate_sequence = json.loads(duplicate)["sequence"]
    lines.insert(target_index + 1, duplicate)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    document = assert_reads_are_idempotent(api, data_dir)
    summary = document["summary"]
    assert summary["status"] == "DISCREPANCY"
    codes = issue_codes(document)
    assert "duplicate_journal_sequence" in codes
    duplicate_issues = [issue for issue in document["issues"] if issue["code"] == "duplicate_journal_sequence"]
    issue = next(item for item in duplicate_issues if item["sequence"] == duplicate_sequence)
    assert issue["details"]["occurrence_count"] == 2
    assert issue["details"]["identical"] is True
    assert summary["duplicate_journal_records"] == 1
    # The duplicated line is not a missing-line or ordering defect.
    assert "journal_sequence_gap" not in codes
    assert "journal_out_of_order" not in codes

    # The events view shows two physical journal rows sharing one sequence.
    rows = [event for event in document["events"] if event["sequence"] == duplicate_sequence]
    assert len(rows) == 2
    assert all(row["source"] == "journal" for row in rows)
    assert rows[0]["ordinal"] != rows[1]["ordinal"]
    assert_reads_are_idempotent(api, data_dir)


def run_scenario(name: str, scenario) -> None:
    server = RunningServer()
    data_dir = Path(server.temp_dir.name) / "data"
    try:
        server.wait_ready()
        scenario(server, data_dir)
    finally:
        server.stop()
    print(f"OK {name}")


if __name__ == "__main__":
    run_scenario("wf_audit_reconciliation_clean", scenario_clean)
    run_scenario("wf_audit_reconciliation_missing_line", scenario_missing_line)
    run_scenario("wf_audit_reconciliation_duplicate_line", scenario_duplicate_line)
    raise SystemExit(0)
