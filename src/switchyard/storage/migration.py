"""Preview and upgrade legacy workspace data into the current schema.

Only schema version 0 packets are considered legacy.  The v0 layout used
different field and enum names; every rename and value mapping applied here is
surfaced in a :class:`MigrationPreview` so operators can review the result
before restoring.  Values without a safe mapping are reported as blocking
issues with an explicit reason instead of being silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .workspace import SCHEMA_VERSION

LEGACY_SCHEMA_VERSION = 0
SUPPORTED_LEGACY_VERSIONS = {LEGACY_SCHEMA_VERSION}

# Legacy car kinds used shorter, inconsistent codes.
LEGACY_CAR_KINDS = {
    "BOXCAR": "BOX",
    "BOX": "BOX",
    "FLATCAR": "FLAT",
    "FLAT": "FLAT",
    "HOPPER": "HOPPER",
    "TANK": "TANK",
    "REEFER": "REEFER",
}

# Legacy car lifecycle names.
LEGACY_CAR_STATES = {
    "NEW": "RECEIVED",
    "RECEIVED": "RECEIVED",
    "PARKED": "STANDING",
    "STANDING": "STANDING",
    "HELD": "RESERVED",
    "RESERVED": "RESERVED",
    "BUILT": "ASSEMBLED",
    "ASSEMBLED": "ASSEMBLED",
    "GONE": "DEPARTED",
    "DEPARTED": "DEPARTED",
    "DROPPED": "REMOVED",
    "REMOVED": "REMOVED",
}

LEGACY_INTAKE_STATES = {
    "OPEN": "OPEN",
    "PARTIAL": "PARTIAL",
    "DONE": "CLASSIFIED",
    "CLASSIFIED": "CLASSIFIED",
    "CANCELLED": "CANCELLED",
}

# Legacy journal used operational verbs instead of the stable event names.
LEGACY_EVENT_KINDS = {
    "SHIFT_OPEN": "SHIFT_OPENED",
    "SHIFT_OPENED": "SHIFT_OPENED",
    "TRAIN_IN": "TRAIN_RECEIVED",
    "TRAIN_RECEIVED": "TRAIN_RECEIVED",
    "TRAIN_DONE": "TRAIN_CLASSIFIED",
    "TRAIN_CLASSIFIED": "TRAIN_CLASSIFIED",
    "PLAN_CREATED": "TRAIN_CREATED",
    "TRAIN_CREATED": "TRAIN_CREATED",
    "PULL_PLAN": "PULL_PLANNED",
    "PULL_PLANNED": "PULL_PLANNED",
    "RUN_START": "PULL_RUN_STARTED",
    "PULL_RUN_STARTED": "PULL_RUN_STARTED",
    "RUN_STEP": "PULL_RUN_ADVANCED",
    "PULL_RUN_ADVANCED": "PULL_RUN_ADVANCED",
    "RUN_DONE": "PULL_RUN_COMPLETED",
    "PULL_RUN_COMPLETED": "PULL_RUN_COMPLETED",
    "TRAIN_OUT": "TRAIN_DEPARTED",
    "TRAIN_DEPARTED": "TRAIN_DEPARTED",
    "SHIFT_LOCK": "SHIFT_CLOSED",
    "SHIFT_CLOSED": "SHIFT_CLOSED",
    "CLOSURE_DENIED": "CLOSURE_BLOCKED",
    "CLOSURE_BLOCKED": "CLOSURE_BLOCKED",
    "YARD_CHECK": "YARD_VIEWED",
    "YARD_VIEWED": "YARD_VIEWED",
}


@dataclass(slots=True)
class MigrationPreview:
    from_version: int
    to_version: int
    migratable: bool
    changes: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "migratable": self.migratable,
            "changes": list(self.changes),
            "issues": list(self.issues),
            "counts": dict(self.counts),
        }


def is_legacy_version(schema_version: Any) -> bool:
    try:
        return int(schema_version) in SUPPORTED_LEGACY_VERSIONS
    except (TypeError, ValueError):
        return False


def preview_migration(
    raw_state: dict[str, Any],
    journal_events: list[dict[str, Any]] | None = None,
) -> MigrationPreview:
    """Dry-run the v0 -> current upgrade without mutating the inputs."""

    import copy

    transformed, journal_out, changes, issues = _transform(
        copy.deepcopy(raw_state),
        [] if journal_events is None else copy.deepcopy(journal_events),
    )
    counts = {
        "tracks": len(transformed.get("tracks", [])),
        "buffer_bays": len(transformed.get("buffer_bays", [])),
        "cars": len(transformed.get("cars", [])),
        "intakes": len(transformed.get("intakes", [])),
        "outbounds": len(transformed.get("outbounds", [])),
        "pull_runs": len(transformed.get("pull_runs", [])),
        "shifts": len(transformed.get("shifts", [])),
        "events": len(transformed.get("events", [])),
        "journal_events": len(journal_out),
        "closure_snapshots": len(transformed.get("closure_snapshots", [])),
    }
    return MigrationPreview(
        from_version=LEGACY_SCHEMA_VERSION,
        to_version=SCHEMA_VERSION,
        migratable=not any(issue["severity"] == "error" for issue in issues),
        changes=changes,
        issues=issues,
        counts=counts,
    )


def migrate_state(raw_state: dict[str, Any]) -> dict[str, Any]:
    """Return an upgraded copy of a v0 state document."""

    import copy

    transformed, _, _, issues = _transform(copy.deepcopy(raw_state), [])
    blocking = [issue for issue in issues if issue["severity"] == "error"]
    if blocking:
        reasons = "; ".join(issue["message"] for issue in blocking)
        raise ValueError(f"legacy state cannot be migrated: {reasons}")
    return transformed


def migrate_journal_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upgrade v0 journal event records to the current shape."""

    _, journal_out, _, issues = _transform({"schema_version": 0}, list(events))
    blocking = [issue for issue in issues if issue["severity"] == "error"]
    if blocking:
        reasons = "; ".join(issue["message"] for issue in blocking)
        raise ValueError(f"legacy journal cannot be migrated: {reasons}")
    return journal_out


def _issue(code: str, message: str) -> dict[str, Any]:
    return {"severity": "error", "stage": "migration", "code": code, "message": message}


def _transform(
    raw: dict[str, Any],
    journal_events: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    changes: list[str] = []
    issues: list[dict[str, Any]] = []

    def record(path: str, old: Any, new: Any) -> None:
        changes.append(f"{path}: {old!r} -> {new!r}")

    out = dict(raw)

    out["tracks"] = [_migrate_track(item, index, issues, record) for index, item in enumerate(raw.get("tracks", []))]
    out["buffer_bays"] = [dict(item) for item in raw.get("buffer_bays", [])]
    out["cars"] = [_migrate_car(item, index, issues, record) for index, item in enumerate(raw.get("cars", []))]
    out["intakes"] = [
        _migrate_intake(item, index, issues, record)
        for index, item in enumerate(raw.get("inbound_trains", raw.get("intakes", [])))
    ]
    if "inbound_trains" in raw:
        changes.append("top-level 'inbound_trains' -> 'intakes'")
    out["outbounds"] = [
        _migrate_outbound(item, index, issues, record)
        for index, item in enumerate(raw.get("departure_trains", raw.get("outbounds", [])))
    ]
    if "departure_trains" in raw:
        changes.append("top-level 'departure_trains' -> 'outbounds'")
    out["pull_runs"] = [dict(item) for item in raw.get("pull_runs", [])]
    out["shifts"] = [dict(item) for item in raw.get("shifts", [])]
    out["events"] = [
        _migrate_event(item, f"events[{index}]", issues, record)
        for index, item in enumerate(raw.get("events", []))
    ]
    out["closure_snapshots"] = [
        _migrate_snapshot(item, index, issues, record)
        for index, item in enumerate(raw.get("snapshots", raw.get("closure_snapshots", [])))
    ]
    if "snapshots" in raw:
        changes.append("top-level 'snapshots' -> 'closure_snapshots'")

    journal_out = [
        _migrate_event(item, f"journal[{index}]", issues, record)
        for index, item in enumerate(journal_events)
    ]

    out["schema_version"] = SCHEMA_VERSION
    out.setdefault("version", 1)
    out.setdefault("next_event_sequence", len(out["events"]) + 1)
    return out, journal_out, changes, issues


def _migrate_track(item: Any, index: int, issues: list[dict[str, Any]], record: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        issues.append(_issue("LEGACY_TRACK_SHAPE", f"tracks[{index}] is not an object"))
        return {}
    track = dict(item)
    if "destination_affinity" in track:
        record(f"tracks[{index}].destination_affinity", track["destination_affinity"], "destination")
        track["destination"] = track.pop("destination_affinity")
    return track


def _migrate_car(item: Any, index: int, issues: list[dict[str, Any]], record: Any) -> dict[str, Any]:
    path = f"cars[{index}]"
    if not isinstance(item, dict):
        issues.append(_issue("LEGACY_CAR_SHAPE", f"{path} is not an object"))
        return {}
    car = dict(item)
    for required in ("code", "destination", "length_m"):
        if required not in car:
            issues.append(_issue("LEGACY_CAR_MISSING_FIELD", f"{path} is missing required field {required!r}"))
    kind = str(car.get("kind", "")).upper()
    if kind not in LEGACY_CAR_KINDS:
        issues.append(
            _issue(
                "LEGACY_CAR_KIND_UNKNOWN",
                f"{path} ({car.get('code', '?')}) uses unknown legacy kind {kind!r} with no mapping",
            )
        )
    else:
        mapped = LEGACY_CAR_KINDS[kind]
        if mapped != kind:
            record(f"{path}.kind", kind, mapped)
        car["kind"] = mapped
    state = str(car.get("state", "NEW")).upper()
    if state not in LEGACY_CAR_STATES:
        issues.append(
            _issue(
                "LEGACY_CAR_STATE_UNKNOWN",
                f"{path} ({car.get('code', '?')}) uses unknown legacy state {state!r} with no mapping",
            )
        )
    else:
        mapped_state = LEGACY_CAR_STATES[state]
        if mapped_state != state:
            record(f"{path}.state", state, mapped_state)
        car["state"] = mapped_state
    if "danger_class" not in car:
        hazmat = bool(car.pop("hazmat", False))
        car["danger_class"] = "UNSPECIFIED" if hazmat else "NONE"
        record(f"{path}.danger_class", f"hazmat={hazmat}", car["danger_class"])
    return car


def _migrate_intake(item: Any, index: int, issues: list[dict[str, Any]], record: Any) -> dict[str, Any]:
    path = f"intakes[{index}]"
    if not isinstance(item, dict):
        issues.append(_issue("LEGACY_INTAKE_SHAPE", f"{path} is not an object"))
        return {}
    intake = dict(item)
    if "cars" in intake:
        record(f"{path}.cars", "list of car codes", "consist")
        intake["consist"] = intake.pop("cars")
    state = str(intake.get("state", "OPEN")).upper()
    if state not in LEGACY_INTAKE_STATES:
        issues.append(
            _issue("LEGACY_INTAKE_STATE_UNKNOWN", f"{path} uses unknown legacy intake state {state!r}")
        )
    else:
        mapped = LEGACY_INTAKE_STATES[state]
        if mapped != state:
            record(f"{path}.state", state, mapped)
        intake["state"] = mapped
    return intake


def _migrate_outbound(item: Any, index: int, issues: list[dict[str, Any]], record: Any) -> dict[str, Any]:
    path = f"outbounds[{index}]"
    if not isinstance(item, dict):
        issues.append(_issue("LEGACY_OUTBOUND_SHAPE", f"{path} is not an object"))
        return {}
    outbound = dict(item)
    if "sequence" in outbound:
        record(f"{path}.sequence", "planned car codes", "planned_car_codes")
        outbound["planned_car_codes"] = outbound.pop("sequence")
    if "built_sequence" in outbound:
        record(f"{path}.built_sequence", "assembled car codes", "assembled_car_codes")
        outbound["assembled_car_codes"] = outbound.pop("built_sequence")
    return outbound


def _migrate_snapshot(item: Any, index: int, issues: list[dict[str, Any]], record: Any) -> dict[str, Any]:
    path = f"closure_snapshots[{index}]"
    if not isinstance(item, dict):
        issues.append(_issue("LEGACY_SNAPSHOT_SHAPE", f"{path} is not an object"))
        return {}
    snapshot = dict(item)
    if "snapshot_code" in snapshot:
        record(f"{path}.snapshot_code", snapshot["snapshot_code"], "code")
        snapshot["code"] = snapshot.pop("snapshot_code")
    if "closed_shift" in snapshot:
        record(f"{path}.closed_shift", snapshot["closed_shift"], "shift_code")
        snapshot["shift_code"] = snapshot.pop("closed_shift")
    return snapshot


def _migrate_event(item: Any, path: str, issues: list[dict[str, Any]], record: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        issues.append(_issue("LEGACY_EVENT_SHAPE", f"{path} is not an object"))
        return {}
    event = dict(item)
    if "sequence" not in event:
        if "seq" in event:
            record(f"{path}.seq", event["seq"], "sequence")
            event["sequence"] = event.pop("seq")
        else:
            issues.append(_issue("LEGACY_EVENT_MISSING_SEQUENCE", f"{path} has neither 'seq' nor 'sequence'"))
    else:
        event.pop("seq", None)
    if "at" not in event:
        if "ts" in event:
            record(f"{path}.ts", event["ts"], "at")
            event["at"] = event.pop("ts")
        else:
            issues.append(_issue("LEGACY_EVENT_MISSING_TIME", f"{path} has neither 'ts' nor 'at'"))
    else:
        event.pop("ts", None)
    legacy_kind = event.pop("type", None)
    raw_kind = legacy_kind if legacy_kind is not None else event.get("kind")
    kind = str(raw_kind or "").upper()
    if kind not in LEGACY_EVENT_KINDS:
        issues.append(
            _issue(
                "LEGACY_EVENT_KIND_UNKNOWN",
                f"{path} (seq {event.get('sequence', '?')}) uses unknown legacy event type {kind!r}",
            )
        )
    else:
        mapped = LEGACY_EVENT_KINDS[kind]
        if mapped != kind:
            record(f"{path}.kind", kind, mapped)
        event["kind"] = mapped
    for required in ("shift_code", "message"):
        if required not in event:
            issues.append(_issue("LEGACY_EVENT_MISSING_FIELD", f"{path} is missing field {required!r}"))
    event.setdefault("payload", {})
    return event


__all__ = [
    "LEGACY_SCHEMA_VERSION",
    "MigrationPreview",
    "SUPPORTED_LEGACY_VERSIONS",
    "is_legacy_version",
    "migrate_journal_events",
    "migrate_state",
    "preview_migration",
]
