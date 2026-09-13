"""Stable enumeration values used by the yard domain."""

from __future__ import annotations

from enum import Enum
from typing import Any


class EnumValue(str, Enum):
    """String enum that serializes as its value."""

    @classmethod
    def parse(cls, value: Any) -> "EnumValue":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).upper())
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(f"invalid {cls.__name__} value {value!r}; expected {choices}") from exc

    def __str__(self) -> str:
        return str(self.value)


class CarKind(EnumValue):
    BOX = "BOX"
    HOPPER = "HOPPER"
    FLAT = "FLAT"
    TANK = "TANK"
    REEFER = "REEFER"


class CarState(EnumValue):
    RECEIVED = "RECEIVED"
    STANDING = "STANDING"
    RESERVED = "RESERVED"
    ASSEMBLED = "ASSEMBLED"
    DEPARTED = "DEPARTED"
    REMOVED = "REMOVED"


class TrackPurpose(EnumValue):
    DESTINATION = "DESTINATION"
    GENERAL = "GENERAL"
    TRANSFER = "TRANSFER"


class TrackState(EnumValue):
    OPERATIONAL = "OPERATIONAL"
    RESTRICTED = "RESTRICTED"
    MAINTENANCE = "MAINTENANCE"


class IntakeState(EnumValue):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    CLASSIFIED = "CLASSIFIED"
    CANCELLED = "CANCELLED"


class OutboundState(EnumValue):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    READY = "READY"
    DEPARTED = "DEPARTED"
    ABANDONED = "ABANDONED"


class RunState(EnumValue):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ShiftState(EnumValue):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class MoveVerb(EnumValue):
    BUFFER = "BUFFER"
    RETURN = "RETURN"
    PULL = "PULL"


class EventKind(EnumValue):
    SHIFT_OPENED = "SHIFT_OPENED"
    TRAIN_RECEIVED = "TRAIN_RECEIVED"
    TRAIN_CLASSIFIED = "TRAIN_CLASSIFIED"
    TRAIN_CREATED = "TRAIN_CREATED"
    PULL_PLANNED = "PULL_PLANNED"
    PULL_RUN_STARTED = "PULL_RUN_STARTED"
    PULL_RUN_ADVANCED = "PULL_RUN_ADVANCED"
    PULL_RUN_COMPLETED = "PULL_RUN_COMPLETED"
    PULL_RUN_CANCELLED = "PULL_RUN_CANCELLED"
    TRANSFER_REGISTERED = "TRANSFER_REGISTERED"
    TRANSFER_STATE_CHANGED = "TRANSFER_STATE_CHANGED"
    TRANSFER_RECONCILED = "TRANSFER_RECONCILED"
    TRAIN_DEPARTED = "TRAIN_DEPARTED"
    SHIFT_CLOSED = "SHIFT_CLOSED"
    CLOSURE_BLOCKED = "CLOSURE_BLOCKED"
    YARD_VIEWED = "YARD_VIEWED"


__all__ = [
    "CarKind",
    "CarState",
    "EnumValue",
    "EventKind",
    "IntakeState",
    "MoveVerb",
    "OutboundState",
    "RunState",
    "ShiftState",
    "TrackPurpose",
    "TrackState",
]
