"""Pull run, move step, and yard event records."""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import EventKind, MoveVerb, RunState
from .timeutil import now_iso


@dataclass(slots=True)
class MoveStep:
    verb: MoveVerb
    car_code: str
    source_code: str
    target_code: str

    def to_dict(self) -> dict[str, str]:
        return {
            "verb": str(self.verb),
            "car_code": self.car_code,
            "source_code": self.source_code,
            "target_code": self.target_code,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> "MoveStep":
        return cls(
            verb=MoveVerb.parse(str(raw["verb"])),
            car_code=str(raw["car_code"]),
            source_code=str(raw["source_code"]),
            target_code=str(raw["target_code"]),
        )


@dataclass(slots=True)
class PullRun:
    code: str
    outbound_code: str
    transfer_code: str
    steps: list[MoveStep] = field(default_factory=list)
    state: RunState = RunState.QUEUED
    current_step: int = 0
    required_cars: int = 0
    transfer_capacity_cars: int = 0
    transfer_available_cars: int = 0
    transfer_mode: str = "EXPLICIT"
    selection_reason: str = ""
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "outbound_code": self.outbound_code,
            "transfer_code": self.transfer_code,
            "steps": [step.to_dict() for step in self.steps],
            "state": str(self.state),
            "current_step": self.current_step,
            "required_cars": self.required_cars,
            "transfer_capacity_cars": self.transfer_capacity_cars,
            "transfer_available_cars": self.transfer_available_cars,
            "transfer_mode": self.transfer_mode,
            "selection_reason": self.selection_reason,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "PullRun":
        steps = [MoveStep.from_dict(dict(item)) for item in raw.get("steps", [])]
        # Runs persisted before transfer selection existed always named X1
        # explicitly, so legacy records decode with explicit mode defaults.
        mode = str(raw.get("transfer_mode", "EXPLICIT")).upper()
        if mode not in {"AUTO", "EXPLICIT"}:
            mode = "EXPLICIT"
        return cls(
            code=str(raw["code"]),
            outbound_code=str(raw["outbound_code"]),
            transfer_code=str(raw["transfer_code"]),
            steps=steps,
            state=RunState.parse(str(raw.get("state", RunState.QUEUED.value))),
            current_step=int(raw.get("current_step", 0)),
            required_cars=int(raw.get("required_cars", 0)),
            transfer_capacity_cars=int(raw.get("transfer_capacity_cars", 0)),
            transfer_available_cars=int(raw.get("transfer_available_cars", 0)),
            transfer_mode=mode,
            selection_reason=str(raw.get("selection_reason", "")),
            created_at=str(raw.get("created_at", "")),
            started_at=None if raw.get("started_at") is None else str(raw["started_at"]),
            completed_at=None if raw.get("completed_at") is None else str(raw["completed_at"]),
            error=None if raw.get("error") is None else str(raw["error"]),
        )

    def remaining(self) -> int:
        return max(0, len(self.steps) - self.current_step)

    def active_step(self) -> MoveStep | None:
        if self.state == RunState.COMPLETED or self.current_step >= len(self.steps):
            return None
        return self.steps[self.current_step]


@dataclass(slots=True)
class YardEvent:
    sequence: int
    at: str
    shift_code: str
    kind: EventKind
    message: str
    payload: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "at": self.at,
            "shift_code": self.shift_code,
            "kind": str(self.kind),
            "message": self.message,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "YardEvent":
        return cls(
            sequence=int(raw["sequence"]),
            at=str(raw["at"]),
            shift_code=str(raw["shift_code"]),
            kind=EventKind.parse(str(raw["kind"])),
            message=str(raw["message"]),
            payload={str(key): value for key, value in dict(raw.get("payload", {})).items()},
        )


__all__ = ["MoveStep", "PullRun", "YardEvent"]
