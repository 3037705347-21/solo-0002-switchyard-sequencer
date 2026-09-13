"""Filtering and append-stable keyset pagination for shift events.

Pagination is anchored to a fixed "high water mark" event sequence. Events that
are appended to the journal *while a caller is paging* carry larger sequences
than the anchor and are therefore invisible to that walk: they can never push
older events off a page (which would skip them) and they can never appear on a
later page of the same walk (which would duplicate them). Callers restart
without a cursor to adopt the new high water mark.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import EventKind
from ..domain.errors import ValidationError
from ..domain.timeutil import parse_iso
from ..report.event_query import referenced_codes

DEFAULT_PAGE_LIMIT = 40
MAX_PAGE_LIMIT = 200

VALID_EVENT_KINDS = frozenset(item.value for item in EventKind)


@dataclass(slots=True)
class EventFilter:
    kinds: frozenset[str] = frozenset()
    start_at: str | None = None
    end_at: str | None = None
    object_code: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def fingerprint_inputs(self) -> list[str]:
        """Normalized filter dimensions that must stay constant across pages."""

        return [
            ",".join(sorted(self.kinds)),
            self.start_at or "",
            self.end_at or "",
            self.object_code or "",
        ]


@dataclass(slots=True)
class EventPage:
    events: list[Any]
    total: int
    anchor: int
    limit: int
    next_cursor: str | None = None
    new_event_count: int = 0
    live_total: int = 0
    filter_echo: dict[str, Any] = field(default_factory=dict)


def _first_value(params: Any, key: str) -> str | None:
    if not isinstance(params, dict):
        return None
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _repeated_values(params: Any, key: str) -> list[str]:
    if not isinstance(params, dict):
        return []
    value = params.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)]


def _parse_kinds(raw: str) -> frozenset[str]:
    kinds: set[str] = set()
    for item in raw.split(","):
        token = item.strip().upper()
        if not token:
            continue
        if token not in VALID_EVENT_KINDS:
            choices = ", ".join(sorted(VALID_EVENT_KINDS))
            raise ValidationError(
                f"unknown event kind {token!r}",
                fields={"kind": [f"expected one of {choices}"]},
            )
        kinds.add(token)
    return frozenset(kinds)


def parse_event_filter(params: Any) -> EventFilter:
    kinds: set[str] = set()
    for raw in _repeated_values(params, "kind"):
        kinds.update(_parse_kinds(raw))

    start_at = _first_value(params, "start_at")
    if start_at:
        start_at = parse_iso(start_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_at = _first_value(params, "end_at")
    if end_at:
        end_at = parse_iso(end_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    if start_at and end_at and parse_iso(start_at) > parse_iso(end_at):
        raise ValidationError(
            "start_at must not be later than end_at",
            fields={"time_range": ["start_at is after end_at"]},
        )

    object_code = _first_value(params, "object_code")
    if object_code:
        object_code = object_code.strip().upper()
        if not object_code:
            raise ValidationError("object_code must not be empty", fields={"object_code": ["is required"]})

    limit = DEFAULT_PAGE_LIMIT
    raw_limit = _first_value(params, "limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise ValidationError(
                "limit must be an integer", fields={"limit": [f"must be between 1 and {MAX_PAGE_LIMIT}"]}
            ) from exc
        if limit < 1 or limit > MAX_PAGE_LIMIT:
            raise ValidationError(
                "limit is out of range", fields={"limit": [f"must be between 1 and {MAX_PAGE_LIMIT}"]}
            )

    return EventFilter(
        kinds=frozenset(kinds),
        start_at=start_at,
        end_at=end_at,
        object_code=object_code,
        limit=limit,
    )


def has_constraints(params: Any) -> bool:
    """Whether any paging/filter parameter was supplied (extended response)."""

    if not isinstance(params, dict):
        return False
    return any(
        key in params
        for key in ("kind", "start_at", "end_at", "object_code", "limit", "cursor")
    )


def cursor_from_params(params: Any) -> str | None:
    if not isinstance(params, dict):
        return None
    raw_cursor = params.get("cursor")
    if isinstance(raw_cursor, list):
        return str(raw_cursor[0]) if raw_cursor else None
    if raw_cursor is not None:
        return str(raw_cursor)
    return None


def _cursor_token(anchor: int, after: int, event_filter: EventFilter, scope: str) -> str:
    fingerprint = hashlib.sha256(
        "\x1f".join([scope, *event_filter.fingerprint_inputs()]).encode("utf-8")
    ).hexdigest()[:16]
    raw = json.dumps(
        {"a": anchor, "f": after, "h": fingerprint},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(token: str, event_filter: EventFilter, scope: str) -> tuple[int, int]:
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        value = json.loads(raw.decode("utf-8"))
        anchor = int(value["a"])
        after = int(value["f"])
        fingerprint = str(value["h"])
    except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cursor is not valid", fields={"cursor": ["could not be decoded"]}) from exc
    expected = hashlib.sha256(
        "\x1f".join([scope, *event_filter.fingerprint_inputs()]).encode("utf-8")
    ).hexdigest()[:16]
    if fingerprint != expected:
        raise ValidationError(
            "cursor does not match the supplied filters",
            fields={"cursor": ["restart paging with the current kind/time/object filters"]},
        )
    if after > anchor or anchor < 1 or after < 0:
        raise ValidationError("cursor is out of range", fields={"cursor": ["anchor/after mismatch"]})
    return anchor, after


def _matches(event: Any, event_filter: EventFilter) -> bool:
    if event_filter.kinds and str(event.kind) not in event_filter.kinds:
        return False
    if event_filter.start_at and event.at < event_filter.start_at:
        return False
    if event_filter.end_at and event.at > event_filter.end_at:
        return False
    if event_filter.object_code:
        # object_code is case-insensitive normalized upper-case; stored codes
        # are upper-case already.
        if event_filter.object_code not in referenced_codes(event):
            return False
    return True


def query_events(
    workspace: Any,
    shift_code: str,
    event_filter: EventFilter,
    cursor_token: str | None = None,
) -> EventPage:
    """Return one stable page of matching events, oldest first.

    The first request fixes the anchor to the highest event sequence visible
    for the shift. Every subsequent page only considers sequences at or below
    that anchor, so concurrent appends cannot cause duplicates or skips.
    """

    shift_events = [event for event in workspace.events if event.shift_code == shift_code]
    live_total = len(shift_events)
    highest = shift_events[-1].sequence if shift_events else 0

    if cursor_token:
        anchor, after = _decode_cursor(cursor_token, event_filter, f"shift:{shift_code}")
        if highest < anchor:
            # Sequences only move forward, so a larger anchor cannot disappear.
            raise ValidationError("cursor anchor is ahead of the journal", fields={"cursor": ["anchor not found"]})
    else:
        anchor, after = highest, 0

    bounded = [event for event in shift_events if event.sequence <= anchor]
    matching = [event for event in bounded if _matches(event, event_filter)]
    total = len(matching)
    start_index = sum(1 for event in matching if event.sequence <= after)
    page_items = matching[start_index : start_index + event_filter.limit]

    next_token: str | None = None
    if start_index + len(page_items) < total:
        next_token = _cursor_token(
            anchor,
            page_items[-1].sequence,
            event_filter,
            f"shift:{shift_code}",
        )

    return EventPage(
        events=page_items,
        total=total,
        anchor=anchor,
        limit=event_filter.limit,
        next_cursor=next_token,
        new_event_count=max(0, live_total - len(bounded)),
        live_total=live_total,
        filter_echo={
            "kinds": sorted(event_filter.kinds),
            "start_at": event_filter.start_at,
            "end_at": event_filter.end_at,
            "object_code": event_filter.object_code,
        },
    )


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "EventFilter",
    "EventPage",
    "MAX_PAGE_LIMIT",
    "cursor_from_params",
    "has_constraints",
    "parse_event_filter",
    "query_events",
]
