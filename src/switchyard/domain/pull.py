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
    attempt: int = 1
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "outbound_code": self.outbound_code,
            "transfer_code": self.transfer_code,
            "steps": [step.to_dict() for step in self.steps],
            "state": str(self.state),
            "current_step": self.current_step,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "failed_at": self.failed_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "PullRun":
        steps = [MoveStep.from_dict(dict(item)) for item in raw.get("steps", [])]
        return cls(
            code=str(raw["code"]),
            outbound_code=str(raw["outbound_code"]),
            transfer_code=str(raw["transfer_code"]),
            steps=steps,
            state=RunState.parse(str(raw.get("state", RunState.QUEUED.value))),
            current_step=int(raw.get("current_step", 0)),
            attempt=int(raw.get("attempt", 1)),
            created_at=str(raw.get("created_at", "")),
            started_at=None if raw.get("started_at") is None else str(raw["started_at"]),
            completed_at=None if raw.get("completed_at") is None else str(raw["completed_at"]),
            failed_at=None if raw.get("failed_at") is None else str(raw["failed_at"]),
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
