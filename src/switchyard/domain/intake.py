"""Inbound train entity."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import IntakeState


@dataclass(slots=True)
class IntakeTrain:
    code: str
    route: str
    arrival_at: str
    consist: list[str] = field(default_factory=list)
    state: IntakeState = IntakeState.OPEN
    unplaced: list[str] = field(default_factory=list)
    placed_at: str | None = None
    note: str = ""
    batch_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "route": self.route,
            "arrival_at": self.arrival_at,
            "consist": list(self.consist),
            "state": str(self.state),
            "unplaced": list(self.unplaced),
            "placed_at": self.placed_at,
            "note": self.note,
            "batch_code": self.batch_code,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "IntakeTrain":
        return cls(
            code=str(raw["code"]),
            route=str(raw["route"]),
            arrival_at=str(raw["arrival_at"]),
            consist=[str(item) for item in raw.get("consist", [])],
            state=IntakeState.parse(str(raw.get("state", IntakeState.OPEN.value))),
            unplaced=[str(item) for item in raw.get("unplaced", [])],
            placed_at=None if raw.get("placed_at") is None else str(raw["placed_at"]),
            note=str(raw.get("note", "")),
            batch_code=None if raw.get("batch_code") is None else str(raw["batch_code"]),
        )

    def is_terminal(self) -> bool:
        return self.state in {IntakeState.CLASSIFIED, IntakeState.CANCELLED}


__all__ = ["IntakeTrain"]
