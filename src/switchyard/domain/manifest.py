"""Frozen outbound departure manifest.

A manifest is published once an outbound plan is confirmed. It snapshots the
planned sequence, the actual assembled sequence, basic car information, and a
provenance trail (shift, intake, spotting track, pull run). Every publication
gets a per-train monotonically increasing version number and a SHA-256 content
digest so older versions stay byte-stable and verifiable even when the yard
keeps changing.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .enums import CarState

# Difference between plan and reality for one planned position.
PENDING = "PENDING"  # planned car not assembled yet, but the plan is still achievable
CONFLICT = "CONFLICT"  # plan and assembly cannot converge as-is
MATCH = "MATCH"  # assembled car matches the planned slot


@dataclass(slots=True)
class ManifestEntry:
    """One frozen line of the manifest."""

    position: int
    planned_car_code: str | None
    assembled_car_code: str | None
    plan_status: str
    car: dict[str, Any] | None
    origin: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "planned_car_code": self.planned_car_code,
            "assembled_car_code": self.assembled_car_code,
            "plan_status": self.plan_status,
            "car": copy.deepcopy(self.car),
            "origin": copy.deepcopy(self.origin),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ManifestEntry":
        return cls(
            position=int(raw["position"]),
            planned_car_code=None if raw.get("planned_car_code") is None else str(raw["planned_car_code"]),
            assembled_car_code=None if raw.get("assembled_car_code") is None else str(raw["assembled_car_code"]),
            plan_status=str(raw.get("plan_status", PENDING)),
            car=None if raw.get("car") is None else dict(raw["car"]),
            origin=dict(raw.get("origin") or {}),
        )


def classify_entry(
    planned: str | None,
    assembled: str | None,
    cars: dict[str, Any],
    buffered_codes: set[str] | None = None,
) -> str:
    """Classify a single planned/actual slot as MATCH, PENDING, or CONFLICT.

    PENDING means the plan has simply not been executed yet: the slot is empty
    and the planned car is still reserved (or parked in the buffer while
    another car is pulled). Everything else that breaks the planned sequence
    is a CONFLICT.
    """

    buffered_codes = buffered_codes or set()
    if assembled is None:
        if planned is None:
            return MATCH
        car = cars.get(planned)
        if car is not None and car.state in {CarState.RESERVED, CarState.STANDING}:
            return PENDING
        return CONFLICT
    if planned == assembled:
        return MATCH
    return CONFLICT


def classify_discrepancies(
    planned_codes: list[str],
    assembled_codes: list[str],
    cars: dict[str, Any],
    buffered_codes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Produce the full plan-vs-actual discrepancy list for a train."""

    buffered_codes = buffered_codes or set()
    discrepancies: list[dict[str, Any]] = []
    total = max(len(planned_codes), len(assembled_codes))
    for index in range(total):
        planned = planned_codes[index] if index < len(planned_codes) else None
        assembled = assembled_codes[index] if index < len(assembled_codes) else None
        status = classify_entry(planned, assembled, cars, buffered_codes)
        if status == MATCH:
            continue
        car_code = planned or assembled
        car = cars.get(car_code) if car_code else None
        live_state = str(car.state) if car is not None else "MISSING"
        live_location = car.location if car is not None else None
        if status == PENDING:
            if planned in buffered_codes:
                reason = f"planned car parked in buffer {live_location}"
            else:
                reason = "planned car not assembled yet"
        elif planned is None:
            reason = "assembled car is not part of the planned consist"
        elif assembled is None:
            reason = f"planned car is {live_state} and cannot be assembled"
        else:
            reason = f"planned {planned} but actual position holds {assembled}"
        discrepancies.append(
            {
                "position": index + 1,
                "planned_car_code": planned,
                "assembled_car_code": assembled,
                "status": status,
                "reason": reason,
                "live_car_state": live_state,
                "live_location": live_location,
            }
        )
    return discrepancies


def _intake_origin(workspace: Any, car_code: str) -> tuple[str | None, str | None]:
    for code, intake in workspace.intakes.items():
        if car_code in intake.consist:
            return code, intake.route
    return None, None


def _origin_record(workspace: Any, car: Any) -> dict[str, Any]:
    intake_code, intake_route = _intake_origin(workspace, car.code)
    received_at: str | None = None
    spotted_track: str | None = None
    spotted_at: str | None = None
    for event in workspace.events:
        kind = str(event.kind)
        if kind == "TRAIN_RECEIVED" and event.payload.get("car_count") is not None:
            # The intake event does not list individual cars, so match via the
            # owning intake recorded in its message payload scope.
            if intake_code and f"intake {intake_code} received" in event.message:
                received_at = event.at
        if kind != "TRAIN_CLASSIFIED":
            continue
        for spot in event.payload.get("spots", []):
            if spot.get("car_code") == car.code:
                spotted_track = str(spot.get("track_code"))
                spotted_at = event.at
                break
        if spotted_track:
            break
    return {
        "intake_code": intake_code,
        "intake_route": intake_route,
        "spotted_track": spotted_track,
        "current_location": car.location,
        "journey": [
            {"stage": "received", "ref": intake_code, "at": received_at},
            {"stage": "spotted", "ref": spotted_track, "at": spotted_at},
            {"stage": "current", "ref": car.location, "at": None},
        ],
        "source_note": f"arrived on {intake_code or 'UNKNOWN'} route {intake_route or 'UNKNOWN'}, spotted on {spotted_track or 'UNKNOWN'}",
    }


def _car_snapshot(car: Any) -> dict[str, Any]:
    return {
        "code": car.code,
        "kind": str(car.kind),
        "destination": car.destination,
        "loaded": car.loaded,
        "length_m": car.length_m,
        "danger_class": car.danger_class,
        "state": str(car.state),
        "location": car.location,
        "note": car.note,
    }


def _active_run(workspace: Any, outbound: Any) -> Any:
    for code in reversed(outbound.run_codes):
        run = workspace.runs.get(code)
        if run is not None:
            return run
    return None


def build_manifest_document(
    manifest_code: str,
    version: int,
    workspace: Any,
    outbound: Any,
    published_at: str,
) -> dict[str, Any]:
    """Build the immutable manifest document from a workspace snapshot.

    This only reads the workspace; callers persist the returned dict. The
    digest covers the frozen payload excluding the digest field itself.
    """

    shift_code = "NONE"
    for code, shift in workspace.shifts.items():
        if str(shift.state) == "OPEN":
            shift_code = code
            break

    run = _active_run(workspace, outbound)
    buffered_codes = {code for bay in workspace.buffer_bays.values() for code in bay.stack}
    discrepancies = classify_discrepancies(
        list(outbound.planned_car_codes),
        list(outbound.assembled_car_codes),
        workspace.cars,
        buffered_codes,
    )
    pending = [item for item in discrepancies if item["status"] == PENDING]
    conflicts = [item for item in discrepancies if item["status"] == CONFLICT]

    entries: list[dict[str, Any]] = []
    total = max(len(outbound.planned_car_codes), len(outbound.assembled_car_codes))
    for index in range(total):
        planned = outbound.planned_car_codes[index] if index < len(outbound.planned_car_codes) else None
        assembled = outbound.assembled_car_codes[index] if index < len(outbound.assembled_car_codes) else None
        car_code = planned or assembled
        car = workspace.cars.get(car_code) if car_code else None
        entry = ManifestEntry(
            position=index + 1,
            planned_car_code=planned,
            assembled_car_code=assembled,
            plan_status=classify_entry(planned, assembled, workspace.cars, buffered_codes),
            car=_car_snapshot(car) if car is not None else None,
            origin=_origin_record(workspace, car) if car is not None else {"source_note": "car record missing"},
        )
        entries.append(entry.to_dict())

    buffered_cars = [
        {"car_code": code, "bay_code": bay.code}
        for bay in workspace.buffer_bays.values()
        for code in bay.stack
    ]
    planned_set = set(outbound.planned_car_codes)
    planned_sources = sorted(
        {
            car.location
            for car in workspace.cars.values()
            if car.code in planned_set
            and car.state == CarState.STANDING
            and car.location in workspace.tracks
        }
    )

    document = {
        "code": manifest_code,
        "outbound_code": outbound.code,
        "version": version,
        "published_at": published_at,
        "shift_code": shift_code,
        "outbound_state": str(outbound.state),
        "destination": outbound.destination,
        "references": {
            "shift_code": shift_code,
            "pull_run_code": run.code if run is not None else None,
            "pull_run_state": str(run.state) if run is not None else None,
            "transfer_bay_code": run.transfer_code if run is not None else None,
            "run_codes": list(outbound.run_codes),
        },
        "planned_sequence": list(outbound.planned_car_codes),
        "assembled_sequence": list(outbound.assembled_car_codes),
        "entries": entries,
        "discrepancy_summary": {
            "total_slots": total,
            "pending_count": len(pending),
            "conflict_count": len(conflicts),
            "ready_to_depart": not pending and not conflicts,
        },
        "discrepancies": discrepancies,
        "yard_context": {
            "buffered_cars": buffered_cars,
            "planned_source_tracks": planned_sources,
            "run_current_step": run.current_step if run is not None else None,
            "run_total_steps": len(run.steps) if run is not None else 0,
            "workspace_version": workspace.version,
        },
    }
    document["content_digest"] = compute_digest(document)
    return document


def canonical_payload(document: dict[str, Any]) -> str:
    """Stable JSON serialization used for digesting and export comparison."""

    frozen = {key: value for key, value in document.items() if key != "content_digest"}
    return json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(document).encode("utf-8")).hexdigest()


def verify_manifest(document: dict[str, Any], workspace: Any) -> dict[str, Any]:
    """Check digest integrity and compare a frozen version against the yard.

    The document itself never changes: ``digest_valid`` confirms the stored
    bytes are intact, while ``matches_current_yard`` reports whether the live
    train still matches this version. Old versions are expected to diverge as
    work continues; they remain retrievable regardless.
    """

    stored_digest = str(document.get("content_digest", ""))
    digest_valid = stored_digest == compute_digest(document)

    outbound_code = str(document["outbound_code"])
    outbound = workspace.outbounds.get(outbound_code)
    frozen_state = document.get("outbound_state")
    references = dict(document.get("references") or {})
    yard_context = dict(document.get("yard_context") or {})
    live = {
        "outbound_exists": outbound is not None,
        "planned_sequence": list(outbound.planned_car_codes) if outbound else None,
        "assembled_sequence": list(outbound.assembled_car_codes) if outbound else None,
        "outbound_state": str(outbound.state) if outbound else None,
    }
    divergences: list[str] = []
    if outbound is None:
        divergences.append("outbound train no longer exists")
    else:
        if frozen_state is not None and frozen_state != str(outbound.state):
            divergences.append(
                f"outbound state changed: {frozen_state} -> {outbound.state.value}"
            )
        if list(document.get("planned_sequence", [])) != outbound.planned_car_codes:
            divergences.append("planned sequence changed")
        if list(document.get("assembled_sequence", [])) != outbound.assembled_car_codes:
            divergences.append("assembled sequence changed")

        # Pull run progress. Early buffer-only moves do not touch the planned
        # cars or the assembled sequence, so the frozen run step/state must be
        # compared explicitly, otherwise a partially executed run still looks
        # identical to the frozen plan.
        run_code = references.get("pull_run_code")
        frozen_step = yard_context.get("run_current_step")
        run = workspace.runs.get(str(run_code)) if run_code else None
        if run is None:
            if run_code:
                divergences.append(f"pull run {run_code} no longer exists")
        else:
            live["pull_run_code"] = run.code
            live["pull_run_state"] = str(run.state)
            live["pull_run_current_step"] = run.current_step
            frozen_run_state = references.get("pull_run_state")
            if frozen_run_state is not None and frozen_run_state != str(run.state):
                divergences.append(
                    f"pull run {run.code} state changed: {frozen_run_state} -> {run.state.value}"
                )
            if frozen_step is not None and int(frozen_step) != run.current_step:
                divergences.append(
                    f"pull run {run.code} advanced from step {frozen_step} to {run.current_step}"
                )

        for entry in document.get("entries", []):
            code = entry.get("planned_car_code") or entry.get("assembled_car_code")
            car = workspace.cars.get(code) if code else None
            frozen_car = entry.get("car") or {}
            if car is None:
                if frozen_car:
                    divergences.append(f"car {code} removed from yard")
                continue
            if frozen_car.get("state") != str(car.state):
                divergences.append(f"car {code} state changed: {frozen_car.get('state')} -> {car.state}")
            if frozen_car.get("location") != car.location:
                divergences.append(f"car {code} location changed: {frozen_car.get('location')} -> {car.location}")

        # Buffer bays. A BUFFER step only moves a non-planned blocker car, so
        # this is the only signal available for early execution. Compare both
        # the full frozen snapshot and per-car live locations.
        frozen_bays: dict[str, set[str]] = {}
        for item in yard_context.get("buffered_cars", []):
            frozen_bays.setdefault(str(item.get("bay_code")), set()).add(str(item.get("car_code")))
        live_bays = {code: set(bay.stack) for code, bay in workspace.buffer_bays.items()}
        for bay_code in sorted(set(frozen_bays) | set(live_bays)):
            before = frozen_bays.get(bay_code, set())
            after = live_bays.get(bay_code, set())
            for code in sorted(after - before):
                car = workspace.cars.get(code)
                location = car.location if car is not None else "?"
                divergences.append(f"car {code} newly buffered in {bay_code} (now at {location})")
            for code in sorted(before - after):
                car = workspace.cars.get(code)
                location = car.location if car is not None else "removed"
                divergences.append(f"car {code} left buffer {bay_code} (now at {location})")
    return {
        "code": document.get("code"),
        "version": document.get("version"),
        "digest_valid": digest_valid,
        "stored_digest": stored_digest,
        "recomputed_digest": compute_digest(document),
        "matches_current_yard": not divergences,
        "divergences": divergences,
        "live": live,
    }


def departure_readiness(workspace: Any, outbound: Any) -> dict[str, Any]:
    """Fresh plan-vs-actual read used while preparing to depart.

    Besides the per-slot pending/conflict classification, this surfaces the
    live shunting picture (active pull run progress and every car currently
    parked in a buffer bay) so the driver can see that an in-progress buffer
    move is still an unexecuted plan rather than a conflict.
    """

    buffered_codes = {code for bay in workspace.buffer_bays.values() for code in bay.stack}
    discrepancies = classify_discrepancies(
        list(outbound.planned_car_codes),
        list(outbound.assembled_car_codes),
        workspace.cars,
        buffered_codes,
    )
    pending = [item for item in discrepancies if item["status"] == PENDING]
    conflicts = [item for item in discrepancies if item["status"] == CONFLICT]

    run = _active_run(workspace, outbound)
    buffered_cars = [
        {
            "car_code": code,
            "bay_code": bay.code,
            "planned_for_train": code in set(outbound.planned_car_codes),
        }
        for bay in workspace.buffer_bays.values()
        for code in bay.stack
    ]
    return {
        "outbound_code": outbound.code,
        "outbound_state": str(outbound.state),
        "planned_sequence": list(outbound.planned_car_codes),
        "assembled_sequence": list(outbound.assembled_car_codes),
        "pending": pending,
        "conflicts": conflicts,
        "ready_to_depart": not pending and not conflicts,
        "buffered_cars": buffered_cars,
        "pull_run": (
            {
                "code": run.code,
                "state": str(run.state),
                "current_step": run.current_step,
                "total_steps": len(run.steps),
                "remaining_steps": run.remaining(),
            }
            if run is not None
            else None
        ),
    }


__all__ = [
    "CONFLICT",
    "MATCH",
    "PENDING",
    "ManifestEntry",
    "build_manifest_document",
    "canonical_payload",
    "classify_discrepancies",
    "classify_entry",
    "compute_digest",
    "departure_readiness",
    "verify_manifest",
]
