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
    manifest_version: int = 1
    placed_at: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "route": self.route,
            "arrival_at": self.arrival_at,
            "consist": list(self.consist),
            "state": str(self.state),
            "unplaced": list(self.unplaced),
            "manifest_version": self.manifest_version,
            "placed_at": self.placed_at,
            "note": self.note,
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
            manifest_version=int(raw.get("manifest_version", 1)),
            placed_at=None if raw.get("placed_at") is None else str(raw["placed_at"]),
            note=str(raw.get("note", "")),
        )

    def is_terminal(self) -> bool:
        return self.state in {IntakeState.CLASSIFIED, IntakeState.CANCELLED}


__all__ = ["IntakeTrain"]
