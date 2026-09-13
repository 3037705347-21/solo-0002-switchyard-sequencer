"""Versioned intake manifest records and correction diffing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"
CHANGE_UPDATED = "updated"


def car_spec_dict(car: Any) -> dict[str, object]:
    """Snapshot the manifest-relevant fields of a FreightCar or CarInput."""
    return {
        "code": str(car.code),
        "kind": str(car.kind),
        "destination": str(car.destination),
        "loaded": bool(car.loaded),
        "length_m": int(car.length_m),
        "danger_class": str(car.danger_class),
        "note": str(car.note),
    }


@dataclass(slots=True)
class ManifestChange:
    kind: str
    car_code: str
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "car_code": self.car_code,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ManifestChange":
        before = raw.get("before")
        after = raw.get("after")
        return cls(
            kind=str(raw["kind"]),
            car_code=str(raw["car_code"]),
            before=None if before is None else dict(before),
            after=None if after is None else dict(after),
        )


@dataclass(slots=True)
class ManifestVersion:
    """Immutable snapshot of one intake manifest revision."""

    intake_code: str
    version: int
    recorded_at: str
    operator: str
    reason: str
    car_codes: list[str] = field(default_factory=list)
    cars: list[dict[str, object]] = field(default_factory=list)
    changes: list[ManifestChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "intake_code": self.intake_code,
            "version": self.version,
            "recorded_at": self.recorded_at,
            "operator": self.operator,
            "reason": self.reason,
            "car_codes": list(self.car_codes),
            "cars": [dict(item) for item in self.cars],
            "changes": [change.to_dict() for change in self.changes],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ManifestVersion":
        return cls(
            intake_code=str(raw["intake_code"]),
            version=int(raw["version"]),
            recorded_at=str(raw.get("recorded_at", "")),
            operator=str(raw.get("operator", "")),
            reason=str(raw.get("reason", "")),
            car_codes=[str(item) for item in raw.get("car_codes", [])],
            cars=[dict(item) for item in raw.get("cars", [])],
            changes=[ManifestChange.from_dict(dict(item)) for item in raw.get("changes", [])],
        )


def diff_manifests(
    before: list[dict[str, object]],
    after: list[dict[str, object]],
) -> list[ManifestChange]:
    """Compute added, removed, and updated car changes between two manifests."""
    changes: list[ManifestChange] = []
    before_map = {str(item["code"]): item for item in before}
    after_map = {str(item["code"]): item for item in after}
    for item in before:
        code = str(item["code"])
        if code not in after_map:
            changes.append(ManifestChange(CHANGE_REMOVED, code, before=dict(item)))
        elif _spec_changed(item, after_map[code]):
            changes.append(ManifestChange(CHANGE_UPDATED, code, before=dict(item), after=dict(after_map[code])))
    for item in after:
        code = str(item["code"])
        if code not in before_map:
            changes.append(ManifestChange(CHANGE_ADDED, code, after=dict(item)))
    return changes


def _spec_changed(before: dict[str, object], after: dict[str, object]) -> bool:
    keys = ("kind", "destination", "loaded", "length_m", "danger_class", "note")
    return any(before.get(key) != after.get(key) for key in keys)


__all__ = [
    "CHANGE_ADDED",
    "CHANGE_REMOVED",
    "CHANGE_UPDATED",
    "ManifestChange",
    "ManifestVersion",
    "car_spec_dict",
    "diff_manifests",
]
