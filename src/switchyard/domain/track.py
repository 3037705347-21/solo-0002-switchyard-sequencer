"""Standing track entity with capacity and LIFO stack behavior."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import TrackPurpose, TrackState


@dataclass(slots=True)
class StandingTrack:
    code: str
    purpose: TrackPurpose
    capacity_cars: int
    capacity_length_m: int
    state: TrackState = TrackState.OPERATIONAL
    destination: str | None = None
    allowed_kinds: list[str] = field(default_factory=list)
    hazard_rated: bool = False
    stack: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "purpose": str(self.purpose),
            "capacity_cars": self.capacity_cars,
            "capacity_length_m": self.capacity_length_m,
            "state": str(self.state),
            "destination": self.destination,
            "allowed_kinds": list(self.allowed_kinds),
            "hazard_rated": self.hazard_rated,
            "stack": list(self.stack),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "StandingTrack":
        destination = raw.get("destination")
        return cls(
            code=str(raw["code"]),
            purpose=TrackPurpose.parse(str(raw["purpose"])),
            capacity_cars=int(raw["capacity_cars"]),
            capacity_length_m=int(raw["capacity_length_m"]),
            state=TrackState.parse(str(raw.get("state", TrackState.OPERATIONAL.value))),
            destination=None if destination is None else str(destination),
            allowed_kinds=[str(item) for item in raw.get("allowed_kinds", [])],
            hazard_rated=bool(raw.get("hazard_rated", False)),
            stack=[str(item) for item in raw.get("stack", [])],
        )

    def used_cars(self, car_lengths: dict[str, int]) -> int:
        return len(self.stack)

    def used_length(self, car_lengths: dict[str, int]) -> int:
        return sum(car_lengths.get(code, 0) for code in self.stack)

    def top_code(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def index_from_top(self, code: str) -> int | None:
        try:
            bottom_index = self.stack.index(code)
        except ValueError:
            return None
        return len(self.stack) - 1 - bottom_index

    def can_operate(self) -> bool:
        return self.state == TrackState.OPERATIONAL


@dataclass(slots=True)
class BufferBay:
    code: str
    capacity_cars: int
    stack: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "capacity_cars": self.capacity_cars, "stack": list(self.stack)}

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "BufferBay":
        return cls(
            code=str(raw["code"]),
            capacity_cars=int(raw["capacity_cars"]),
            stack=[str(item) for item in raw.get("stack", [])],
        )

    def top_code(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def remaining(self) -> int:
        return max(0, self.capacity_cars - len(self.stack))


__all__ = ["BufferBay", "StandingTrack"]
