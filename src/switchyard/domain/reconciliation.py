"""Assembly reconciliation between the pull plan and the recorded moves.

The report is recomputed from persisted state on every call: the planned
sequence and sources come from the run's immutable pull steps, the actual
sequence comes from the outbound consist, and the append-only move log
provides the recorded source of every pulled car. Deleting a car from the
consist or editing the planned order cannot clear a discrepancy, because
neither the run steps nor the move log change through those edits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import AssemblyStatus, DiscrepancyKind, MoveVerb
from .outbound import OutboundTrain
from .pull import MoveRecord, PullRun


@dataclass(slots=True)
class Discrepancy:
    kind: DiscrepancyKind
    car_code: str
    position: int
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "car_code": self.car_code,
            "position": self.position,
            "detail": self.detail,
        }


@dataclass(slots=True)
class PositionEntry:
    """Mapping of one planned consist position to the actual car seen there."""

    position: int
    planned_car: str
    actual_car: str | None
    planned_source: str | None
    actual_source: str | None
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "position": self.position,
            "planned_car": self.planned_car,
            "actual_car": self.actual_car,
            "planned_source": self.planned_source,
            "actual_source": self.actual_source,
            "status": self.status,
        }


@dataclass(slots=True)
class AssemblyReport:
    outbound_code: str
    run_code: str
    status: AssemblyStatus
    entries: list[PositionEntry] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)
    first_deviation_position: int | None = None
    planned_count: int = 0
    assembled_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "outbound_code": self.outbound_code,
            "run_code": self.run_code,
            "status": str(self.status),
            "planned_count": self.planned_count,
            "assembled_count": self.assembled_count,
            "first_deviation_position": self.first_deviation_position,
            "positions": [entry.to_dict() for entry in self.entries],
            "discrepancies": [item.to_dict() for item in self.discrepancies],
        }


def _pulls_by_car(moves: list[MoveRecord], outbound_code: str) -> dict[str, list[MoveRecord]]:
    pulls: dict[str, list[MoveRecord]] = {}
    for move in moves:
        if move.verb == MoveVerb.PULL and move.target_code == outbound_code:
            pulls.setdefault(move.car_code, []).append(move)
    return pulls


def reconcile_assembly(outbound: OutboundTrain, run: PullRun, final: bool = False) -> AssemblyReport:
    """Compare the planned consist against the assembled one.

    ``final`` marks the point where every planned pull step should have
    happened (run steps exhausted, or a departure check); only then is an
    unpulled planned car reported as missing instead of pending.
    """
    planned_sequence: list[str] = []
    planned_source: dict[str, str] = {}
    planned_position: dict[str, int] = {}
    pull_step_index: dict[str, int] = {}
    for index, step in enumerate(run.steps):
        if step.verb != MoveVerb.PULL:
            continue
        planned_position[step.car_code] = len(planned_sequence)
        planned_sequence.append(step.car_code)
        planned_source[step.car_code] = step.source_code
        pull_step_index[step.car_code] = index
    consist = list(outbound.assembled_car_codes)
    pulls = _pulls_by_car(run.actual_moves, outbound.code)

    discrepancies: list[Discrepancy] = []
    first_seen: dict[str, int] = {}
    for position, car_code in enumerate(consist):
        if car_code in first_seen:
            discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.DUPLICATE,
                    car_code,
                    position,
                    f"car {car_code} is assembled at positions {first_seen[car_code]} and {position}",
                )
            )
        else:
            first_seen[car_code] = position
    for position, car_code in enumerate(consist):
        if car_code not in planned_position:
            discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.SOURCE,
                    car_code,
                    position,
                    f"car {car_code} is not part of the pull plan",
                )
            )
            continue
        records = pulls.get(car_code, [])
        if not records:
            discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.SOURCE,
                    car_code,
                    position,
                    f"car {car_code} has no recorded pull onto {outbound.code}",
                )
            )
            continue
        for record in records:
            if record.source_code != planned_source[car_code]:
                discrepancies.append(
                    Discrepancy(
                        DiscrepancyKind.SOURCE,
                        car_code,
                        position,
                        f"car {car_code} was pulled from {record.source_code}, "
                        f"planned source is {planned_source[car_code]}",
                    )
                )
    for position in range(min(len(consist), len(planned_sequence))):
        actual = consist[position]
        planned = planned_sequence[position]
        if actual != planned and actual in planned_position:
            discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.SEQUENCE,
                    actual,
                    position,
                    f"car {actual} is assembled at position {position}, "
                    f"planned at position {planned_position[actual]}",
                )
            )
    for position, car_code in enumerate(planned_sequence):
        if car_code in consist:
            continue
        due = final or car_code in pulls or pull_step_index[car_code] < run.current_step
        if due:
            discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.MISSING,
                    car_code,
                    position,
                    f"planned car {car_code} is missing from the assembled consist",
                )
            )

    entries: list[PositionEntry] = []
    missing_cars = {item.car_code for item in discrepancies if item.kind == DiscrepancyKind.MISSING}
    for position, planned_car in enumerate(planned_sequence):
        actual_car = consist[position] if position < len(consist) else None
        actual_source = None
        if actual_car is not None and pulls.get(actual_car):
            actual_source = pulls[actual_car][-1].source_code
        if actual_car is None:
            status = "MISSING" if planned_car in missing_cars else "PENDING"
        elif actual_car == planned_car:
            status = "MATCH"
        else:
            status = "WRONG_CAR"
        entries.append(
            PositionEntry(
                position=position,
                planned_car=planned_car,
                actual_car=actual_car,
                planned_source=planned_source.get(planned_car),
                actual_source=actual_source,
                status=status,
            )
        )

    if discrepancies:
        status = AssemblyStatus.DIVERGED
    elif consist == planned_sequence:
        status = AssemblyStatus.ALIGNED
    else:
        status = AssemblyStatus.PENDING
    first_deviation = min((item.position for item in discrepancies), default=None)
    return AssemblyReport(
        outbound_code=outbound.code,
        run_code=run.code,
        status=status,
        entries=entries,
        discrepancies=discrepancies,
        first_deviation_position=first_deviation,
        planned_count=len(planned_sequence),
        assembled_count=len(consist),
    )


__all__ = [
    "AssemblyReport",
    "Discrepancy",
    "PositionEntry",
    "reconcile_assembly",
]
