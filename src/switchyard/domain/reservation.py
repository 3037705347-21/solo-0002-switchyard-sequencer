"""Reservation ledger records and lifecycle helpers.

A reservation record freezes the link between a planned car and the outbound
train that claimed it. Records are created when a pull plan is generated,
fulfilled when the car is pulled onto the outbound consist, and released when
the plan is cancelled or replanned. Released and fulfilled records stay in the
ledger so duty officers can trace historical occupancy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import ReservationStatus
from .pull import MoveStep


@dataclass(slots=True)
class ReservationRecord:
    code: str
    car_code: str
    outbound_code: str
    run_code: str
    destination: str
    track_code: str
    frozen_at: str
    status: ReservationStatus = ReservationStatus.ACTIVE
    released_at: str | None = None
    release_reason: str | None = None
    actions: list[MoveStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "car_code": self.car_code,
            "outbound_code": self.outbound_code,
            "run_code": self.run_code,
            "destination": self.destination,
            "track_code": self.track_code,
            "frozen_at": self.frozen_at,
            "status": str(self.status),
            "released_at": self.released_at,
            "release_reason": self.release_reason,
            "actions": [step.to_dict() for step in self.actions],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "ReservationRecord":
        return cls(
            code=str(raw["code"]),
            car_code=str(raw["car_code"]),
            outbound_code=str(raw["outbound_code"]),
            run_code=str(raw["run_code"]),
            destination=str(raw["destination"]),
            track_code=str(raw["track_code"]),
            frozen_at=str(raw["frozen_at"]),
            status=ReservationStatus.parse(str(raw.get("status", ReservationStatus.ACTIVE.value))),
            released_at=None if raw.get("released_at") is None else str(raw["released_at"]),
            release_reason=None if raw.get("release_reason") is None else str(raw["release_reason"]),
            actions=[MoveStep.from_dict(dict(item)) for item in raw.get("actions", [])],
        )


def active_for_car(reservations: dict[str, ReservationRecord], car_code: str) -> ReservationRecord | None:
    for record in reservations.values():
        if record.car_code == car_code and record.status == ReservationStatus.ACTIVE:
            return record
    return None


def active_for_outbound(reservations: dict[str, ReservationRecord], outbound_code: str) -> list[ReservationRecord]:
    return [
        record
        for record in reservations.values()
        if record.outbound_code == outbound_code and record.status == ReservationStatus.ACTIVE
    ]


def release_active(
    reservations: dict[str, ReservationRecord],
    outbound_code: str,
    reason: str,
    released_at: str,
) -> list[ReservationRecord]:
    released: list[ReservationRecord] = []
    for record in active_for_outbound(reservations, outbound_code):
        record.status = ReservationStatus.RELEASED
        record.released_at = released_at
        record.release_reason = reason
        released.append(record)
    return released


def fulfill_for_car(
    reservations: dict[str, ReservationRecord],
    outbound_code: str,
    car_code: str,
    reason: str,
    fulfilled_at: str,
) -> ReservationRecord | None:
    for record in active_for_outbound(reservations, outbound_code):
        if record.car_code != car_code:
            continue
        record.status = ReservationStatus.FULFILLED
        record.released_at = fulfilled_at
        record.release_reason = reason
        return record
    return None


__all__ = [
    "ReservationRecord",
    "active_for_car",
    "active_for_outbound",
    "fulfill_for_car",
    "release_active",
]
