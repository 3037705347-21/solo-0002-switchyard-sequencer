"""Command-line interface for yard backup, inspection, migration and restore.

Subcommands:

  backup   export a live data directory into an offline packet
  inspect  validate a packet in isolation and show its contents / migration
  restore  validate a packet and atomically restore it into a target directory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..storage.backup import BackupError, create_backup, inspect_packet
from ..storage.migration import is_legacy_version, preview_migration
from ..storage.restore import RestoreRejected, restore_backup, target_has_state


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard-backup",
        description="Switchyard Sequencer offline backup and migration tool",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="export a data directory into an offline packet")
    backup.add_argument("--data-dir", required=True, help="live data directory to export")
    backup.add_argument("--output", help="destination packet path (default: next to data dir)")

    inspect_cmd = sub.add_parser("inspect", help="validate a packet without restoring it")
    inspect_cmd.add_argument("packet", help="path to the offline packet")
    inspect_cmd.add_argument("--keep-stage", action="store_true", help="keep the isolated staging directory")

    migrate = sub.add_parser("migrate-preview", help="preview migration of a legacy state document")
    migrate.add_argument("state_file", help="path to a legacy yard-state.json")
    migrate.add_argument("--journal", help="optional legacy events journal to preview alongside")

    restore = sub.add_parser("restore", help="validate and restore a packet into a data directory")
    restore.add_argument("packet", help="path to the offline packet")
    restore.add_argument("--data-dir", required=True, help="target data directory")
    restore.add_argument(
        "--replace-existing",
        action="store_true",
        help="allow replacing a target directory that already holds running state",
    )

    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            return _run_backup(args)
        if args.command == "inspect":
            return _run_inspect(args)
        if args.command == "migrate-preview":
            return _run_migrate_preview(args)
        if args.command == "restore":
            return _run_restore(args)
    except (BackupError, RestoreRejected, ValueError, OSError) as exc:
        payload = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, RestoreRejected) and exc.issues:
            payload["issues"] = exc.issues
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
            if isinstance(exc, RestoreRejected):
                for issue in exc.issues:
                    print(f"  - [{issue.get('stage')}/{issue.get('code')}] {issue.get('message')}", file=sys.stderr)
        return 1
    parser.error(f"unknown command {args.command}")
    return 2


def _run_backup(args: argparse.Namespace) -> int:
    result = create_backup(Path(args.data_dir), args.output)
    if args.as_json:
        print(json.dumps({"ok": True, "result": result.to_dict()}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        event_range = result.manifest.event_range
        print(f"backup written: {result.packet_path}")
        print(f"  schema version : {result.manifest.state_schema_version}")
        print(f"  exported at    : {result.manifest.exported_at}")
        print(f"  state bytes    : {result.state_bytes}")
        print(f"  journal bytes  : {result.journal_bytes}")
        print(
            "  event range    : "
            f"{event_range.count} event(s) #{event_range.first_sequence}..#{event_range.last_sequence}"
        )
    return 0


def _run_inspect(args: argparse.Namespace) -> int:
    report = inspect_packet(Path(args.packet), keep_stage=args.keep_stage)
    if args.as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_report(report.to_dict(), args.keep_stage)
    return 0 if report.ok else 2


def _run_migrate_preview(args: argparse.Namespace) -> int:
    from ..storage.backup import parse_journal_bytes

    state_path = Path(args.state_file)
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(raw_state, dict):
        raise ValueError("state root is not a JSON object")
    journal_events = []
    if args.journal:
        journal_events = parse_journal_bytes(Path(args.journal).read_bytes())
    if not is_legacy_version(raw_state.get("schema_version")):
        print(
            f"schema_version={raw_state.get('schema_version')} is not a legacy version; no migration needed",
            file=sys.stderr,
        )
        return 0
    preview = preview_migration(raw_state, journal_events)
    if args.as_json:
        print(json.dumps(preview.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"migration v{preview.from_version} -> v{preview.to_version}")
        print(f"migratable: {preview.migratable}")
        print("counts:")
        for key, value in sorted(preview.counts.items()):
            print(f"  {key:18s} {value}")
        print("changes:")
        for change in preview.changes:
            print(f"  ~ {change}")
        if preview.issues:
            print("blocking issues:")
            for issue in preview.issues:
                print(f"  ! [{issue['code']}] {issue['message']}")
    return 0 if preview.migratable else 2


def _run_restore(args: argparse.Namespace) -> int:
    if target_has_state(Path(args.data_dir)) and not args.replace_existing:
        raise RestoreRejected(
            f"target {args.data_dir} already holds running state; re-run with --replace-existing to replace it"
        )
    report = restore_backup(Path(args.packet), Path(args.data_dir), replace_existing=args.replace_existing)
    if args.as_json:
        print(json.dumps({"ok": True, "result": report.to_dict()}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"restored: {report.packet_path} -> {report.target_dir}")
        print(f"  replaced existing: {report.replaced_existing}")
        print(f"  migrated legacy  : {report.migrated}")
        print("  counts:")
        for key, value in sorted(report.summary.items()):
            print(f"    {key:18s} {value}")
    return 0


def _print_report(payload: dict[str, Any], keep_stage: bool) -> None:
    print(f"packet       : {payload['packet_path']}")
    print(f"valid        : {payload['ok']}")
    if keep_stage:
        print(f"stage dir    : {payload['stage_dir']}")
    manifest = payload.get("manifest")
    if manifest:
        event_range = manifest.get("event_range", {})
        print(f"exported at  : {manifest.get('exported_at')}")
        print(f"packet format: v{manifest.get('packet_format_version')}")
        print(f"state schema : v{manifest.get('state_schema_version')}")
        print(
            "event range  : "
            f"{event_range.get('count')} event(s) "
            f"#{event_range.get('first_sequence')}..#{event_range.get('last_sequence')}"
        )
        for name, entry in sorted(manifest.get("files", {}).items()):
            print(f"  {name:18s} {entry.get('bytes')} bytes sha256={entry.get('sha256', '')[:16]}...")
    migration = payload.get("migration")
    if migration:
        print(
            f"legacy       : v{migration.get('from_version')} -> v{migration.get('to_version')} "
            f"(migratable={migration.get('migratable')})"
        )
    summary = payload.get("summary") or {}
    if summary:
        print("contents     :")
        for key, value in sorted(summary.items()):
            print(f"  {key:18s} {value}")
    issues = payload.get("issues", [])
    if issues:
        print("issues:")
        for issue in issues:
            marker = "!" if issue.get("severity") == "error" else "~"
            print(f"  {marker} [{issue.get('stage')}/{issue.get('code')}] {issue.get('message')}")


if __name__ == "__main__":
    sys.exit(main())
