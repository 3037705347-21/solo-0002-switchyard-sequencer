"""Offline backup packets for the yard workspace.

A packet is a zip archive holding a manifest, the JSON state file, and the
append-only event journal.  Inspection never touches the live data directory:
the archive is extracted into an isolated staging location and checked in
four layers:

1. packet integrity  - manifest presence, zip CRCs, sizes, SHA-256 digests
2. field readability - JSON parses and every entity decodes through the codec
3. legacy migration  - old schema versions produce a preview, or a reason
4. basic consistency - event sequences, state-vs-journal parity, references

Restore consumes only a fully inspected, consistent packet.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.enums import EventKind
from .codec import decode_workspace, encode_workspace
from .migration import (
    is_legacy_version,
    migrate_journal_events,
    migrate_state,
    preview_migration,
)
from .repository import JOURNAL_FILE, STATE_FILE
from .workspace import SCHEMA_VERSION

PACKET_FORMAT_VERSION = 1
MANIFEST_FILE = "manifest.json"
PACKET_SUFFIX = ".sypack"

# Members a packet must contain, in validation order.
PACKET_MEMBERS = (MANIFEST_FILE, STATE_FILE, JOURNAL_FILE)
_ALLOWED_MEMBERS = frozenset(PACKET_MEMBERS)


@dataclass(slots=True)
class Issue:
    severity: str  # "error" or "warning"
    stage: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "stage": self.stage, "code": self.code, "message": self.message}

    @classmethod
    def error(cls, stage: str, code: str, message: str) -> "Issue":
        return cls("error", stage, code, message)

    @classmethod
    def warning(cls, stage: str, code: str, message: str) -> "Issue":
        return cls("warning", stage, code, message)


@dataclass(slots=True)
class EventRange:
    count: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
        }

    @classmethod
    def from_events(cls, events: list[dict[str, Any]]) -> "EventRange":
        sequences = [int(event.get("sequence", event.get("seq"))) for event in events]
        if not sequences:
            return cls()
        return cls(count=len(sequences), first_sequence=min(sequences), last_sequence=max(sequences))


@dataclass(slots=True)
class BackupManifest:
    packet_format_version: int
    exported_at: str
    source_dir: str
    state_schema_version: int
    event_range: EventRange
    files: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_format_version": self.packet_format_version,
            "exported_at": self.exported_at,
            "source_dir": self.source_dir,
            "state_schema_version": self.state_schema_version,
            "event_range": self.event_range.to_dict(),
            "files": self.files,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BackupManifest":
        files_raw = raw.get("files", {})
        files: dict[str, dict[str, Any]] = {}
        if isinstance(files_raw, dict):
            for name, item in files_raw.items():
                if isinstance(item, dict):
                    files[str(name)] = dict(item)
        range_raw = raw.get("event_range", {})
        if not isinstance(range_raw, dict):
            range_raw = {}
        return cls(
            packet_format_version=int(raw.get("packet_format_version", 0)),
            exported_at=str(raw.get("exported_at", "")),
            source_dir=str(raw.get("source_dir", "")),
            state_schema_version=int(raw.get("state_schema_version", 0)),
            event_range=EventRange(
                count=int(range_raw.get("count", 0)),
                first_sequence=range_raw.get("first_sequence"),
                last_sequence=range_raw.get("last_sequence"),
            ),
            files=files,
        )


@dataclass(slots=True)
class BackupResult:
    packet_path: Path
    manifest: BackupManifest
    state_bytes: int
    journal_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_path": str(self.packet_path),
            "manifest": self.manifest.to_dict(),
            "state_bytes": self.state_bytes,
            "journal_bytes": self.journal_bytes,
        }


@dataclass(slots=True)
class InspectionReport:
    packet_path: Path
    stage_dir: Path
    ok: bool
    issues: list[Issue] = field(default_factory=list)
    manifest: BackupManifest | None = None
    migration: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_path": str(self.packet_path),
            "stage_dir": str(self.stage_dir),
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "manifest": None if self.manifest is None else self.manifest.to_dict(),
            "migration": self.migration,
            "summary": dict(self.summary),
        }


class BackupError(Exception):
    """Raised when a live source directory cannot be exported."""


# --------------------------------------------------------------------------- #
# Hashing and journal helpers
# --------------------------------------------------------------------------- #


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_file_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read()


def parse_journal_bytes(data: bytes) -> list[dict[str, Any]]:
    """Parse the journal as either JSONL lines or a single JSON array."""

    text = data.decode("utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped[0] == "[":
        parsed = json.loads(stripped)
        if not isinstance(parsed, list):
            raise ValueError("journal JSON payload is not a list")
        events: list[dict[str, Any]] = []
        for index, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(f"journal[{index}] is not an object")
            events.append(dict(item))
        return events
    events = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f"journal line {line_no} is not a JSON object")
        events.append(dict(item))
    return events


def serialize_journal_events(events: list[dict[str, Any]]) -> bytes:
    lines = [json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def extract_state_events(raw_state: dict[str, Any]) -> list[dict[str, Any]]:
    events = raw_state.get("events", [])
    if not isinstance(events, list):
        raise ValueError("state field 'events' is not a list")
    for index, item in enumerate(events):
        if not isinstance(item, dict):
            raise ValueError(f"state events[{index}] is not an object")
    return [dict(item) for item in events]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def create_backup(data_dir: Path | str, packet_path: Path | str | None = None) -> BackupResult:
    """Export the live data directory into an offline packet.

    The state file must exist and both files must be readable; a corrupted live
    directory is never exported as if it were a valid backup.
    """

    from ..domain.timeutil import now_iso

    source = Path(data_dir)
    state_path = source / STATE_FILE
    journal_path = source / JOURNAL_FILE
    if not state_path.is_file():
        raise BackupError(f"no {STATE_FILE} in {source}; nothing to export")
    state_bytes = _read_file_bytes(state_path)
    journal_bytes = b""
    if journal_path.is_file():
        journal_bytes = _read_file_bytes(journal_path)

    # Prove the source is at least structurally readable before packaging it.
    try:
        raw_state = json.loads(state_bytes.decode("utf-8"))
        if not isinstance(raw_state, dict):
            raise ValueError("state root is not a JSON object")
        state_events = extract_state_events(raw_state)
        journal_events = parse_journal_bytes(journal_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError(f"cannot export unreadable workspace in {source}: {exc}") from exc

    schema_version = int(raw_state.get("schema_version", 0))
    event_range = EventRange.from_events(state_events)
    exported_at = now_iso()
    files = {
        STATE_FILE: {"sha256": sha256_hex(state_bytes), "bytes": len(state_bytes)},
        JOURNAL_FILE: {"sha256": sha256_hex(journal_bytes), "bytes": len(journal_bytes)},
    }
    manifest = BackupManifest(
        packet_format_version=PACKET_FORMAT_VERSION,
        exported_at=exported_at,
        source_dir=str(source),
        state_schema_version=schema_version,
        event_range=event_range,
        files=files,
    )
    manifest_bytes = (json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )

    target = Path(packet_path) if packet_path is not None else _default_packet_path(source, exported_at)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp_target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_FILE, manifest_bytes)
            archive.writestr(STATE_FILE, state_bytes)
            archive.writestr(JOURNAL_FILE, journal_bytes)
        os.replace(tmp_target, target)
    except BaseException:
        tmp_target.unlink(missing_ok=True)
        raise
    return BackupResult(
        packet_path=target,
        manifest=manifest,
        state_bytes=len(state_bytes),
        journal_bytes=len(journal_bytes),
    )


def _default_packet_path(source: Path, exported_at: str) -> Path:
    stamp = exported_at.replace(":", "").replace("-", "")
    return source.parent / f"switchyard-backup-{stamp}{PACKET_SUFFIX}"


# --------------------------------------------------------------------------- #
# Isolated extraction and inspection
# --------------------------------------------------------------------------- #


def _extract_isolated(packet_path: Path, root_dir: Path | None) -> tuple[Path, list[Issue]]:
    issues: list[Issue] = []
    stage = Path(tempfile.mkdtemp(prefix="sypack-stage-", dir=None if root_dir is None else str(root_dir)))
    try:
        with zipfile.ZipFile(packet_path) as archive:
            bad = archive.testzip()
            if bad is not None:
                issues.append(Issue.error("integrity", "ZIP_CRC", f"member {bad!r} failed CRC check"))
            members = archive.namelist()
            unexpected = [name for name in members if name not in _ALLOWED_MEMBERS]
            if unexpected:
                issues.append(
                    Issue.error("integrity", "UNEXPECTED_MEMBERS", f"packet contains unknown files: {unexpected}")
                )
            archive.extractall(stage)
    except zipfile.BadZipFile as exc:
        issues.append(Issue.error("integrity", "BAD_ARCHIVE", f"packet is not a readable zip: {exc}"))
    except KeyError as exc:
        issues.append(Issue.error("integrity", "MISSING_MEMBER", f"packet member missing: {exc}"))
    except OSError as exc:
        issues.append(Issue.error("integrity", "EXTRACT_FAILED", f"staging extraction failed: {exc}"))
    return stage, issues


def inspect_packet(
    packet_path: Path | str,
    *,
    keep_stage: bool = False,
    stage_root: Path | str | None = None,
) -> InspectionReport:
    """Validate a packet entirely inside an isolated staging directory."""

    packet = Path(packet_path)
    root = None if stage_root is None else Path(stage_root)
    stage, issues = _extract_isolated(packet, root)
    manifest: BackupManifest | None = None

    def fail_fast() -> InspectionReport:
        report = InspectionReport(packet_path=packet, stage_dir=stage, ok=False, issues=issues, manifest=manifest)
        if not keep_stage:
            shutil.rmtree(stage, ignore_errors=True)
        return report

    if not packet.is_file():
        issues.append(Issue.error("integrity", "PACKET_MISSING", f"packet file does not exist: {packet}"))
    if any(issue.severity == "error" for issue in issues):
        return fail_fast()

    # Layer 1: manifest and per-file digests.
    manifest_path = stage / MANIFEST_FILE
    state_path = stage / STATE_FILE
    journal_path = stage / JOURNAL_FILE
    for member in PACKET_MEMBERS:
        if not (stage / member).is_file():
            issues.append(Issue.error("integrity", "MISSING_MEMBER", f"packet member missing after extract: {member}"))
    if issues:
        return fail_fast()
    try:
        manifest = BackupManifest.from_dict(json.loads(_read_file_bytes(manifest_path).decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        issues.append(Issue.error("integrity", "MANIFEST_UNREADABLE", f"manifest is not valid JSON: {exc}"))
        return fail_fast()

    if manifest.packet_format_version != PACKET_FORMAT_VERSION:
        issues.append(
            Issue.error(
                "integrity",
                "PACKET_FORMAT_UNSUPPORTED",
                f"packet format version {manifest.packet_format_version} is not supported "
                f"(expected {PACKET_FORMAT_VERSION})",
            )
        )
    for name, path in ((STATE_FILE, state_path), (JOURNAL_FILE, journal_path)):
        entry = manifest.files.get(name)
        if entry is None:
            issues.append(Issue.error("integrity", "MANIFEST_ENTRY_MISSING", f"manifest has no checksum for {name}"))
            continue
        data = _read_file_bytes(path)
        if int(entry.get("bytes", -1)) != len(data):
            issues.append(
                Issue.error(
                    "integrity",
                    "SIZE_MISMATCH",
                    f"{name}: manifest declares {entry.get('bytes')} bytes but packet holds {len(data)}",
                )
            )
        if str(entry.get("sha256", "")) != sha256_hex(data):
            issues.append(
                Issue.error("integrity", "CHECKSUM_MISMATCH", f"{name}: SHA-256 digest does not match manifest")
            )
    if issues:
        return fail_fast()

    # Layer 2/3: parse, migrate legacy content, decode every entity.
    raw_state: dict[str, Any]
    journal_events: list[dict[str, Any]]
    try:
        parsed_state = json.loads(_read_file_bytes(state_path).decode("utf-8"))
        if not isinstance(parsed_state, dict):
            raise ValueError("state root is not a JSON object")
        raw_state = dict(parsed_state)
        journal_events = parse_journal_bytes(_read_file_bytes(journal_path))
    except (ValueError, UnicodeDecodeError) as exc:
        issues.append(Issue.error("readability", "JSON_UNREADABLE", f"payload JSON cannot be parsed: {exc}"))
        return fail_fast()

    state_version = raw_state.get("schema_version", SCHEMA_VERSION)
    migration_preview: dict[str, Any] | None = None
    if is_legacy_version(state_version):
        preview = preview_migration(raw_state, journal_events)
        migration_preview = preview.to_dict()
        if not preview.migratable:
            for item in preview.issues:
                if item.get("severity") == "error":
                    issues.append(
                        Issue.error(
                            "migration",
                            str(item.get("code", "MIGRATION_BLOCKED")),
                            str(item.get("message", "legacy data cannot be migrated")),
                        )
                    )
        if issues:
            report = InspectionReport(
                packet_path=packet,
                stage_dir=stage,
                ok=False,
                issues=issues,
                manifest=manifest,
                migration=migration_preview,
            )
            return _finalize(report, stage, keep_stage)
        try:
            raw_state = migrate_state(raw_state)
            journal_events = migrate_journal_events(journal_events)
        except ValueError as exc:
            issues.append(Issue.error("migration", "MIGRATION_FAILED", str(exc)))
            return fail_fast()
    else:
        try:
            state_version_int = int(state_version)
        except (TypeError, ValueError):
            issues.append(Issue.error("readability", "SCHEMA_VERSION_INVALID", f"schema_version {state_version!r}"))
            return fail_fast()
        if state_version_int > SCHEMA_VERSION:
            issues.append(
                Issue.error(
                    "readability",
                    "SCHEMA_VERSION_NEWER",
                    f"state schema_version {state_version_int} is newer than supported {SCHEMA_VERSION}",
                )
            )
            return fail_fast()

    workspace, decode_issues = _decode_all_entities(raw_state)
    issues.extend(decode_issues)
    if decode_issues:
        report = InspectionReport(
            packet_path=packet,
            stage_dir=stage,
            ok=False,
            issues=issues,
            manifest=manifest,
            migration=migration_preview,
        )
        return _finalize(report, stage, keep_stage)

    # Layer 4: basic consistency between state, events and journal.
    state_events_dicts = extract_state_events(raw_state)
    consistency_issues = _consistency_issues(workspace, state_events_dicts, journal_events, manifest)
    issues.extend(consistency_issues)

    summary = _workspace_summary(workspace, state_events_dicts, journal_events)
    report = InspectionReport(
        packet_path=packet,
        stage_dir=stage,
        ok=not any(issue.severity == "error" for issue in issues),
        issues=issues,
        manifest=manifest,
        migration=migration_preview,
        summary=summary,
    )
    return _finalize(report, stage, keep_stage)


def _finalize(report: InspectionReport, stage: Path, keep_stage: bool) -> InspectionReport:
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return report


def _decode_all_entities(raw_state: dict[str, Any]) -> tuple[Any, list[Issue]]:
    """Decode each entity collection independently so every bad field is seen."""

    issues: list[Issue] = []
    workspace = None
    try:
        workspace = decode_workspace(raw_state)
    except Exception as exc:  # codec surfaces the first failure only; sectionize below
        section_workspace, section_issues = _decode_by_sections(raw_state)
        if section_issues:
            return section_workspace, section_issues
        issues.append(Issue.error("readability", "DECODE_FAILED", f"state cannot be decoded: {exc}"))
        return section_workspace, issues
    return workspace, issues


def _decode_by_sections(raw_state: dict[str, Any]) -> tuple[Any, list[Issue]]:
    from ..domain.car import FreightCar
    from ..domain.intake import IntakeTrain
    from ..domain.outbound import OutboundTrain
    from ..domain.pull import PullRun, YardEvent
    from ..domain.shift import YardShift
    from ..domain.track import BufferBay, StandingTrack
    from .workspace import YardWorkspace

    decoders = {
        "tracks": ("tracks", StandingTrack.from_dict),
        "buffer_bays": ("buffer_bays", BufferBay.from_dict),
        "cars": ("cars", FreightCar.from_dict),
        "intakes": ("intakes", IntakeTrain.from_dict),
        "outbounds": ("outbounds", OutboundTrain.from_dict),
        "pull_runs": ("pull_runs", PullRun.from_dict),
        "shifts": ("shifts", YardShift.from_dict),
        "events": ("events", YardEvent.from_dict),
    }
    decoded: dict[str, dict[str, Any]] = {
        "tracks": {},
        "buffer_bays": {},
        "cars": {},
        "intakes": {},
        "outbounds": {},
        "runs": {},
        "shifts": {},
    }
    event_list: list[Any] = []
    issues: list[Issue] = []
    for field_name, (raw_name, decoder) in decoders.items():
        items = raw_state.get(raw_name, [])
        if not isinstance(items, list):
            issues.append(Issue.error("readability", "FIELD_SHAPE", f"state field {raw_name!r} is not a list"))
            continue
        for index, item in enumerate(items):
            try:
                value = decoder(dict(item))
            except Exception as exc:
                code = item.get("code") if isinstance(item, dict) else item
                issues.append(
                    Issue.error(
                        "readability",
                        "FIELD_UNREADABLE",
                        f"{raw_name}[{index}] ({code!r}) cannot be decoded: {type(exc).__name__}: {exc}",
                    )
                )
                continue
            if field_name == "events":
                event_list.append(value)
            else:
                bucket = "runs" if field_name == "pull_runs" else field_name
                decoded[bucket][value.code] = value
    workspace = YardWorkspace(
        tracks=decoded["tracks"],
        buffer_bays=decoded["buffer_bays"],
        cars=decoded["cars"],
        intakes=decoded["intakes"],
        outbounds=decoded["outbounds"],
        runs=decoded["runs"],
        shifts=decoded["shifts"],
        events=event_list,
        closure_snapshots=list(raw_state.get("closure_snapshots", [])),
    )
    return workspace, issues


# --------------------------------------------------------------------------- #
# Consistency checks
# --------------------------------------------------------------------------- #


def _consistency_issues(
    workspace: Any,
    state_events: list[dict[str, Any]],
    journal_events: list[dict[str, Any]],
    manifest: BackupManifest,
) -> list[Issue]:
    issues: list[Issue] = []

    # Event sequences must be gap-free and match next_event_sequence.
    sequences = [event.sequence for event in workspace.events]
    if sequences:
        expected = list(range(sequences[0], sequences[0] + len(sequences)))
        if sequences != expected:
            issues.append(
                Issue.error(
                    "consistency",
                    "EVENT_SEQUENCE_GAP",
                    f"state event sequences are not gap-free: got {sequences[:20]}...",
                )
            )
        if sequences[-1] >= workspace.next_event_sequence:
            issues.append(
                Issue.error(
                    "consistency",
                    "EVENT_COUNTER_STALE",
                    f"last event sequence {sequences[-1]} but next_event_sequence "
                    f"is {workspace.next_event_sequence}",
                )
            )

    # Every journal event must appear in state events in the same order.
    state_sequences = [int(event["sequence"]) for event in state_events]
    journal_sequences = [int(event["sequence"]) for event in journal_events]
    if journal_sequences:
        state_prefix = state_sequences[: len(journal_sequences)]
        if state_prefix != journal_sequences:
            issues.append(
                Issue.error(
                    "consistency",
                    "JOURNAL_NOT_PREFIX",
                    "journal events are not an in-order prefix of state events "
                    f"(journal {_short(journal_sequences)}, state {_short(state_sequences)})",
                )
            )
        for index, event in enumerate(journal_events):
            matching = state_events[index] if index < len(state_events) else None
            if matching is None or json.dumps(event, sort_keys=True) != json.dumps(matching, sort_keys=True):
                issues.append(
                    Issue.error(
                        "consistency",
                        "JOURNAL_EVENT_MISMATCH",
                        f"journal event sequence {event.get('sequence')} differs from the state record",
                    )
                )
                break
    elif state_events:
        issues.append(Issue.warning("consistency", "JOURNAL_EMPTY", "state holds events but journal is empty"))

    # Manifest event range must describe the state events.
    declared = manifest.event_range
    actual = EventRange.from_events(state_events)
    if (declared.count, declared.first_sequence, declared.last_sequence) != (
        actual.count,
        actual.first_sequence,
        actual.last_sequence,
    ):
        issues.append(
            Issue.error(
                "consistency",
                "EVENT_RANGE_MISMATCH",
                f"manifest event range {declared.to_dict()} does not match state events {actual.to_dict()}",
            )
        )

    issues.extend(_reference_issues(workspace))
    return issues


def _short(values: list[int]) -> str:
    text = ", ".join(str(value) for value in values[:10])
    if len(values) > 10:
        text += ", ..."
    return f"[{text}]"


def _reference_issues(workspace: Any) -> list[Issue]:
    issues: list[Issue] = []
    car_codes = set(workspace.cars)
    track_codes = set(workspace.tracks)
    bay_codes = set(workspace.buffer_bays)
    outbound_codes = set(workspace.outbounds)
    # Cars waiting for classification are parked at the virtual intake siding.
    valid_locations = track_codes | bay_codes | outbound_codes | {"INTAKE"}

    for code, track in workspace.tracks.items():
        for car_code in track.stack:
            if car_code not in car_codes:
                issues.append(
                    Issue.error("consistency", "DANGLING_REFERENCE", f"track {code} stacks unknown car {car_code}")
                )
    for code, bay in workspace.buffer_bays.items():
        for car_code in bay.stack:
            if car_code not in car_codes:
                issues.append(
                    Issue.error("consistency", "DANGLING_REFERENCE", f"buffer bay {code} stacks unknown car {car_code}")
                )
    for code, car in workspace.cars.items():
        if car.location is not None and car.location not in valid_locations:
            issues.append(
                Issue.error(
                    "consistency",
                    "DANGLING_REFERENCE",
                    f"car {code} references unknown location {car.location!r}",
                )
            )
    for code, train in workspace.intakes.items():
        for car_code in list(train.consist) + list(train.unplaced):
            if car_code not in car_codes:
                issues.append(
                    Issue.error(
                        "consistency", "DANGLING_REFERENCE", f"intake {code} references unknown car {car_code}"
                    )
                )
    for code, train in workspace.outbounds.items():
        for car_code in list(train.planned_car_codes) + list(train.assembled_car_codes):
            if car_code not in car_codes:
                issues.append(
                    Issue.error(
                        "consistency", "DANGLING_REFERENCE", f"outbound {code} references unknown car {car_code}"
                    )
                )
        for run_code in train.run_codes:
            if run_code not in workspace.runs:
                issues.append(
                    Issue.error(
                        "consistency", "DANGLING_REFERENCE", f"outbound {code} references unknown pull run {run_code}"
                    )
                )
    for code, run in workspace.runs.items():
        if run.outbound_code not in workspace.outbounds:
            issues.append(
                Issue.error(
                    "consistency",
                    "DANGLING_REFERENCE",
                    f"pull run {code} references unknown outbound {run.outbound_code}",
                )
            )
        if run.transfer_code not in bay_codes:
            issues.append(
                Issue.error(
                    "consistency",
                    "DANGLING_REFERENCE",
                    f"pull run {code} references unknown buffer bay {run.transfer_code}",
                )
            )
        for step in run.steps:
            # BUFFER/RETURN move between tracks and bays; PULL ends on the
            # outbound train being assembled.
            valid_targets = track_codes | bay_codes
            if str(step.verb) == "PULL":
                valid_targets |= outbound_codes
            if step.source_code not in track_codes and step.source_code not in bay_codes:
                issues.append(
                    Issue.error(
                        "consistency",
                        "DANGLING_REFERENCE",
                        f"pull run {code} step references unknown source track or bay {step.source_code}",
                    )
                )
            if step.target_code not in valid_targets:
                issues.append(
                    Issue.error(
                        "consistency",
                        "DANGLING_REFERENCE",
                        f"pull run {code} step references unknown target {step.target_code}",
                    )
                )
            if step.car_code not in car_codes:
                issues.append(
                    Issue.error(
                        "consistency",
                        "DANGLING_REFERENCE",
                        f"pull run {code} step references unknown car {step.car_code}",
                    )
                )
    shift_codes = set(workspace.shifts)
    for event in workspace.events:
        if event.shift_code not in shift_codes:
            issues.append(
                Issue.error(
                    "consistency",
                    "DANGLING_REFERENCE",
                    f"event {event.sequence} references unknown shift {event.shift_code}",
                )
            )
        try:
            EventKind.parse(str(event.kind))
        except ValueError:
            issues.append(
                Issue.error(
                    "consistency",
                    "EVENT_KIND_UNKNOWN",
                    f"event {event.sequence} uses unknown kind {event.kind!r}",
                )
            )
    for index, snapshot in enumerate(workspace.closure_snapshots):
        if not isinstance(snapshot, dict):
            issues.append(
                Issue.error("readability", "FIELD_UNREADABLE", f"closure_snapshots[{index}] is not an object")
            )
            continue
        shift_code = snapshot.get("shift_code")
        if shift_code is not None and shift_code not in shift_codes:
            issues.append(
                Issue.error(
                    "consistency",
                    "DANGLING_REFERENCE",
                    f"closure snapshot {snapshot.get('code', index)} references unknown shift {shift_code}",
                )
            )
    return issues


def _workspace_summary(
    workspace: Any,
    state_events: list[dict[str, Any]],
    journal_events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": workspace.schema_version,
        "shifts": len(workspace.shifts),
        "cars": len(workspace.cars),
        "tracks": len(workspace.tracks),
        "intakes": len(workspace.intakes),
        "outbounds": len(workspace.outbounds),
        "pull_runs": len(workspace.runs),
        "state_events": len(state_events),
        "journal_events": len(journal_events),
        "closure_snapshots": len(workspace.closure_snapshots),
        "event_range": EventRange.from_events(state_events).to_dict(),
    }


def canonical_payloads(raw_state: dict[str, Any], journal_events: list[dict[str, Any]]) -> tuple[bytes, bytes]:
    """Re-encode validated data through the codec so output is normalized."""

    workspace = decode_workspace(raw_state)
    state_text = json.dumps(encode_workspace(workspace), ensure_ascii=False, indent=2, sort_keys=True)
    state_bytes = (state_text + "\n").encode("utf-8")
    journal_bytes = serialize_journal_events(journal_events)
    return state_bytes, journal_bytes


__all__ = [
    "MANIFEST_FILE",
    "PACKET_FORMAT_VERSION",
    "PACKET_SUFFIX",
    "BackupError",
    "BackupManifest",
    "BackupResult",
    "EventRange",
    "InspectionReport",
    "Issue",
    "canonical_payloads",
    "create_backup",
    "extract_state_events",
    "inspect_packet",
    "parse_journal_bytes",
    "sha256_hex",
]
