"""Batch inbound train intake from local manifest files.

The batch command validates the whole document first (structure, in-batch
duplicates) and then every yard-state conflict (existing batches, trains, and
cars).  Only when the entire batch is acceptable does it mutate the
workspace; all trains, cars, events, and the provenance record are produced in
a single commit, so a failure can never leave a half-imported batch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..domain.batch import (
    IntakeBatchRecord,
    annotate_yard_conflicts,
    build_batch_plan,
)
from ..domain.enums import EventKind
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..domain.timeutil import now_iso
from .context import YardApplication
from .intake_service import car_from_input, ensure_open_shift

MAX_BATCH_FILE_BYTES = 256 * 1024


def _load_batch_source(payload: Any) -> tuple[Any, dict[str, Any]]:
    """Read the batch document from a local path or an inline payload."""
    if not isinstance(payload, dict):
        raise ValidationError(
            "batch request must be an object",
            **{"payload": ["expected an object with source_path or batch"]},
        )
    source_path = payload.get("source_path")
    inline = payload.get("batch")
    if source_path is not None and inline is not None:
        raise ValidationError(
            "provide either source_path or batch, not both",
            **{"source_path": ["mutually exclusive with batch"]},
        )
    if source_path is None and inline is None:
        raise ValidationError(
            "a batch source is required",
            **{"source_path": ["is required"], "batch": ["is required"]},
        )
    if source_path is not None:
        if not isinstance(source_path, str) or not source_path.strip():
            raise ValidationError(
                "source_path must be a path string", **{"source_path": ["is required"]}
            )
        path = Path(source_path.strip()).expanduser()
        if not path.is_file():
            raise NotFoundError("batch file", str(path))
        size = path.stat().st_size
        if size > MAX_BATCH_FILE_BYTES:
            raise ValidationError(
                "batch file is too large",
                **{"source_path": [f"must be at most {MAX_BATCH_FILE_BYTES} bytes"]},
            )
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise ValidationError(
                "batch file could not be read", **{"source_path": [str(exc)]}
            ) from exc
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                "batch file is not valid UTF-8", **{"source_path": ["invalid encoding"]}
            ) from exc
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "batch file is not valid JSON", **{"source_path": [str(exc)]}
            ) from exc
        return document, {
            "source_kind": "file",
            "source_path": str(path.resolve()),
            "source_bytes": len(raw_bytes),
            "source_sha256": content_sha256(document),
        }

    if not isinstance(inline, dict):
        raise ValidationError(
            "batch must be an object", **{"batch": ["expected a batch document"]}
        )
    return inline, {
        "source_kind": "inline",
        "source_path": None,
        "source_bytes": len(json.dumps(inline, ensure_ascii=False).encode("utf-8")),
        "source_sha256": content_sha256(inline),
    }


def content_sha256(document: Any) -> str:
    """Stable hash of batch *content* independent of on-disk formatting.

    Batch identity fields (code/received_at/note) are excluded so the same
    manifest re-submitted under a new batch code is still recognised.
    """
    if not isinstance(document, dict):
        body = document
    else:
        body = {key: value for key, value in document.items()
                if key not in {"code", "received_at", "note"}}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reject(plan: Any, *, status_code: str, message: str) -> ConflictError | ValidationError:
    report = plan.report()
    fields: dict[str, list[str]] = {}
    for issue in report["issues"]:
        fields.setdefault(str(issue["locator"]), []).append(str(issue["message"]))
    if status_code == "CONFLICT":
        return ConflictError(message, report=report)
    return ValidationError(message, fields=fields, report=report)


def import_intake_batch(app: YardApplication, payload: Any) -> dict[str, Any]:
    document, source = _load_batch_source(payload)
    workspace = app.load()
    shift_code = ensure_open_shift(workspace)

    plan = build_batch_plan(document)
    plan.source_sha256 = source["source_sha256"]

    if plan.issues:
        raise _reject(plan, status_code="VALIDATION_ERROR", message="batch failed validation")

    # Plan passes every structural and in-batch rule; check yard state.  No
    # mutation has happened yet.
    conflicts = annotate_yard_conflicts(plan, workspace)
    if conflicts:
        raise _reject(
            plan,
            status_code="CONFLICT",
            message="batch conflicts with existing yard state",
        )

    # All checks passed — build trains, cars, and events exactly like the
    # single-create entry, then commit everything at once.
    created_trains: list[Any] = []
    created_cars: list[Any] = []
    events: list[Any] = []
    train_codes: list[str] = []
    car_codes: list[str] = []

    for row in plan.trains:
        train = row.train
        assert train is not None  # guaranteed by plan.accepted
        train.batch_code = plan.code
        cars = []
        for car_row in row.cars:
            car = car_from_input(car_row.car)
            workspace.cars[car.code] = car
            cars.append(car)
            created_cars.append(car)
            car_codes.append(car.code)
        workspace.intakes[train.code] = train
        created_trains.append(train)
        train_codes.append(train.code)
        event = workspace.record_event(
            shift_code,
            EventKind.TRAIN_RECEIVED,
            f"intake {train.code} received {len(cars)} cars via batch {plan.code}",
            {
                "route": train.route,
                "car_count": len(cars),
                "arrival_at": train.arrival_at,
                "batch_code": plan.code,
            },
        )
        events.append(event)

    batch_event = workspace.record_event(
        shift_code,
        EventKind.BATCH_IMPORTED,
        f"batch {plan.code} imported {len(train_codes)} trains and {len(car_codes)} cars",
        {
            "batch_code": plan.code,
            "source_kind": source["source_kind"],
            "source_path": source["source_path"],
            "source_bytes": source["source_bytes"],
            "source_sha256": source["source_sha256"],
            "train_codes": list(train_codes),
            "car_codes": list(car_codes),
            "received_at": plan.received_at,
        },
    )
    events.append(batch_event)

    record = IntakeBatchRecord(
        code=plan.code,
        received_at=plan.received_at,
        imported_at=now_iso(),
        shift_code=shift_code,
        source_kind=source["source_kind"],
        source_path=source["source_path"],
        source_bytes=source["source_bytes"],
        source_sha256=source["source_sha256"],
        train_codes=train_codes,
        car_codes=car_codes,
        note=plan.note,
        train_event_sequences=[event.sequence for event in events[:-1]],
    )
    workspace.batch_intakes[record.code] = record

    # Single atomic commit: one state-file replace plus journal appends.
    app.commit(workspace, events)

    return {
        "batch": record.to_dict(),
        "report": plan.report(),
        "intakes": [train.to_dict() for train in created_trains],
        "cars": [car.to_dict() for car in created_cars],
        "batch_event": batch_event.to_dict(),
    }


def get_intake_batch(app: YardApplication, batch_code: str) -> dict[str, Any]:
    workspace = app.load()
    record = workspace.batch_intakes.get(batch_code)
    if record is None:
        raise NotFoundError("intake batch", batch_code)
    intakes = [
        workspace.intakes[code].to_dict()
        for code in record.train_codes
        if code in workspace.intakes
    ]
    cars = [
        workspace.cars[code].to_dict()
        for code in record.car_codes
        if code in workspace.cars
    ]
    def belongs(event: Any) -> bool:
        if event.payload.get("batch_code") != batch_code:
            return False
        return event.kind in {EventKind.BATCH_IMPORTED, EventKind.TRAIN_RECEIVED}

    events = [
        event.to_dict()
        for event in workspace.events
        if event.shift_code == record.shift_code and belongs(event)
    ]
    return {
        "batch": record.to_dict(),
        "intakes": intakes,
        "cars": cars,
        "events": events,
    }


__all__ = ["get_intake_batch", "import_intake_batch"]
