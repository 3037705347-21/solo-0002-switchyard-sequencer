"""Freight car entity."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import CarKind, CarState


@dataclass(slots=True)
class FreightCar:
    code: str
    kind: CarKind
    destination: str
    loaded: bool
    length_m: int
    danger_class: str = "NONE"
    state: CarState = CarState.RECEIVED
    location: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "kind": str(self.kind),
            "destination": self.destination,
            "loaded": self.loaded,
            "length_m": self.length_m,
            "danger_class": self.danger_class,
            "state": str(self.state),
            "location": self.location,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "FreightCar":
        return cls(
            code=str(raw["code"]),
            kind=CarKind.parse(str(raw["kind"])),
            destination=str(raw["destination"]),
            loaded=bool(raw["loaded"]),
            length_m=int(raw["length_m"]),
            danger_class=str(raw.get("danger_class", "NONE")),
            state=CarState.parse(str(raw.get("state", CarState.RECEIVED.value))),
            location=None if raw.get("location") is None else str(raw["location"]),
            note=str(raw.get("note", "")),
        )

    def is_hazardous(self) -> bool:
        return self.danger_class.upper() != "NONE"

    def copy(self) -> "FreightCar":
        return FreightCar(
            code=self.code,
            kind=self.kind,
            destination=self.destination,
            loaded=self.loaded,
            length_m=self.length_m,
            danger_class=self.danger_class,
            state=self.state,
            location=self.location,
            note=self.note,
        )


@dataclass(slots=True)
class CarInput:
    """Mutable payload before a FreightCar is created."""

    code: str
    kind: str
    destination: str
    loaded: bool
    length_m: int
    danger_class: str = "NONE"
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "kind": self.kind,
            "destination": self.destination,
            "loaded": self.loaded,
            "length_m": self.length_m,
            "danger_class": self.danger_class,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "CarInput":
        return cls(
            code=str(raw.get("code", "")).strip().upper(),
            kind=str(raw.get("kind", "")).strip().upper(),
            destination=str(raw.get("destination", "")).strip().upper(),
            loaded=bool(raw.get("loaded", False)),
            length_m=int(raw.get("length_m", 0)),
            danger_class=str(raw.get("danger_class", "NONE")).strip().upper(),
            note=str(raw.get("note", "")).strip(),
        )


__all__ = ["CarInput", "FreightCar"]
