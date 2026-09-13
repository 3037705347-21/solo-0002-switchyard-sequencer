"""Workflow check: yard backup, integrity validation, migration and restore.

Scenarios:

1. normal packet   - full shift lifecycle is exported, restored into a fresh
                     directory, and shifts/cars/events/closure snapshot are
                     read again through the repository and a live HTTP server.
2. truncated packet- an archive cut short and a packet whose state member was
                     truncated must both be rejected; the running target
                     directory is served untouched.
3. legacy packet   - a hand-built schema v0 packet yields a previewable
                     migration (with rename notes) and restores as v1; a
                     legacy packet with an unmappable event is refused with a
                     reason.
4. existing target - restoring over a running directory is refused by
                     default, bad packets never replace it, and an explicit
                     replace restores atomically after the service stops.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from support import RunningServer
from switchyard.entry.backup_cli import main as backup_cli_main
from switchyard.storage.backup import (
    JOURNAL_FILE,
    MANIFEST_FILE,
    STATE_FILE,
    BackupManifest,
    EventRange,
    create_backup,
    inspect_packet,
    sha256_hex,
)
from switchyard.storage.repository import YardRepository
from switchyard.storage.restore import RestoreRejected, restore_backup, target_has_state


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def drive_full_shift(data_dir: Path) -> None:
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        api = server.api
        api.expect_ok(
            "POST",
            "/api/shifts",
            {"code": "SHIFT-BR", "dispatcher": "KE", "opened_at": "2026-09-13T08:00:00Z"},
        )
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {
                "code": "INT-BR",
                "route": "RAIL-BR",
                "arrival_at": "2026-09-13T09:00:00Z",
                "cars": [
                    {
                        "code": "C-N4-B1",
                        "kind": "BOX",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-N4-B2",
                        "kind": "REEFER",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 20,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-E7-B1",
                        "kind": "HOPPER",
                        "destination": "E7",
                        "loaded": False,
                        "length_m": 19,
                        "danger_class": "NONE",
                    },
                ],
            },
        )
        api.expect_ok("POST", "/api/intake-trains/INT-BR/classify", {})
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-BR", "destination": "N4", "car_codes": ["C-N4-B2", "C-N4-B1"]},
        )
        sequenced = api.expect_ok("POST", "/api/outbound-trains/OB-BR/sequencer", {"transfer_code": "X1"})
        run_code = sequenced["pull_run"]["code"]
        steps = len(sequenced["pull_run"]["steps"])
        advanced = api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": steps})
        assert advanced["completed"] is True
        departed = api.expect_ok("POST", "/api/outbound-trains/OB-BR/depart", {})
        assert departed["outbound"]["state"] == "DEPARTED"
        closed = api.expect_ok("POST", "/api/shifts/SHIFT-BR/close", {})
        assert closed["shift"]["state"] == "CLOSED"
        assert closed["snapshot"]["code"] == "SNAP-SHIFT-BR"
    finally:
        server.stop()


def assert_restored_workspace(target: Path) -> Any:
    repository = YardRepository(target)
    workspace = repository.load()
    assert "SHIFT-BR" in workspace.shifts, "shift missing after restore"
    assert workspace.shifts["SHIFT-BR"].state.value == "CLOSED"
    assert workspace.shifts["SHIFT-BR"].closure_snapshot_code == "SNAP-SHIFT-BR"
    assert {"C-N4-B1", "C-N4-B2", "C-E7-B1"} <= set(workspace.cars), "cars missing after restore"
    assert len(workspace.events) == 9, f"expected 9 events, got {len(workspace.events)}"
    kinds = [str(event.kind) for event in workspace.events]
    assert kinds[0] == "SHIFT_OPENED"
    assert kinds[-1] == "SHIFT_CLOSED"
    assert len(workspace.closure_snapshots) == 1, "closure snapshot missing after restore"
    snapshot = workspace.closure_snapshots[0]
    assert snapshot["code"] == "SNAP-SHIFT-BR"
    assert snapshot["shift_code"] == "SHIFT-BR"
    assert snapshot["metrics"]["car_state_counts"]["departed"] == 2
    assert snapshot["metrics"]["car_state_counts"]["standing"] == 1
    journal = repository.journal.read_all()
    assert len(journal) == 9, f"journal unreadable after restore: {len(journal)} events"
    assert [item["sequence"] for item in journal] == list(range(1, 10))
    return workspace


def serve_readback(target: Path) -> None:
    """Start a service on the restored directory and read through the HTTP API."""

    server = RunningServer(data_dir=target)
    try:
        server.wait_ready()
        api = server.api
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["active_shift"] == "NONE"
        counts = yard["metrics"]["car_state_counts"]
        assert counts["standing"] == 1 and counts["departed"] == 2
        assert yard["metrics"]["event_count"] == 9
        shift = api.expect_ok("GET", "/api/shifts/SHIFT-BR")
        assert shift["shift"]["state"] == "CLOSED"
        assert shift["shift"]["closure_snapshot_code"] == "SNAP-SHIFT-BR"
        kinds = [event["kind"] for event in shift["events"]]
        assert kinds == [
            "SHIFT_OPENED",
            "TRAIN_RECEIVED",
            "TRAIN_CLASSIFIED",
            "TRAIN_CREATED",
            "PULL_PLANNED",
            "PULL_RUN_STARTED",
            "PULL_RUN_COMPLETED",
            "TRAIN_DEPARTED",
            "SHIFT_CLOSED",
        ]
    finally:
        server.stop()


def digest(path: Path) -> str:
    return sha256_hex(path.read_bytes())


def cli_exit(argv: list[str]) -> int:
    """Run the backup CLI without polluting the check output."""

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return backup_cli_main(argv)


# --------------------------------------------------------------------------- #
# Scenario 1: normal packet
# --------------------------------------------------------------------------- #


def scenario_normal_packet(work: Path) -> Path:
    print("[1] normal packet: export, validate, restore, read back ...")
    live = work / "live"
    drive_full_shift(live)

    packet = work / "normal.sypack"
    result = create_backup(live, packet)
    assert packet.is_file()
    assert result.manifest.event_range.count == 9
    assert result.manifest.event_range.first_sequence == 1
    assert result.manifest.event_range.last_sequence == 9
    assert result.manifest.state_schema_version == 1

    report = inspect_packet(packet)
    assert report.ok, [issue.message for issue in report.errors()]
    assert report.summary["shifts"] == 1
    assert report.summary["closure_snapshots"] == 1

    target = work / "restored"
    restore = restore_backup(packet, target)
    assert restore.restored and not restore.migrated
    assert target_has_state(target)
    assert_restored_workspace(target)
    serve_readback(target)

    # CLI surface must report the same verdict.
    code = cli_exit(["inspect", str(packet)])
    assert code == 0
    print("    OK export -> isolated validation -> restore -> repository/API read-back")
    return packet


# --------------------------------------------------------------------------- #
# Scenario 2: truncated packets
# --------------------------------------------------------------------------- #


def truncate_archive(packet: Path, output: Path) -> None:
    data = packet.read_bytes()
    output.write_bytes(data[: len(data) // 2])


def truncate_state_member(packet: Path, output: Path) -> None:
    with zipfile.ZipFile(packet) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    state = members[STATE_FILE]
    members[STATE_FILE] = state[: len(state) // 3]  # manifest checksum no longer matches
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in (MANIFEST_FILE, STATE_FILE, JOURNAL_FILE):
            archive.writestr(name, members[name])


def scenario_truncated_packet(work: Path, normal_packet: Path) -> None:
    print("[2] truncated packet: bad archive and tampered member rejected ...")
    target = work / "running-target"
    shutil.copytree(work / "live", target)
    state_before = digest(target / STATE_FILE)
    journal_before = digest(target / JOURNAL_FILE)

    # The target directory is actively serving while bad packets arrive.
    server = RunningServer(data_dir=target)
    try:
        server.wait_ready()
        yard_before = server.api.expect_ok("GET", "/api/yard")

        cut_archive = work / "cut-archive.sypack"
        truncate_archive(normal_packet, cut_archive)
        cut_report = inspect_packet(cut_archive)
        assert not cut_report.ok
        stages = {(issue.stage, issue.code) for issue in cut_report.errors()}
        assert any(stage == "integrity" for stage, code in stages), stages

        cut_member = work / "cut-member.sypack"
        truncate_state_member(normal_packet, cut_member)
        member_report = inspect_packet(cut_member)
        assert not member_report.ok
        codes = {issue.code for issue in member_report.errors()}
        assert "CHECKSUM_MISMATCH" in codes or "JSON_UNREADABLE" in codes, codes

        for bad_packet in (cut_archive, cut_member):
            try:
                restore_backup(bad_packet, target, replace_existing=True)
            except RestoreRejected as exc:
                assert exc.issues, "rejection must explain the validation failures"
            else:
                raise AssertionError("truncated packet should not restore over running state")

            assert digest(target / STATE_FILE) == state_before, "running state was overwritten by a bad packet"
            assert digest(target / JOURNAL_FILE) == journal_before, "running journal was overwritten by a bad packet"

        yard_after = server.api.expect_ok("GET", "/api/yard")
        assert yard_after == yard_before, "live service view changed after failed restores"
    finally:
        server.stop()

    assert cli_exit(["inspect", str(cut_archive)]) == 2
    assert cli_exit(["inspect", str(cut_member)]) == 2
    print("    OK both truncation shapes rejected; running data directory untouched")


# --------------------------------------------------------------------------- #
# Scenario 3: legacy v0 packets
# --------------------------------------------------------------------------- #


def legacy_state_document() -> dict[str, Any]:
    return {
        "schema_version": 0,
        "version": 8,
        "next_event_sequence": 9,
        "tracks": [
            {
                "code": "N4-A",
                "purpose": "DESTINATION",
                "capacity_cars": 10,
                "capacity_length_m": 300,
                "state": "OPERATIONAL",
                "destination_affinity": "N4",
                "allowed_kinds": [],
                "haz_rated": False,
                "stack": ["C-N4-91"],
            },
            {
                "code": "HAZ-1",
                "purpose": "GENERAL",
                "capacity_cars": 8,
                "capacity_length_m": 240,
                "state": "OPERATIONAL",
                "destination_affinity": None,
                "allowed_kinds": [],
                "haz_rated": True,
                "stack": [],
            },        ],
        "buffer_bays": [{"code": "X1", "capacity_cars": 10, "stack": []}],
        "cars": [
            {
                "code": "C-N4-91",
                "kind": "BOXCAR",
                "destination": "N4",
                "loaded": True,
                "length_m": 18,
                "hazmat": False,
                "state": "PARKED",
                "location": "N4-A",
            },
            {
                "code": "C-HAZ-91",
                "kind": "TANK",
                "destination": "W9",
                "loaded": True,
                "length_m": 22,
                "hazmat": True,
                "state": "GONE",
                "location": "OB-90",
            },
        ],        "inbound_trains": [
            {
                "code": "INT-90",
                "route": "RAIL-90",
                "arrival_at": "2026-09-12T09:00:00Z",
                "cars": ["C-N4-91", "C-HAZ-91"],
                "state": "DONE",
                "unplaced": [],
                "placed_at": "2026-09-12T09:10:00Z",
            }
        ],
        "departure_trains": [
            {
                "code": "OB-90",
                "destination": "W9",
                "sequence": ["C-HAZ-91"],
                "built_sequence": ["C-HAZ-91"],
                "state": "DEPARTED",
                "run_codes": ["RUN-OB-90"],
                "created_at": "2026-09-12T10:00:00Z",
                "departed_at": "2026-09-12T11:00:00Z",
            }
        ],
        "pull_runs": [
            {
                "code": "RUN-OB-90",
                "outbound_code": "OB-90",
                "transfer_code": "X1",
                "steps": [
                    {
                        "verb": "PULL",
                        "car_code": "C-HAZ-91",
                        "source_code": "HAZ-1",
                        "target_code": "OB-90",
                    }
                ],
                "state": "COMPLETED",
                "current_step": 1,
                "created_at": "2026-09-12T10:05:00Z",
                "started_at": "2026-09-12T10:06:00Z",
                "completed_at": "2026-09-12T10:30:00Z",
                "error": None,
            }
        ],        "shifts": [
            {
                "code": "SHIFT-90",
                "dispatcher": "LEG",
                "opened_at": "2026-09-12T08:00:00Z",
                "state": "CLOSED",
                "closed_at": "2026-09-12T12:00:00Z",
                "closure_snapshot_code": "SNAP-SHIFT-90",
                "note": "",
            }
        ],
        "events": [_legacy_event(seq, kind) for seq, kind in enumerate(_legacy_event_types(), start=1)],
        "snapshots": [
            {
                "snapshot_code": "SNAP-SHIFT-90",
                "closed_shift": "SHIFT-90",
                "metrics": {"version": 7},
                "blockers": [],
                "version": 7,
            }
        ],
    }


def _legacy_event_types() -> list[str]:
    return [
        "SHIFT_OPEN",
        "TRAIN_IN",
        "TRAIN_DONE",
        "PLAN_CREATED",
        "PULL_PLAN",
        "RUN_START",
        "TRAIN_OUT",
        "SHIFT_LOCK",
    ]


def _legacy_event(seq: int, kind: str) -> dict[str, Any]:
    return {
        "seq": seq,
        "ts": f"2026-09-12T{7 + seq:02d}:00:00Z",
        "type": kind,
        "shift_code": "SHIFT-90",
        "message": f"legacy event {seq}",
        "payload": {},
    }


def build_packet_from_members(
    output: Path,
    raw_state: dict[str, Any],
    journal_events: list[dict[str, Any]],
    *,
    schema_version: int,
) -> None:
    state_bytes = (json.dumps(raw_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    journal_bytes = b"".join(
        (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for event in journal_events
    )
    state_events = raw_state.get("events", [])
    manifest = BackupManifest(
        packet_format_version=1,
        exported_at="2026-09-13T00:00:00Z",
        source_dir="legacy-export",
        state_schema_version=schema_version,
        event_range=EventRange.from_events(state_events),
        files={
            STATE_FILE: {"sha256": sha256_hex(state_bytes), "bytes": len(state_bytes)},
            JOURNAL_FILE: {"sha256": sha256_hex(journal_bytes), "bytes": len(journal_bytes)},
        },
    )
    manifest_bytes = (json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_FILE, manifest_bytes)
        archive.writestr(STATE_FILE, state_bytes)
        archive.writestr(JOURNAL_FILE, journal_bytes)


def scenario_legacy_packet(work: Path) -> None:
    print("[3] legacy v0 packet: preview, migrate, restore with reasons on failure ...")
    raw_state = legacy_state_document()
    journal_events = [_legacy_event(seq, kind) for seq, kind in enumerate(_legacy_event_types(), start=1)]
    legacy_packet = work / "legacy-v0.sypack"
    build_packet_from_members(legacy_packet, raw_state, journal_events, schema_version=0)

    report = inspect_packet(legacy_packet)
    assert report.ok, [issue.message for issue in report.errors()]
    assert report.migration is not None
    migration = report.migration
    assert migration["from_version"] == 0 and migration["to_version"] == 1
    assert migration["migratable"] is True
    change_text = "\n".join(migration["changes"])
    assert "inbound_trains" in change_text and "intakes" in change_text
    assert "BOXCAR" in change_text and "'BOX'" in change_text
    assert "PARKED" in change_text and "'STANDING'" in change_text
    assert "hazmat" in change_text
    assert "destination_affinity" in change_text
    assert "SHIFT_OPEN" in change_text and "SHIFT_OPENED" in change_text
    assert migration["counts"]["shifts"] == 1 and migration["counts"]["cars"] == 2

    # The CLI migration preview shows the same human-reviewable result.
    stage = work / "legacy-unpack"
    with zipfile.ZipFile(legacy_packet) as archive:
        archive.extractall(stage)
    assert cli_exit(["migrate-preview", str(stage / STATE_FILE), "--journal", str(stage / JOURNAL_FILE)]) == 0

    target = work / "legacy-restored"
    restore = restore_backup(legacy_packet, target)
    assert restore.restored and restore.migrated

    repository = YardRepository(target)
    workspace = repository.load()
    assert workspace.schema_version == 1
    shift = workspace.shifts["SHIFT-90"]
    assert shift.state.value == "CLOSED"
    assert shift.closure_snapshot_code == "SNAP-SHIFT-90"
    standing = workspace.cars["C-N4-91"]
    assert standing.kind.value == "BOX" and standing.state.value == "STANDING"
    assert standing.danger_class == "NONE"
    departed = workspace.cars["C-HAZ-91"]
    assert departed.state.value == "DEPARTED" and departed.danger_class == "UNSPECIFIED"
    assert "INT-90" in workspace.intakes and workspace.intakes["INT-90"].state.value == "CLASSIFIED"
    assert workspace.intakes["INT-90"].consist == ["C-N4-91", "C-HAZ-91"]
    outbound = workspace.outbounds["OB-90"]
    assert outbound.planned_car_codes == ["C-HAZ-91"] and outbound.assembled_car_codes == ["C-HAZ-91"]
    assert [str(event.kind) for event in workspace.events] == [
        "SHIFT_OPENED",
        "TRAIN_RECEIVED",
        "TRAIN_CLASSIFIED",
        "TRAIN_CREATED",
        "PULL_PLANNED",
        "PULL_RUN_STARTED",
        "TRAIN_DEPARTED",
        "SHIFT_CLOSED",
    ]
    snapshot = workspace.closure_snapshots[0]
    assert snapshot["code"] == "SNAP-SHIFT-90" and snapshot["shift_code"] == "SHIFT-90"
    migrated_journal = repository.journal.read_all()
    assert len(migrated_journal) == 8
    assert migrated_journal[0]["kind"] == "SHIFT_OPENED" and migrated_journal[-1]["kind"] == "SHIFT_CLOSED"

    # An unmappable legacy event must be reported, not guessed.
    bad_state = legacy_state_document()
    bad_state["events"][3] = {
        "seq": 4,
        "ts": "2026-09-12T11:00:00Z",
        "type": "SIGNAL_LOST",
        "shift_code": "SHIFT-90",
        "message": "unknown legacy verb",
        "payload": {},
    }
    bad_journal = list(journal_events)
    bad_journal[3] = bad_state["events"][3]
    bad_packet = work / "legacy-v0-bad.sypack"
    build_packet_from_members(bad_packet, bad_state, bad_journal, schema_version=0)
    bad_report = inspect_packet(bad_packet)
    assert not bad_report.ok
    assert bad_report.migration is not None and bad_report.migration["migratable"] is False
    reasons = [issue.message for issue in bad_report.errors() if issue.stage == "migration"]
    assert reasons and "SIGNAL_LOST" in reasons[0]

    bad_target = work / "legacy-bad-target"
    try:
        restore_backup(bad_packet, bad_target)
    except RestoreRejected as exc:
        assert exc.issues and any("SIGNAL_LOST" in item["message"] for item in exc.issues)
    else:
        raise AssertionError("unmigratable legacy packet should be refused")
    assert not bad_target.exists() or not (bad_target / STATE_FILE).exists(), "failed restore left target state"
    print("    OK migratable legacy packet upgraded; unmappable data refused with a reason")


# --------------------------------------------------------------------------- #
# Scenario 4: target directory already running
# --------------------------------------------------------------------------- #


def scenario_existing_target(work: Path, normal_packet: Path) -> None:
    print("[4] existing running target: guarded refusal, intact data, explicit replace ...")
    target = work / "occupied"
    shutil.copytree(work / "live", target)
    state_before = digest(target / STATE_FILE)

    server = RunningServer(data_dir=target)
    try:
        server.wait_ready()
        # Default restore must refuse while the directory holds running state.
        try:
            restore_backup(normal_packet, target)
        except RestoreRejected as exc:
            assert "running state" in str(exc)
        else:
            raise AssertionError("restore over running state should be refused by default")
        assert digest(target / STATE_FILE) == state_before

        yard = server.api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["event_count"] == 9

        # A packet that fails validation must never reach the live directory.
        cut_member = work / "occupied-cut.sypack"
        truncate_state_member(normal_packet, cut_member)
        try:
            restore_backup(cut_member, target, replace_existing=True)
        except RestoreRejected:
            pass
        else:
            raise AssertionError("bad packet must not overwrite the running target even with replace")
        assert digest(target / STATE_FILE) == state_before
        assert server.api.expect_ok("GET", "/api/yard") == yard
    finally:
        server.stop()

    # Once the operator explicitly replaces (service stopped), it commits.
    replaced = restore_backup(normal_packet, target, replace_existing=True)
    assert replaced.restored and replaced.replaced_existing
    assert_restored_workspace(target)
    serve_readback(target)
    print("    OK target protected by default; explicit replace restores atomically")


# --------------------------------------------------------------------------- #


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="switchyard-backup-check-") as temp_dir:
        work = Path(temp_dir)
        normal_packet = scenario_normal_packet(work)
        scenario_truncated_packet(work, normal_packet)
        scenario_legacy_packet(work)
        scenario_existing_target(work, normal_packet)
    print("OK wf_backup_restore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
