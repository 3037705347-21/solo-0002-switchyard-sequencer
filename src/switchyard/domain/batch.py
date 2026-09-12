"""Whole-batch inbound train planning and batch provenance records.

A batch file can contain several intake trains.  Nothing is written to the
yard until every structural, per-car, in-batch, and in-yard rule passes; the
issue collector below keeps the per-train/per-car locators that the dispatcher
needs to fix a rejected file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .car import CarInput
from .enums import IntakeState
from .errors import ValidationError
from .intake import IntakeTrain
from .rules import MAX_TRAIN_CONSIST, is_car_code, is_entity_code
from .timeutil import now_iso, normalize_iso
from .validators import parse_car_input

MAX_BATCH_TRAINS = 20
BATCH_CODE_PREFIX = "BATCH"

# Machine-readable issue codes surfaced in batch reports.
ISSUE_NOT_OBJECT = "NOT_AN_OBJECT"
ISSUE_REQUIRED = "REQUIRED"
ISSUE_INVALID_CODE = "INVALID_CODE"
ISSUE_INVALID_TIME = "INVALID_TIME"
ISSUE_TOO_LONG = "TOO_LONG"
ISSUE_CARS_NOT_LIST = "CARS_NOT_A_LIST"
ISSUE_EMPTY_CONSIST = "EMPTY_CONSIST"
ISSUE_TOO_MANY_CARS = "TOO_MANY_CARS"
ISSUE_DUPLICATE_CAR = "DUPLICATE_CAR_IN_BATCH"
ISSUE_DUPLICATE_TRAIN = "DUPLICATE_TRAIN_IN_BATCH"
ISSUE_TRAIN_EXISTS = "TRAIN_ALREADY_IN_YARD"
ISSUE_CAR_EXISTS = "CAR_ALREADY_IN_YARD"
ISSUE_BATCH_EXISTS = "BATCH_ALREADY_IMPORTED"
ISSUE_SOURCE_EXISTS = "BATCH_SOURCE_ALREADY_IMPORTED"


@dataclass(slots=True)
class BatchIssue:
    """One located problem inside a batch file."""

    scope: str  # "batch" | "train" | "car"
    locator: str  # e.g. "trains[1]", "trains[1].cars[2].length_m"
    code: str
    message: str
    train_code: str | None = None
    car_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "locator": self.locator,
            "code": self.code,
            "message": self.message,
            "train_code": self.train_code,
            "car_code": self.car_code,
        }


@dataclass(slots=True)
class BatchCarRow:
    index: int
    locator: str
    car: CarInput | None
    raw_code: str | None
    issues: list[BatchIssue] = field(default_factory=list)

    @property
    def code(self) -> str | None:
        return self.car.code if self.car is not None else (self.raw_code or None)

    @property
    def status(self) -> str:
        return "accepted" if not self.issues and self.car is not None else "rejected"

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "locator": self.locator,
            "code": self.code,
            "status": self.status,
            "car": self.car.to_dict() if self.car is not None else None,
            "issues": [item.to_dict() for item in self.issues],
        }


@dataclass(slots=True)
class BatchTrainRow:
    index: int
    locator: str
    raw_code: str | None
    train: IntakeTrain | None
    cars: list[BatchCarRow] = field(default_factory=list)
    issues: list[BatchIssue] = field(default_factory=list)

    @property
    def code(self) -> str | None:
        return self.train.code if self.train is not None else (self.raw_code or None)

    @property
    def status(self) -> str:
        if self.issues:
            return "rejected"
        if any(car.issues for car in self.cars):
            return "rejected"
        return "accepted"

    @property
    def car_inputs(self) -> list[CarInput]:
        return [row.car for row in self.cars if row.car is not None]

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "locator": self.locator,
            "code": self.code,
            "status": self.status,
            "route": self.train.route if self.train is not None else None,
            "arrival_at": self.train.arrival_at if self.train is not None else None,
            "car_count": len(self.cars),
            "accepted_car_count": sum(1 for car in self.cars if car.status == "accepted"),
            "train": self.train.to_dict() if self.train is not None else None,
            "issues": [item.to_dict() for item in self.issues],
            "cars": [car.to_dict() for car in self.cars],
        }


@dataclass(slots=True)
class BatchPlan:
    code: str
    received_at: str
    note: str
    trains: list[BatchTrainRow]
    issues: list[BatchIssue] = field(default_factory=list)
    source_sha256: str = ""

    @property
    def accepted(self) -> bool:
        return not self.issues and all(row.status == "accepted" for row in self.trains)

    @property
    def train_count(self) -> int:
        return len(self.trains)

    @property
    def car_count(self) -> int:
        return sum(len(row.cars) for row in self.trains)

    def report(self) -> dict[str, Any]:
        return {
            "batch_code": self.code,
            "received_at": self.received_at,
            "note": self.note,
            "accepted": self.accepted,
            "train_count": self.train_count,
            "car_count": self.car_count,
            "accepted_train_count": sum(1 for row in self.trains if row.status == "accepted"),
            "accepted_car_count": sum(
                1 for row in self.trains for car in row.cars if car.status == "accepted"
            ),
            "issue_count": len(self.issues),
            "issues": [item.to_dict() for item in self.issues],
            "trains": [row.to_dict() for row in self.trains],
        }


@dataclass(slots=True)
class IssueCollector:
    issues: list[BatchIssue] = field(default_factory=list)

    def add(
        self,
        scope: str,
        locator: str,
        code: str,
        message: str,
        *,
        train_code: str | None = None,
        car_code: str | None = None,
    ) -> BatchIssue:
        issue = BatchIssue(
            scope=scope,
            locator=locator,
            code=code,
            message=message,
            train_code=train_code,
            car_code=car_code,
        )
        self.issues.append(issue)
        return issue


def _validated_text(
    collector: IssueCollector,
    scope: str,
    locator: str,
    raw: Any,
    label: str,
    *,
    max_length: int = 40,
    train_code: str | None = None,
) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        collector.add(scope, locator, ISSUE_REQUIRED, f"{label} is required", train_code=train_code)
        return None
    value = raw.strip()
    if len(value) > max_length:
        collector.add(
            scope,
            locator,
            ISSUE_TOO_LONG,
            f"{label} must be at most {max_length} characters",
            train_code=train_code,
        )
    return value


def _build_train_row(index: int, raw: Any, collector: IssueCollector) -> BatchTrainRow:
    locator = f"trains[{index}]"
    if not isinstance(raw, dict):
        issue = collector.add("train", locator, ISSUE_NOT_OBJECT, "each train must be an object")
        return BatchTrainRow(
            index=index, locator=locator, raw_code=None, train=None, issues=[issue]
        )

    raw_code = raw.get("code")
    code = _validated_text(collector, "train", f"{locator}.code", raw_code, "code")
    code = code.upper() if code is not None else None
    if code is not None and not is_entity_code(code, "INT"):
        collector.add(
            "train",
            f"{locator}.code",
            ISSUE_INVALID_CODE,
            "invalid intake code; expected prefix INT-",
            train_code=code,
        )
        code = None

    route = _validated_text(
        collector, "train", f"{locator}.route", raw.get("route"), "route",
        max_length=30, train_code=code,
    )
    route = route.upper() if route is not None else None

    arrival = None
    arrival_raw = _validated_text(
        collector, "train", f"{locator}.arrival_at", raw.get("arrival_at"), "arrival_at",
        train_code=code,
    )
    if arrival_raw is not None:
        try:
            arrival = normalize_iso(arrival_raw)
        except (ValueError, TypeError):
            collector.add(
                "train",
                f"{locator}.arrival_at",
                ISSUE_INVALID_TIME,
                "arrival_at must be an ISO 8601 timestamp",
                train_code=code,
            )

    cars_raw = raw.get("cars")
    car_rows: list[BatchCarRow] = []
    if not isinstance(cars_raw, list):
        collector.add(
            "train", f"{locator}.cars", ISSUE_CARS_NOT_LIST, "cars must be a list", train_code=code
        )
    elif not cars_raw:
        collector.add(
            "train",
            f"{locator}.cars",
            ISSUE_EMPTY_CONSIST,
            "cars must not be empty",
            train_code=code,
        )
    elif len(cars_raw) > MAX_TRAIN_CONSIST:
        collector.add(
            "train",
            f"{locator}.cars",
            ISSUE_TOO_MANY_CARS,
            f"at most {MAX_TRAIN_CONSIST} cars per intake",
            train_code=code,
        )

    if isinstance(cars_raw, list):
        for car_index, car_raw in enumerate(cars_raw):
            car_locator = f"{locator}.cars[{car_index}]"
            raw_car_code = car_raw.get("code") if isinstance(car_raw, dict) else None
            raw_car_code = raw_car_code if isinstance(raw_car_code, str) and raw_car_code.strip() else None
            row = BatchCarRow(
                index=car_index,
                locator=car_locator,
                car=None,
                raw_code=raw_car_code.strip().upper() if raw_car_code else None,
            )
            if not isinstance(car_raw, dict):
                row.issues.append(
                    collector.add(
                        "car", car_locator, ISSUE_NOT_OBJECT, "each car must be an object",
                        train_code=code,
                    )
                )
            else:
                try:
                    row.car = parse_car_input(car_raw)
                except ValidationError as exc:
                    for field_name, messages in exc.fields.items():
                        for message in messages:
                            row.issues.append(
                                collector.add(
                                    "car",
                                    f"{car_locator}.{field_name}",
                                    f"CAR_{field_name.upper()}_INVALID",
                                    message,
                                    train_code=code,
                                    car_code=row.raw_code,
                                )
                            )
            car_rows.append(row)

    train = None
    if code is not None and route is not None and arrival is not None:
        consist = [car.car.code for car in car_rows if car.car is not None]
        if consist:
            train = IntakeTrain(
                code=code,
                route=route,
                arrival_at=arrival,
                consist=consist,
                state=IntakeState.OPEN,
                unplaced=[],
            )
    row = BatchTrainRow(
        index=index,
        locator=locator,
        raw_code=train.code if train else (raw_code.strip().upper() if isinstance(raw_code, str) else None),
        train=train,
        cars=car_rows,
    )
    return row


def build_batch_plan(raw: Any, *, default_received_at: str | None = None) -> BatchPlan:
    """Validate the whole batch document structure and in-batch duplicates.

    Yard-state conflicts are checked separately by
    :func:`annotate_yard_conflicts`; this function never reads files or the
    workspace.
    """
    collector = IssueCollector()
    if not isinstance(raw, dict):
        collector.add("batch", "batch", ISSUE_NOT_OBJECT, "batch document must be an object")
        return BatchPlan(
            code="",
            received_at=default_received_at or now_iso(),
            note="",
            trains=[],
            issues=collector.issues,
        )

    code = _validated_text(collector, "batch", "code", raw.get("code"), "code")
    code = code.upper() if code is not None else ""
    if code and not is_entity_code(code, BATCH_CODE_PREFIX):
        collector.add(
            "batch", "code", ISSUE_INVALID_CODE, "invalid batch code; expected prefix BATCH-"
        )
        code = ""

    received_at = default_received_at or now_iso()
    received_raw = raw.get("received_at")
    if received_raw is not None:
        text = _validated_text(collector, "batch", "received_at", received_raw, "received_at")
        if text is not None:
            try:
                received_at = normalize_iso(text)
            except (ValueError, TypeError):
                collector.add(
                    "batch",
                    "received_at",
                    ISSUE_INVALID_TIME,
                    "received_at must be an ISO 8601 timestamp",
                )

    note = ""
    note_raw = raw.get("note")
    if note_raw is not None:
        if not isinstance(note_raw, str):
            collector.add("batch", "note", ISSUE_NOT_OBJECT, "note must be a string")
        else:
            note = note_raw.strip()[:200]

    trains_raw = raw.get("trains")
    if not isinstance(trains_raw, list):
        collector.add("batch", "trains", ISSUE_CARS_NOT_LIST, "trains must be a list")
        trains_raw = []
    elif not trains_raw:
        collector.add("batch", "trains", ISSUE_EMPTY_CONSIST, "trains must not be empty")
    elif len(trains_raw) > MAX_BATCH_TRAINS:
        collector.add(
            "batch",
            "trains",
            ISSUE_TOO_MANY_CARS,
            f"at most {MAX_BATCH_TRAINS} trains per batch",
        )

    rows: list[BatchTrainRow] = []
    for index, item in enumerate(list(trains_raw)[:MAX_BATCH_TRAINS]):
        before = len(collector.issues)
        row = _build_train_row(index, item, collector)
        # Train-scoped issues added while building this row belong to it.
        for issue in collector.issues[before:]:
            if issue.scope == "train" and issue not in row.issues:
                row.issues.append(issue)
        rows.append(row)

    # In-batch duplicate train codes.
    seen_trains: dict[str, str] = {}
    for row in rows:
        train_code = row.code
        if not train_code or not is_entity_code(train_code, "INT"):
            continue
        if train_code in seen_trains:
            issue = collector.add(
                "train",
                f"{row.locator}.code",
                ISSUE_DUPLICATE_TRAIN,
                f"train {train_code} already appears at {seen_trains[train_code]}",
                train_code=train_code,
            )
            row.issues.append(issue)
        else:
            seen_trains[train_code] = row.locator

    # In-batch duplicate car codes (also catches duplicates across trains).
    seen_cars: dict[str, tuple[str, str]] = {}
    for row in rows:
        for car in row.cars:
            car_code = car.code
            if not car_code or not is_car_code(car_code):
                continue
            if car_code in seen_cars:
                first_train, first_locator = seen_cars[car_code]
                car.issues.append(
                    collector.add(
                        "car",
                        f"{car.locator}.code",
                        ISSUE_DUPLICATE_CAR,
                        f"car {car_code} already appears at {first_locator} (train {first_train})",
                        train_code=row.code,
                        car_code=car_code,
                    )
                )
            else:
                seen_cars[car_code] = (row.code or "?", car.locator)

    return BatchPlan(
        code=code,
        received_at=received_at,
        note=note,
        trains=rows,
        issues=collector.issues,
    )


def annotate_yard_conflicts(plan: BatchPlan, workspace: Any) -> list[BatchIssue]:
    """Compare an otherwise-valid plan against the persisted yard.

    Returns conflict issues (also appended to ``plan.issues``). Existing
    batches, trains, and cars are all reported so the dispatcher sees every
    clash in one pass.
    """
    conflicts: list[BatchIssue] = []

    existing_batch = workspace.batch_intakes.get(plan.code) if plan.code else None
    if existing_batch is not None:
        conflicts.append(
            BatchIssue(
                scope="batch",
                locator="code",
                code=ISSUE_BATCH_EXISTS,
                message=f"batch {plan.code} was already imported at {existing_batch.imported_at}",
            )
        )

    if plan.source_sha256:
        for previous in workspace.batch_intakes.values():
            if previous.source_sha256 == plan.source_sha256:
                conflicts.append(
                    BatchIssue(
                        scope="batch",
                        locator="source",
                        code=ISSUE_SOURCE_EXISTS,
                        message=(
                            f"identical batch content already imported as {previous.code} "
                            f"from {previous.source_path or 'inline payload'}"
                        ),
                    )
                )
                break

    for row in plan.trains:
        if row.status != "accepted" or row.train is None:
            continue
        if row.train.code in workspace.intakes:
            issue = BatchIssue(
                scope="train",
                locator=f"{row.locator}.code",
                code=ISSUE_TRAIN_EXISTS,
                message=f"intake train {row.train.code} already exists in the yard",
                train_code=row.train.code,
            )
            row.issues.append(issue)
            conflicts.append(issue)
        for car in row.cars:
            if car.status != "accepted" or car.car is None:
                continue
            if car.car.code in workspace.cars:
                issue = BatchIssue(
                    scope="car",
                    locator=f"{car.locator}.code",
                    code=ISSUE_CAR_EXISTS,
                    message=f"car {car.car.code} already exists in the yard",
                    train_code=row.code,
                    car_code=car.car.code,
                )
                car.issues.append(issue)
                conflicts.append(issue)

    plan.issues.extend(conflicts)
    return conflicts


@dataclass(slots=True)
class IntakeBatchRecord:
    """Persisted provenance for one accepted batch import."""

    code: str
    received_at: str
    imported_at: str
    shift_code: str
    source_kind: str  # "file" | "inline"
    source_path: str | None
    source_bytes: int
    source_sha256: str
    train_codes: list[str] = field(default_factory=list)
    car_codes: list[str] = field(default_factory=list)
    note: str = ""
    train_event_sequences: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "received_at": self.received_at,
            "imported_at": self.imported_at,
            "shift_code": self.shift_code,
            "source_kind": self.source_kind,
            "source_path": self.source_path,
            "source_bytes": self.source_bytes,
            "source_sha256": self.source_sha256,
            "train_codes": list(self.train_codes),
            "car_codes": list(self.car_codes),
            "note": self.note,
            "train_event_sequences": list(self.train_event_sequences),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "IntakeBatchRecord":
        return cls(
            code=str(raw["code"]),
            received_at=str(raw.get("received_at", "")),
            imported_at=str(raw.get("imported_at", "")),
            shift_code=str(raw.get("shift_code", "")),
            source_kind=str(raw.get("source_kind", "inline")),
            source_path=None if raw.get("source_path") is None else str(raw["source_path"]),
            source_bytes=int(raw.get("source_bytes", 0)),
            source_sha256=str(raw.get("source_sha256", "")),
            train_codes=[str(item) for item in raw.get("train_codes", [])],
            car_codes=[str(item) for item in raw.get("car_codes", [])],
            note=str(raw.get("note", "")),
            train_event_sequences=[int(item) for item in raw.get("train_event_sequences", [])],
        )


__all__ = [
    "BATCH_CODE_PREFIX",
    "MAX_BATCH_TRAINS",
    "BatchCarRow",
    "IntakeBatchRecord",
    "BatchIssue",
    "BatchPlan",
    "BatchTrainRow",
    "IssueCollector",
    "annotate_yard_conflicts",
    "build_batch_plan",
]
