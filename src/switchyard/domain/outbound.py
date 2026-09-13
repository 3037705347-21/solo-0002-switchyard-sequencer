"""Outbound train entity."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import OutboundState


@dataclass(slots=True)
class OutboundTrain:
    code: str
    destination: str
    planned_car_codes: list[str] = field(default_factory=list)
    assembled_car_codes: list[str] = field(default_factory=list)
    state: OutboundState = OutboundState.DRAFT
    run_codes: list[str] = field(default_factory=list)
    manifest_codes: list[str] = field(default_factory=list)
    created_at: str = ""
    departed_at: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "destination": self.destination,
            "planned_car_codes": list(self.planned_car_codes),
            "assembled_car_codes": list(self.assembled_car_codes),
            "state": str(self.state),
            "run_codes": list(self.run_codes),
            "manifest_codes": list(self.manifest_codes),
            "created_at": self.created_at,
            "departed_at": self.departed_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "OutboundTrain":
        return cls(
            code=str(raw["code"]),
            destination=str(raw["destination"]),
            planned_car_codes=[str(item) for item in raw.get("planned_car_codes", [])],
            assembled_car_codes=[str(item) for item in raw.get("assembled_car_codes", [])],
            state=OutboundState.parse(str(raw.get("state", OutboundState.DRAFT.value))),
            run_codes=[str(item) for item in raw.get("run_codes", [])],
            manifest_codes=[str(item) for item in raw.get("manifest_codes", [])],
            created_at=str(raw.get("created_at", "")),
            departed_at=None if raw.get("departed_at") is None else str(raw["departed_at"]),
            note=str(raw.get("note", "")),
        )

    def assembly_complete(self) -> bool:
        return self.assembled_car_codes == self.planned_car_codes


__all__ = ["OutboundTrain"]
