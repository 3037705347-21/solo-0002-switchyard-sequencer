"""Field-level diff engine for immutable closure snapshots.

The engine compares two *frozen* snapshot documents and never treats a missing
field as zero: whenever one side lacks a field (for example an older archive
document written before a metric existed), the comparison entry reports
``None`` together with an explicit ``missing_in_base`` / ``missing_in_target``
status instead of fabricating an empty list or a zero.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .metrics import CAR_STATE_KEYS

TRACK_LEAF_KEYS = (
    "state",
    "cars",
    "length_m",
    "capacity_cars",
    "capacity_length_m",
    "car_utilization",
    "length_utilization",
    "top_car",
)
TRACK_NUMERIC_KEYS = {
    "cars",
    "length_m",
    "capacity_cars",
    "capacity_length_m",
    "car_utilization",
    "length_utilization",
}
RUN_LEAF_KEYS = ("outbound_code", "state", "current_step", "total_steps", "remaining_steps")
RUN_NUMERIC_KEYS = {"current_step", "total_steps", "remaining_steps"}
TOTAL_LEAF_KEYS = ("total_cars", "event_count")
TOTAL_NUMERIC_KEYS = {"total_cars", "event_count"}

_MISSING = object()

# Sections that the archive contract expects every well-formed snapshot to
# carry. Older or damaged documents may lack them.
METRIC_MAP_SECTIONS = ("car_state_counts", "run_state_counts", "outbound_state_counts")
METRIC_LIST_SECTIONS = ("open_outbounds", "completed_outbounds", "unfinished_runs")


def snapshot_diff(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    base = deepcopy(base)
    target = deepcopy(target)
    base_metrics = _mapping(base.get("metrics"))
    target_metrics = _mapping(target.get("metrics"))
    sections: dict[str, Any] = {}
    sections["totals"] = _leaf_section(
        base_metrics,
        target_metrics,
        TOTAL_LEAF_KEYS,
        TOTAL_NUMERIC_KEYS,
    )
    sections["car_state_counts"] = _count_section(
        base_metrics.get("car_state_counts", _MISSING),
        target_metrics.get("car_state_counts", _MISSING),
        CAR_STATE_KEYS,
    )
    sections["track_occupancy"] = _track_section(
        base_metrics.get("track_occupancy", _MISSING),
        target_metrics.get("track_occupancy", _MISSING),
    )
    sections["open_outbounds"] = _code_list_section(
        base_metrics.get("open_outbounds", _MISSING),
        target_metrics.get("open_outbounds", _MISSING),
    )
    sections["completed_outbounds"] = _code_list_section(
        base_metrics.get("completed_outbounds", _MISSING),
        target_metrics.get("completed_outbounds", _MISSING),
    )
    sections["outbound_state_counts"] = _count_section(
        base_metrics.get("outbound_state_counts", _MISSING),
        target_metrics.get("outbound_state_counts", _MISSING),
        ("draft", "planned", "ready", "departed", "abandoned"),
    )
    sections["unfinished_runs"] = _run_section(
        base_metrics.get("unfinished_runs", _MISSING),
        target_metrics.get("unfinished_runs", _MISSING),
    )
    sections["run_state_counts"] = _count_section(
        base_metrics.get("run_state_counts", _MISSING),
        target_metrics.get("run_state_counts", _MISSING),
        ("queued", "running", "completed", "failed"),
    )
    sections["blockers"] = _blocker_section(
        base.get("blockers", _MISSING),
        target.get("blockers", _MISSING),
    )
    sections["source_event_range"] = _event_range_section(
        base.get("source_event_range", _MISSING),
        target.get("source_event_range", _MISSING),
    )
    missing_sections = [name for name, item in sections.items() if item["status"].startswith("missing")]
    summary = _summarize(sections)
    summary["missing_sections"] = missing_sections
    identical = (
        not missing_sections
        and summary["fields_changed"] == 0
        and summary["items_added"] == 0
        and summary["items_removed"] == 0
        and summary["items_changed"] == 0
    )
    return {
        "base": _snapshot_ref(base),
        "target": _snapshot_ref(target),
        "identical": identical,
        "summary": summary,
        "sections": sections,
    }


def _snapshot_ref(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": doc.get("code"),
        "shift_code": doc.get("shift_code"),
        "closed_at": doc.get("closed_at"),
        "version": doc.get("version"),
        "schema_version": doc.get("schema_version"),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _status_for(base: Any, target: Any) -> str:
    if base is _MISSING and target is _MISSING:
        return "missing_in_both"
    if base is _MISSING:
        return "missing_in_base"
    if target is _MISSING:
        return "missing_in_target"
    if base == target:
        return "equal"
    return "changed"


def _delta(base: Any, target: Any, numeric: bool) -> Any:
    if base is _MISSING or target is _MISSING:
        return None
    if numeric and isinstance(base, (int, float)) and isinstance(target, (int, float)) and not isinstance(
        base, bool
    ) and not isinstance(target, bool):
        return round(target - base, 4)
    return None


def _leaf_entry(base: Any, target: Any, numeric: bool = False) -> dict[str, Any]:
    return {
        "status": _status_for(base, target),
        "base": None if base is _MISSING else base,
        "target": None if target is _MISSING else target,
        "delta": _delta(base, target, numeric),
    }


def _section_status(base: Any, target: Any) -> str:
    if base is _MISSING and target is _MISSING:
        return "missing_in_both"
    if base is _MISSING:
        return "missing_in_base"
    if target is _MISSING:
        return "missing_in_target"
    return "compared"


def _leaf_section(
    base_metrics: dict[str, Any],
    target_metrics: dict[str, Any],
    keys: tuple[str, ...],
    numeric_keys: set[str],
) -> dict[str, Any]:
    section = {"status": "compared", "fields": {}}
    for key in keys:
        section["fields"][key] = _leaf_entry(
            base_metrics.get(key, _MISSING),
            target_metrics.get(key, _MISSING),
            numeric=key in numeric_keys,
        )
    section["status"] = _section_status_from_fields(section["fields"])
    return section


def _count_section(base: Any, target: Any, canonical_keys: tuple[str, ...]) -> dict[str, Any]:
    status = _section_status(base, target)
    base_map = _mapping(base) if base is not _MISSING else {}
    target_map = _mapping(target) if target is not _MISSING else {}
    keys = tuple(dict.fromkeys((*canonical_keys, *sorted(set(base_map) | set(target_map)))))
    fields = {
        key: _leaf_entry(base_map.get(key, _MISSING), target_map.get(key, _MISSING), numeric=True)
        for key in keys
    }
    return {"status": status, "fields": fields}


def _track_section(base: Any, target: Any) -> dict[str, Any]:
    status = _section_status(base, target)
    base_map = _mapping(base) if base is not _MISSING else {}
    target_map = _mapping(target) if target is not _MISSING else {}
    codes = sorted(set(base_map) | set(target_map))
    items: dict[str, Any] = {}
    for code in codes:
        base_track = base_map.get(code, _MISSING)
        target_track = target_map.get(code, _MISSING)
        if base_track is _MISSING or target_track is _MISSING:
            item_status = "missing_in_base" if base_track is _MISSING else "missing_in_target"
            fields = {}
            present = target_track if base_track is _MISSING else base_track
            present_map = _mapping(present)
            for leaf in tuple(dict.fromkeys((*TRACK_LEAF_KEYS, *sorted(present_map)))):
                fields[leaf] = _leaf_entry(
                    _MISSING if base_track is _MISSING else present_map.get(leaf, _MISSING),
                    present_map.get(leaf, _MISSING) if base_track is _MISSING else _MISSING,
                    numeric=leaf in TRACK_NUMERIC_KEYS,
                )
            items[code] = {"status": item_status, "fields": fields}
            continue
        fields = {}
        leaf_keys = tuple(dict.fromkeys((*TRACK_LEAF_KEYS, *sorted(set(base_track) | set(target_track)))))
        for leaf in leaf_keys:
            fields[leaf] = _leaf_entry(
                base_track.get(leaf, _MISSING),
                target_track.get(leaf, _MISSING),
                numeric=leaf in TRACK_NUMERIC_KEYS,
            )
        item_status = "changed" if any(entry["status"] == "changed" for entry in fields.values()) else "equal"
        items[code] = {"status": item_status, "fields": fields}
    return {
        "status": status,
        "items": items,
        "items_added": sorted(code for code in codes if code not in base_map),
        "items_removed": sorted(code for code in codes if code not in target_map),
    }


def _code_list_section(base: Any, target: Any) -> dict[str, Any]:
    status = _section_status(base, target)
    base_codes = sorted(base) if base is not _MISSING and isinstance(base, list) else None
    target_codes = sorted(target) if target is not _MISSING and isinstance(target, list) else None
    if base_codes is None or target_codes is None:
        added: list[str] = []
        removed: list[str] = []
    else:
        added = sorted(set(target_codes) - set(base_codes))
        removed = sorted(set(base_codes) - set(target_codes))
    return {
        "status": status,
        "codes": {"base": base_codes, "target": target_codes},
        "added": added,
        "removed": removed,
    }


def _index_runs(raw: Any) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "code" in item:
                indexed[str(item["code"])] = dict(item)
    return indexed


def _run_section(base: Any, target: Any) -> dict[str, Any]:
    status = _section_status(base, target)
    base_runs = _index_runs(base) if base is not _MISSING else {}
    target_runs = _index_runs(target) if target is not _MISSING else {}
    codes = sorted(set(base_runs) | set(target_runs))
    items: dict[str, Any] = {}
    for code in codes:
        base_run = base_runs.get(code, _MISSING)
        target_run = target_runs.get(code, _MISSING)
        if base_run is _MISSING or target_run is _MISSING:
            present = target_run if base_run is _MISSING else base_run
            items[code] = {
                "status": "missing_in_base" if base_run is _MISSING else "missing_in_target",
                "fields": {
                    leaf: _leaf_entry(
                        _MISSING if base_run is _MISSING else present.get(leaf, _MISSING),
                        present.get(leaf, _MISSING) if base_run is _MISSING else _MISSING,
                        numeric=leaf in RUN_NUMERIC_KEYS,
                    )
                    for leaf in tuple(dict.fromkeys((*RUN_LEAF_KEYS, *sorted(present))))
                },
            }
            continue
        fields = {
            leaf: _leaf_entry(
                base_run.get(leaf, _MISSING),
                target_run.get(leaf, _MISSING),
                numeric=leaf in RUN_NUMERIC_KEYS,
            )
            for leaf in tuple(dict.fromkeys((*RUN_LEAF_KEYS, *sorted(set(base_run) | set(target_run)))))
        }
        if any(entry["status"] == "changed" for entry in fields.values()):
            items[code] = {"status": "changed", "fields": fields}
    return {
        "status": status,
        "items": items,
        "items_added": sorted(code for code in codes if code not in base_runs),
        "items_removed": sorted(code for code in codes if code not in target_runs),
    }


def _blocker_key(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    return f"{item.get('kind', 'unknown')}:{item.get('code', 'unknown')}"


def _blocker_section(base: Any, target: Any) -> dict[str, Any]:
    status = _section_status(base, target)
    base_items = {key: item for item in (base if isinstance(base, list) else []) if (key := _blocker_key(item))}
    target_items = {key: item for item in (target if isinstance(target, list) else []) if (key := _blocker_key(item))}
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: dict[str, Any] = {}
    for key in sorted(set(base_items) | set(target_items)):
        if key not in base_items:
            added.append({"key": key, "blocker": target_items[key]})
        elif key not in target_items:
            removed.append({"key": key, "blocker": base_items[key]})
        elif base_items[key] != target_items[key]:
            changed[key] = {
                "base": base_items[key],
                "target": target_items[key],
            }
    return {
        "status": status,
        "items_added": added,
        "items_removed": removed,
        "items_changed": changed,
    }


def _event_range_section(base: Any, target: Any) -> dict[str, Any]:
    status = _section_status(base, target)
    base_map = _mapping(base) if base is not _MISSING else {}
    target_map = _mapping(target) if target is not _MISSING else {}
    keys = ("first_sequence", "last_sequence", "event_count")
    fields = {
        key: _leaf_entry(base_map.get(key, _MISSING), target_map.get(key, _MISSING), numeric=True)
        for key in keys
    }
    return {"status": status, "fields": fields}


def _section_status_from_fields(fields: dict[str, dict[str, Any]]) -> str:
    statuses = {entry["status"] for entry in fields.values()}
    if statuses <= {"equal"}:
        return "compared"
    if all(status.startswith("missing_in_base") for status in statuses):
        return "missing_in_base"
    if all(status.startswith("missing_in_target") for status in statuses):
        return "missing_in_target"
    return "compared"


def _summarize(sections: dict[str, Any]) -> dict[str, int]:
    fields_changed = 0
    items_added = 0
    items_removed = 0
    items_changed = 0

    def walk_fields(fields: dict[str, Any]) -> None:
        nonlocal fields_changed
        fields_changed += sum(1 for entry in fields.values() if entry["status"] == "changed")

    for name, section in sections.items():
        if "fields" in section and isinstance(section["fields"], dict):
            if name in {"track_occupancy", "unfinished_runs"}:
                for item in section.get("items", {}).values():
                    walk_fields(item["fields"])
            else:
                walk_fields(section["fields"])
        items_added += len(section.get("items_added", []))
        items_removed += len(section.get("items_removed", []))
        items_changed += len(section.get("items_changed", {})) if isinstance(
            section.get("items_changed"), dict
        ) else 0
        if name in {"open_outbounds", "completed_outbounds"}:
            items_added += len(section.get("added", []))
            items_removed += len(section.get("removed", []))
    return {
        "fields_changed": fields_changed,
        "items_added": items_added,
        "items_removed": items_removed,
        "items_changed": items_changed,
    }


__all__ = ["snapshot_diff"]
