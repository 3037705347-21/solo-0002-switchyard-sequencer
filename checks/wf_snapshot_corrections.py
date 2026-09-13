"""Workflow check: controlled corrections on an archived closure snapshot.

Four operation groups verify the revision contract:

A. correct a remark ........................ original numbers stay intact and
                                              list/detail/export split original
                                              data from revised content;
B. attempt to change a metric ............... rejected, snapshot untouched;
C. two consecutive corrections then repeats  revision chain grows, identical
                                              resubmissions replay stably, stale
                                              or key-mismatched retries conflict;
D. read the old snapshot after a restart .... archived values and the correction
                                              chain survive persistence and the
                                              journal records every revision.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from support import ApiClient, RunningServer

SHIFT = "SHIFT-05"
SNAPSHOT = f"SNAP-{SHIFT}"
ORIGINAL_REMARK = "归档时的原始备注"

FIRST_BODY = {
    "changes": {"remark": "更正后的责任备注"},
    "reason": "关班后发现备注写错，按核验流程更正",
    "revised_by": "SL-LEAD",
}
SECOND_BODY = {
    "changes": {"responsible": "SL-LEAD-2"},
    "reason": "责任信息写错，更正为实际责任人",
    "revised_by": "AUDITOR",
}
THIRD_BODY = {
    "changes": {"responsible": "SL-LEAD-3", "remark": "最终责任备注"},
    "reason": "责任人再次核对，补充最终备注",
    "revised_by": "AUDITOR-2",
    "idempotency_key": "fixed-key-003",
}


def _close_a_shift(api: ApiClient) -> dict:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": SHIFT, "dispatcher": "KE", "opened_at": "2026-09-13T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-51",
            "route": "RAIL-51",
            "arrival_at": "2026-09-13T09:30:00Z",
            "cars": [
                {
                    "code": "C-N4-51",
                    "kind": "BOX",
                    "destination": "N4",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-51/classify", {})
    closed = api.expect_ok(
        "POST",
        f"/api/shifts/{SHIFT}/close",
        {"remark": ORIGINAL_REMARK},
    )
    assert closed["snapshot"]["code"] == SNAPSHOT
    return closed["snapshot"]


def group_a_correct_remark(api: ApiClient, archived: dict) -> None:
    detail = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}")
    assert detail["record_type"] == "ORIGINAL"
    assert detail["revised"] is False
    assert detail["original"]["remark"] == ORIGINAL_REMARK

    corrected = api.expect_ok(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        FIRST_BODY,
    )
    correction = corrected["correction"]
    assert corrected["replayed"] is False
    assert correction["revision"] == 1
    assert correction["snapshot_code"] == SNAPSHOT
    assert correction["previous_values"] == {"remark": ORIGINAL_REMARK}
    assert correction["changes"] == {"remark": "更正后的责任备注"}

    snapshot = corrected["snapshot"]
    # Original snapshot values are immutable, including the archived remark.
    assert snapshot["metrics"] == archived["metrics"]
    assert snapshot["original"]["remark"] == ORIGINAL_REMARK
    assert snapshot["effective"]["remark"] == "更正后的责任备注"
    assert snapshot["correction_count"] == 1
    assert snapshot["latest_revision"] == 1

    listing = api.expect_ok("GET", "/api/closure-snapshots")
    typed = [(entry["record_type"], entry["code"]) for entry in listing["entries"]]
    assert ("ORIGINAL", SNAPSHOT) in typed
    assert ("CORRECTION", correction["code"]) in typed
    original_entry = next(entry for entry in listing["entries"] if entry["code"] == SNAPSHOT)
    assert original_entry["metrics"] == archived["metrics"]
    assert original_entry["remark"] == ORIGINAL_REMARK
    assert original_entry["effective"]["remark"] == "更正后的责任备注"

    export = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}/export")
    assert export["archived"]["metrics"] == archived["metrics"]
    assert export["archived"]["remark"] == ORIGINAL_REMARK
    assert export["revised"]["remark"] == "更正后的责任备注"
    assert len(export["corrections"]) == 1


def group_b_reject_metric_changes(api: ApiClient, archived: dict) -> None:
    rejected = api.expect_error(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        {
            "changes": {"metrics": {"standing": 999}, "remark": "想夹带改指标"},
            "reason": "should fail",
            "revised_by": "SL-LEAD",
        },
    )
    assert rejected["code"] == "VALIDATION_ERROR"
    assert "changes.metrics" in rejected["fields"]

    blocked_fields = api.expect_error(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        {
            "changes": {"blockers": ["x"], "version": 2},
            "reason": "should fail",
            "revised_by": "SL-LEAD",
        },
    )
    assert blocked_fields["code"] == "VALIDATION_ERROR"
    assert "changes.blockers" in blocked_fields["fields"]
    assert "changes.version" in blocked_fields["fields"]

    empty = api.expect_error(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        {"changes": {}, "reason": "nothing here", "revised_by": "SL-LEAD"},
    )
    assert empty["code"] == "VALIDATION_ERROR"

    detail = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}")
    assert detail["correction_count"] == 1
    assert detail["metrics"] == archived["metrics"]
    assert detail["original"]["remark"] == ORIGINAL_REMARK
    assert detail["effective"]["remark"] == "更正后的责任备注"


def _event_kinds(api: ApiClient) -> list[str]:
    shift = api.expect_ok("GET", f"/api/shifts/{SHIFT}")
    return [event["kind"] for event in shift["events"]]


def group_c_two_corrections_and_repeats(api: ApiClient) -> str:
    second = api.expect_ok(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        SECOND_BODY,
    )
    assert second["replayed"] is False
    assert second["correction"]["revision"] == 2
    assert second["correction"]["previous_values"] == {"responsible": ""}
    second_code = second["correction"]["code"]

    detail = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}")
    assert detail["latest_revision"] == 2
    assert detail["effective"]["remark"] == "更正后的责任备注"
    assert detail["effective"]["responsible"] == "SL-LEAD-2"
    assert _event_kinds(api).count("SNAPSHOT_CORRECTED") == 2

    # Exact repeat of the latest submission replays it without a new revision.
    replay = api.expect_ok(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        SECOND_BODY,
    )
    assert replay["replayed"] is True
    assert replay["correction"]["code"] == second_code
    detail = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}")
    assert detail["correction_count"] == 2
    assert _event_kinds(api).count("SNAPSHOT_CORRECTED") == 2

    # A third explicit-keyed correction extends the chain and links back to
    # the previously effective values.
    third = api.expect_ok(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        THIRD_BODY,
    )
    assert third["replayed"] is False
    assert third["correction"]["revision"] == 3
    assert third["correction"]["previous_values"] == {
        "remark": "更正后的责任备注",
        "responsible": "SL-LEAD-2",
    }
    third_code = third["correction"]["code"]
    third_replay = api.expect_ok(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        THIRD_BODY,
    )
    assert third_replay["replayed"] is True
    assert third_replay["correction"]["code"] == third_code
    assert _event_kinds(api).count("SNAPSHOT_CORRECTED") == 3

    # Same key with different content is a conflict, never a silent overwrite.
    clash = api.expect_error(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        {
            "changes": {"responsible": "SOMEBODY-ELSE"},
            "reason": "different reason",
            "revised_by": "AUDITOR",
            "idempotency_key": "fixed-key-003",
        },
    )
    assert clash["code"] == "CONFLICT"

    # Replaying an older revision and submitting a no-op both fail loudly:
    # prior revisions stay in place and cannot be quietly re-opened.
    stale = api.expect_error(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        SECOND_BODY,
    )
    assert stale["code"] == "CONFLICT"
    noop = api.expect_error(
        "POST",
        f"/api/closure-snapshots/{SNAPSHOT}/corrections",
        {"changes": {"responsible": "SL-LEAD-3"}, "reason": "again", "revised_by": "AUDITOR"},
    )
    assert noop["code"] == "CONFLICT"

    detail = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}")
    assert detail["correction_count"] == 3
    assert detail["effective"] == {"remark": "最终责任备注", "responsible": "SL-LEAD-3"}
    return third_code


def group_d_read_old_snapshot(data_dir: Path, archived: dict, latest_code: str) -> None:
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        api = server.api
        detail = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}")
        assert detail["metrics"] == archived["metrics"]
        assert detail["original"]["remark"] == ORIGINAL_REMARK
        assert detail["effective"]["remark"] == "最终责任备注"
        assert detail["effective"]["responsible"] == "SL-LEAD-3"
        codes = [item["code"] for item in detail["corrections"]]
        assert latest_code in codes
        assert [item["revision"] for item in detail["corrections"]] == [1, 2, 3]

        export = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}/export")
        assert export["archived"]["metrics"] == archived["metrics"]
        assert export["archived"]["remark"] == ORIGINAL_REMARK
        assert export["revised"] == {"remark": "最终责任备注", "responsible": "SL-LEAD-3"}

        listing = api.expect_ok("GET", "/api/closure-snapshots")
        assert listing["snapshot_count"] == 1
        assert listing["correction_count"] == 3

        # Idempotency holds across restarts: no fourth revision is created.
        replay = api.expect_ok(
            "POST",
            f"/api/closure-snapshots/{SNAPSHOT}/corrections",
            THIRD_BODY,
        )
        assert replay["replayed"] is True
        assert replay["correction"]["code"] == latest_code
        detail = api.expect_ok("GET", f"/api/closure-snapshots/{SNAPSHOT}")
        assert detail["correction_count"] == 3
    finally:
        server.stop()


def run_with_persistence() -> None:
    root = Path(tempfile.mkdtemp(prefix="switchyard-correction-check-"))
    data_dir = root / "data"
    server = RunningServer(data_dir=data_dir)
    archived: dict = {}
    latest_code = ""
    try:
        server.wait_ready()
        archived = _close_a_shift(server.api)
        group_a_correct_remark(server.api, archived)
        group_b_reject_metric_changes(server.api, archived)
        latest_code = group_c_two_corrections_and_repeats(server.api)
    finally:
        server.stop()
    group_d_read_old_snapshot(data_dir, archived, latest_code)


if __name__ == "__main__":
    run_with_persistence()
    print("OK wf_snapshot_corrections")
    raise SystemExit(0)
