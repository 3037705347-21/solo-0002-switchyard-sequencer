"""Yard shift entity."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import ShiftState


@dataclass(slots=True)
class YardShift:
    code: str
    dispatcher: str
    opened_at: str
    state: ShiftState = ShiftState.OPEN
    closed_at: str | None = None
    closure_snapshot_code: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "dispatcher": self.dispatcher,
            "opened_at": self.opened_at,
            "state": str(self.state),
            "closed_at": self.closed_at,
            "closure_snapshot_code": self.closure_snapshot_code,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "YardShift":
        return cls(
            code=str(raw["code"]),
            dispatcher=str(raw["dispatcher"]),
            opened_at=str(raw["opened_at"]),
            state=ShiftState.parse(str(raw.get("state", ShiftState.OPEN.value))),
            closed_at=None if raw.get("closed_at") is None else str(raw["closed_at"]),
            closure_snapshot_code=None
            if raw.get("closure_snapshot_code") is None
            else str(raw["closure_snapshot_code"]),
            note=str(raw.get("note", "")),
        )


__all__ = ["YardShift"]
