"""Car deactivation record (vehicle withdrawal / hold) entity."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import DeactivationKind, DeactivationStatus


@dataclass(slots=True)
class CarDeactivation:
    code: str
    car_code: str
    kind: DeactivationKind
    reason: str
    operator: str
    status: DeactivationStatus = DeactivationStatus.ACTIVE
    requested_at: str = ""
    prior_state: str = ""
    prior_location: str | None = None
    restore_index: int | None = None
    blocked_attempts: int = 0
    recovered_at: str | None = None
    recovery_reason: str | None = None
    recovery_operator: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "car_code": self.car_code,
            "kind": str(self.kind),
            "reason": self.reason,
            "operator": self.operator,
            "status": str(self.status),
            "requested_at": self.requested_at,
            "prior_state": self.prior_state,
            "prior_location": self.prior_location,
            "restore_index": self.restore_index,
            "blocked_attempts": self.blocked_attempts,
            "recovered_at": self.recovered_at,
            "recovery_reason": self.recovery_reason,
            "recovery_operator": self.recovery_operator,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "CarDeactivation":
        return cls(
            code=str(raw["code"]),
            car_code=str(raw["car_code"]),
            kind=DeactivationKind.parse(str(raw.get("kind", DeactivationKind.HOLD.value))),
            reason=str(raw.get("reason", "")),
            operator=str(raw.get("operator", "")),
            status=DeactivationStatus.parse(str(raw.get("status", DeactivationStatus.ACTIVE.value))),
            requested_at=str(raw.get("requested_at", "")),
            prior_state=str(raw.get("prior_state", "")),
            prior_location=None if raw.get("prior_location") is None else str(raw["prior_location"]),
            restore_index=None if raw.get("restore_index") is None else int(raw["restore_index"]),
            blocked_attempts=int(raw.get("blocked_attempts", 0)),
            recovered_at=None if raw.get("recovered_at") is None else str(raw["recovered_at"]),
            recovery_reason=None if raw.get("recovery_reason") is None else str(raw["recovery_reason"]),
            recovery_operator=None
            if raw.get("recovery_operator") is None
            else str(raw["recovery_operator"]),
        )


__all__ = ["CarDeactivation"]
