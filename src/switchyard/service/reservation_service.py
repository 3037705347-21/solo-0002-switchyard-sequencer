"""Reservation ledger queries for duty officers."""

from __future__ import annotations

from typing import Any

from ..domain.enums import ReservationStatus
from ..domain.errors import NotFoundError, ValidationError
from ..domain.reservation import ReservationRecord
from ..domain.timeutil import duration_seconds, now_iso
from .context import YardApplication

FILTER_KEYS = ("track", "destination", "status", "car", "outbound")


def _parse_status(raw: str) -> ReservationStatus:
    try:
        return ReservationStatus.parse(raw)
    except ValueError as exc:
        raise ValidationError(
            "unknown reservation status",
            **{"status": ["expected ACTIVE, RELEASED, or FULFILLED"]},
        ) from exc


def _matches(record: ReservationRecord, filters: dict[str, str]) -> bool:
    if "track" in filters and record.track_code != filters["track"]:
        return False
    if "destination" in filters and record.destination != filters["destination"]:
        return False
    if "status" in filters and str(record.status) != filters["status"]:
        return False
    if "car" in filters and record.car_code != filters["car"]:
        return False
    if "outbound" in filters and record.outbound_code != filters["outbound"]:
        return False
    return True


def _release_condition(workspace: Any, record: ReservationRecord) -> str:
    if record.status == ReservationStatus.RELEASED:
        return f"released at {record.released_at} ({record.release_reason})"
    if record.status == ReservationStatus.FULFILLED:
        return f"fulfilled at {record.released_at} ({record.release_reason})"
    run = workspace.runs.get(record.run_code)
    run_state = str(run.state) if run is not None else "REMOVED"
    return f"held until pull run {record.run_code} ({run_state}) completes or the plan is replanned or cancelled"


def _entry(workspace: Any, record: ReservationRecord, now: str) -> dict[str, Any]:
    entry = record.to_dict()
    end = record.released_at or now
    entry["held_for_seconds"] = duration_seconds(record.frozen_at, end)
    entry["release_condition"] = _release_condition(workspace, record)
    outbound = workspace.outbounds.get(record.outbound_code)
    entry["outbound_state"] = str(outbound.state) if outbound is not None else "REMOVED"
    car = workspace.cars.get(record.car_code)
    entry["car_state"] = str(car.state) if car is not None else "REMOVED"
    return entry


def _normalize_filters(query: dict[str, str] | None) -> dict[str, str]:
    filters: dict[str, str] = {}
    for key, value in (query or {}).items():
        if key not in FILTER_KEYS:
            raise ValidationError(
                "unknown reservation filter",
                **{key: [f"expected one of {', '.join(FILTER_KEYS)}"]},
            )
        text = str(value).strip()
        if not text:
            continue
        filters[key] = text.upper()
    if "status" in filters:
        filters["status"] = str(_parse_status(filters["status"]))
    return filters


def reservation_ledger(app: YardApplication, query: dict[str, str] | None = None) -> dict[str, Any]:
    filters = _normalize_filters(query)
    workspace = app.load()
    now = now_iso()
    records = [
        record
        for record in sorted(workspace.reservations.values(), key=lambda item: item.code)
        if _matches(record, filters)
    ]
    entries = [_entry(workspace, record, now) for record in records]
    by_car: dict[str, list[str]] = {}
    by_outbound: dict[str, list[str]] = {}
    for record in records:
        by_car.setdefault(record.car_code, []).append(record.code)
        by_outbound.setdefault(record.outbound_code, []).append(record.code)
    return {
        "filters": filters,
        "total": len(entries),
        "reservations": entries,
        "by_car": by_car,
        "by_outbound": by_outbound,
    }


def reservation_view(app: YardApplication, code: str) -> dict[str, Any]:
    workspace = app.load()
    record = workspace.reservations.get(code.strip().upper())
    if record is None:
        raise NotFoundError("reservation", code)
    return {"reservation": _entry(workspace, record, now_iso())}


__all__ = ["reservation_ledger", "reservation_view"]
