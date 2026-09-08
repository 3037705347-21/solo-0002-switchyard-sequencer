"""Typed domain failures with stable HTTP mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DomainError(Exception):
    """Base exception carried through service boundaries."""

    message: str
    code: str = "DOMAIN_ERROR"
    status: int = 400
    fields: dict[str, list[str]] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "fields": self.fields,
            "details": self.payload,
        }


class ValidationError(DomainError):
    """One or more payload or state validation problems."""

    def __init__(self, message: str, fields: dict[str, list[str]] | None = None, **payload: Any):
        super().__init__(message, code="VALIDATION_ERROR", status=422, fields=fields or {}, payload=payload)


class NotFoundError(DomainError):
    """The requested code or resource does not exist."""

    def __init__(self, resource: str, code: str):
        super().__init__(
            f"{resource} {code!r} was not found",
            code="NOT_FOUND",
            status=404,
            payload={"resource": resource, "code": code},
        )


class ConflictError(DomainError):
    """A uniqueness or existing-resource conflict."""

    def __init__(self, message: str, **payload: Any):
        super().__init__(message, code="CONFLICT", status=409, payload=payload)


class StateTransitionError(DomainError):
    """A transition was rejected by the state table."""

    def __init__(self, entity: str, current: str, target: str, reason: str | None = None):
        message = f"{entity} cannot move from {current} to {target}"
        if reason:
            message += f": {reason}"
        super().__init__(
            message,
            code="STATE_TRANSITION",
            status=409,
            payload={"entity": entity, "current": current, "target": target, "reason": reason},
        )


class ResourceBusyError(DomainError):
    """An active resource prevents another lifecycle operation."""

    def __init__(self, message: str, **payload: Any):
        super().__init__(message, code="RESOURCE_BUSY", status=409, payload=payload)


__all__ = [
    "ConflictError",
    "DomainError",
    "NotFoundError",
    "ResourceBusyError",
    "StateTransitionError",
    "ValidationError",
]
