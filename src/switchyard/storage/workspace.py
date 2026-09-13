"""In-memory workspace root used by all service commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import EventKind
from ..domain.pull import YardEvent
from ..domain.timeutil import now_iso
from ..domain.track import BufferBay, StandingTrack

SCHEMA_VERSION = 1


@dataclass(slots=True)
class YardWorkspace:
    schema_version: int = SCHEMA_VERSION
    version: int = 1
    next_event_sequence: int = 1
    tracks: dict[str, StandingTrack] = field(default_factory=dict)
    buffer_bays: dict[str, BufferBay] = field(default_factory=dict)
    cars: dict[str, Any] = field(default_factory=dict)
    intakes: dict[str, Any] = field(default_factory=dict)
    outbounds: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, Any] = field(default_factory=dict)
    reorders: dict[str, Any] = field(default_factory=dict)
    shifts: dict[str, Any] = field(default_factory=dict)
    events: list[YardEvent] = field(default_factory=list)
    closure_snapshots: list[dict[str, Any]] = field(default_factory=list)

    def bump(self) -> None:
        self.version += 1

    def record_event(
        self,
        shift_code: str,
        kind: EventKind,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> YardEvent:
        event = YardEvent(
            sequence=self.next_event_sequence,
            at=now_iso(),
            shift_code=shift_code,
            kind=kind,
            message=message,
            payload=payload or {},
        )
        self.events.append(event)
        self.next_event_sequence += 1
        self.bump()
        return event

    def track(self, code: str) -> StandingTrack:
        return self.tracks[code]

    def bay(self, code: str) -> BufferBay:
        return self.buffer_bays[code]


__all__ = ["SCHEMA_VERSION", "YardWorkspace"]
