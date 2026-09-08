"""UTC timestamp helpers shared by services and events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FORMAT)


def parse_iso(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_iso(value: str) -> str:
    return parse_iso(value).strftime(UTC_FORMAT)


def iso_plus_seconds(value: str, seconds: int) -> str:
    return (parse_iso(value) + timedelta(seconds=seconds)).strftime(UTC_FORMAT)


def duration_seconds(start_iso: str, end_iso: str) -> int:
    return int((parse_iso(end_iso) - parse_iso(start_iso)).total_seconds())


def is_after_or_equal(left: str, right: str) -> bool:
    return parse_iso(left) >= parse_iso(right)


__all__ = [
    "UTC_FORMAT",
    "duration_seconds",
    "is_after_or_equal",
    "iso_plus_seconds",
    "normalize_iso",
    "now_iso",
    "parse_iso",
]
