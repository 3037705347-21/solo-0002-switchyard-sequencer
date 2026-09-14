"""Read-only car location and ownership view.

Dispatchers usually only know a car code and need one consistent answer that
joins the car's own state field, its *physical* presence in a track stack,
transfer bay, or train consist, and the plans/events that reference it.

The view never mutates the workspace.  When the sources disagree it does not
silently pick one field: every disagreement is surfaced in ``consistency``
with the source that made each claim (``state`` / ``ref`` / ``run`` /
``intake`` / ``yard``), while ``phase`` reflects the physically observed
situation so a departed or removed car can never be reported as still in the
yard.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, EventKind, MoveVerb, RunState
from .metrics import yard_metrics

# Dispatcher-facing presence phases used by both this view and the yard
# overview cross-check.  RECEIVED cars are folded into IN_YARD.
PHASE_IN_YARD = "IN_YARD"
PHASE_BUFFERED = "BUFFERED"
PHASE_RESERVED = "RESERVED"
PHASE_ASSEMBLED = "ASSEMBLED"
PHASE_DEPARTED = "DEPARTED"
PHASE_REMOVED = "REMOVED"
PHASE_UNKNOWN = "UNKNOWN"

IN_YARD_PHASES = {
    PHASE_IN_YARD,
    PHASE_BUFFERED,
    PHASE_RESERVED,
    PHASE_ASSEMBLED,
}

_STATE_CLAIMED_PHASE = {
    CarState.RECEIVED: PHASE_IN_YARD,
    CarState.STANDING: PHASE_IN_YARD,
    CarState.RESERVED: PHASE_RESERVED,
    CarState.ASSEMBLED: PHASE_ASSEMBLED,
    CarState.DEPARTED: PHASE_DEPARTED,
    CarState.REMOVED: PHASE_REMOVED,
}


def _phases_compatible(claimed: str, observed: str) -> bool:
    """Whether a state-field claim agrees with the physical observation.

    A buffered blocker is still STANDING on paper (the executor only moves
    its location to the bay), so IN_YARD claim vs BUFFERED observation is
    expected, not a conflict.
    """
    if claimed == observed:
        return True
    if claimed == PHASE_IN_YARD and observed == PHASE_BUFFERED:
        return True
    return False

_PHASE_LABELS = {
    PHASE_IN_YARD: "in yard",
    PHASE_BUFFERED: "buffered in transfer bay",
    PHASE_RESERVED: "reserved for outbound",
    PHASE_ASSEMBLED: "assembled on outbound train",
    PHASE_DEPARTED: "departed",
    PHASE_REMOVED: "removed from yard",
    PHASE_UNKNOWN: "unknown",
}

_MOVE_VERB_LABEL = {
    MoveVerb.BUFFER: "buffered",
    MoveVerb.PULL: "pulled",
    MoveVerb.RETURN: "returned",
}


def build_car_view(workspace: Any, car_code: str) -> dict[str, Any]:
    """Return the reconciled location/ownership document for one car.

    A missing car is reported with ``found = False``; the service layer turns
    that into a NotFoundError so the report module stays HTTP-free.
    """
    code = (car_code or "").strip().upper()
    car = workspace.cars.get(code)
    if car is None:
        return {"code": code, "found": False}

    alerts: list[dict[str, Any]] = []

    track_claims = _scan_tracks(workspace, code)
    bay_claims = _scan_bays(workspace, code)
    outbound_claims = _scan_outbounds(workspace, code)
    intake_claims = _scan_intakes(workspace, code)
    run_steps = _scan_runs(workspace, code)

    physical = _resolve_physical(car, track_claims, bay_claims, outbound_claims, alerts)
    claimed_phase = _STATE_CLAIMED_PHASE.get(car.state, PHASE_UNKNOWN)
    observed_phase = _derive_phase(car, physical)
    phase_conflict = not _phases_compatible(claimed_phase, observed_phase)
    if phase_conflict:
        alerts.append(
            _alert(
                "phase-conflict",
                f"car state claims {claimed_phase} but physical references show {observed_phase}",
                source="state",
                severity="critical",
                claimed=claimed_phase,
                observed=observed_phase,
            )
        )

    in_yard = observed_phase in IN_YARD_PHASES

    _check_location_field(car, physical, claimed_phase, alerts)
    _check_track_consistency(car, physical, track_claims, alerts)
    _check_outbound_consistency(car, physical, outbound_claims, alerts)
    _check_run_consistency(workspace, code, car, physical, run_steps, alerts)
    _check_intake_consistency(car, intake_claims, alerts)

    events = _referenced_events(workspace, code, intake_claims, outbound_claims, run_steps)
    recent_events = [event.to_dict() for event in reversed(events[-10:])]
    last_event = events[-1] if events else None
    last_action = _last_action(car, physical, last_event)
    move_history = _move_history(workspace, run_steps)
    timeline = _state_timeline(workspace, code, events, intake_claims, outbound_claims, run_steps)

    shift = _resolve_shift(workspace, events)
    ownership = _ownership(intake_claims, outbound_claims, shift)
    plans = _plans(workspace, outbound_claims, run_steps)
    yard = _yard_crosscheck(workspace, car, claimed_phase, observed_phase)
    alerts.extend(yard.pop("alerts"))

    return {
        "found": True,
        "car": car.to_dict(),
        "phase": observed_phase,
        "phase_label": _PHASE_LABELS.get(observed_phase, observed_phase),
        "claimed_phase": claimed_phase,
        "phase_conflict": phase_conflict,
        "in_yard": in_yard,
        "location": _location_block(car, physical, track_claims, bay_claims, outbound_claims),
        "ownership": ownership,
        "plans": plans,
        "last_action": last_action,
        "recent_changes": {
            "events": recent_events,
            "move_history": move_history,
            "state_timeline": timeline,
        },
        "yard_overview": yard,
        "consistency": alerts,
        "alert_count": len(alerts),
        "version": workspace.version,
    }


# ---------------------------------------------------------------------------
# Reference scanning
# ---------------------------------------------------------------------------


def _scan_tracks(workspace: Any, code: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for track in workspace.tracks.values():
        if code in track.stack:
            bottom_index = track.stack.index(code)
            claims.append(
                {
                    "kind": "standing_track",
                    "code": track.code,
                    "index_from_top": len(track.stack) - 1 - bottom_index,
                    "stack_size": len(track.stack),
                    "state": str(track.state),
                    "purpose": str(track.purpose),
                }
            )
    return claims


def _scan_bays(workspace: Any, code: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for bay in workspace.buffer_bays.values():
        if code in bay.stack:
            claims.append(
                {
                    "kind": "buffer_bay",
                    "code": bay.code,
                    "index_from_top": len(bay.stack) - 1 - bay.stack.index(code),
                    "stack_size": len(bay.stack),
                }
            )
    return claims


def _scan_outbounds(workspace: Any, code: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for train in workspace.outbounds.values():
        if code in train.planned_car_codes or code in train.assembled_car_codes:
            claims.append(
                {
                    "kind": "outbound_train",
                    "code": train.code,
                    "destination": train.destination,
                    "state": str(train.state),
                    "planned": code in train.planned_car_codes,
                    "assembled": code in train.assembled_car_codes,
                    "planned_position": (
                        train.planned_car_codes.index(code) + 1 if code in train.planned_car_codes else None
                    ),
                    "assembled_position": (
                        train.assembled_car_codes.index(code) + 1
                        if code in train.assembled_car_codes
                        else None
                    ),
                }
            )
    return claims


def _scan_intakes(workspace: Any, code: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for train in workspace.intakes.values():
        if code in train.consist:
            claims.append(
                {
                    "kind": "intake_train",
                    "code": train.code,
                    "route": train.route,
                    "state": str(train.state),
                    "unplaced": code in train.unplaced,
                }
            )
    return claims


def _scan_runs(workspace: Any, code: str) -> list[dict[str, Any]]:
    """Return every move step referencing the car across every pull run."""
    found: list[dict[str, Any]] = []
    for run in workspace.runs.values():
        for index, step in enumerate(run.steps):
            if step.car_code != code:
                continue
            found.append(
                {
                    "run_code": run.code,
                    "outbound_code": run.outbound_code,
                    "transfer_code": run.transfer_code,
                    "step_index": index,
                    "verb": str(step.verb),
                    "source_code": step.source_code,
                    "target_code": step.target_code,
                    "executed": index < run.current_step,
                    "run_state": str(run.state),
                }
            )
    return found


# ---------------------------------------------------------------------------
# Physical presence and phase
# ---------------------------------------------------------------------------


def _resolve_physical(
    car: Any,
    track_claims: list[dict[str, Any]],
    bay_claims: list[dict[str, Any]],
    outbound_claims: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collate physical containment; multiple containers are inconsistent."""
    containers: list[dict[str, Any]] = []
    containers.extend(track_claims)
    containers.extend(bay_claims)
    for claim in outbound_claims:
        if claim["assembled"]:
            containers.append(
                {
                    "kind": "outbound_train",
                    "code": claim["code"],
                    "state": claim["state"],
                    "position": claim["assembled_position"],
                }
            )

    if len(containers) > 1:
        locations = sorted({item["code"] for item in containers})
        alerts.append(
            _alert(
                "duplicate-physical-location",
                f"car is physically present in {len(containers)} places: {', '.join(locations)}",
                source="ref",
                severity="critical",
                containers=containers,
            )
        )

    primary = containers[0] if containers else None
    if primary is not None:
        location_kind: str | None = primary["kind"]
        location_code: str | None = primary["code"]
    elif car.location == "INTAKE":
        location_kind, location_code = "intake", car.location
    elif car.location is None:
        location_kind, location_code = None, None
    else:
        location_kind, location_code = "dangling_reference", car.location

    return {
        "kind": location_kind,
        "code": location_code,
        "containers": containers,
        "track": track_claims[0] if track_claims else None,
        "bay": bay_claims[0] if bay_claims else None,
        "assembled_on": next((claim for claim in outbound_claims if claim["assembled"]), None),
    }


def _derive_phase(car: Any, physical: dict[str, Any]) -> str:
    """Phase from physical evidence, independent of the car state field."""
    if car.state == CarState.REMOVED:
        return PHASE_REMOVED
    assembled = physical.get("assembled_on")
    if assembled is not None:
        return PHASE_DEPARTED if assembled["state"] == "DEPARTED" else PHASE_ASSEMBLED
    if physical.get("bay") is not None:
        return PHASE_BUFFERED
    if physical.get("track") is not None:
        return PHASE_RESERVED if car.state == CarState.RESERVED else PHASE_IN_YARD
    if car.location == "INTAKE" or car.state == CarState.RECEIVED:
        return PHASE_IN_YARD
    if car.state == CarState.DEPARTED:
        return PHASE_DEPARTED
    return PHASE_UNKNOWN


# ---------------------------------------------------------------------------
# Consistency checks (every alert names the conflicting source)
# ---------------------------------------------------------------------------


def _check_location_field(
    car: Any,
    physical: dict[str, Any],
    claimed_phase: str,
    alerts: list[dict[str, Any]],
) -> None:
    if claimed_phase in {PHASE_DEPARTED, PHASE_REMOVED}:
        return
    if car.location is None:
        alerts.append(
            _alert(
                "location-missing",
                "car has no location value but has not departed or been removed",
                source="state",
                severity="warning",
                location=None,
            )
        )
        return
    if physical["containers"]:
        observed = physical["containers"][0]["code"]
        if car.location != observed:
            alerts.append(
                _alert(
                    "location-mismatch",
                    f"car.location is {car.location} but the car is physically in {observed}",
                    source="state",
                    severity="critical",
                    claimed_location=car.location,
                    observed_location=observed,
                )
            )
    elif physical["kind"] == "dangling_reference":
        alerts.append(
            _alert(
                "location-dangling",
                f"car.location {car.location} matches no track, bay, intake, or train consist",
                source="state",
                severity="warning",
                claimed_location=car.location,
            )
        )


def _check_track_consistency(
    car: Any,
    physical: dict[str, Any],
    track_claims: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> None:
    expected_on_track = car.state in {CarState.STANDING, CarState.RESERVED}
    in_bay = physical.get("bay") is not None
    # When the car is also stacked somewhere else (e.g. still on a train
    # consist), the duplicate-physical-location alert already names both
    # sources; only report missing/extra track membership from an unambiguous
    # position.
    ambiguous = len(physical.get("containers", [])) > 1
    # A car parked in the transfer bay is deliberately off its track while a
    # deeper car is pulled; missing track membership is expected there.
    if expected_on_track and not track_claims and not in_bay and not ambiguous:
        alerts.append(
            _alert(
                "track-membership-missing",
                f"car state is {car.state} but no standing track stack contains it",
                source="ref",
                severity="critical",
                state=str(car.state),
            )
        )
    if not expected_on_track and track_claims and not ambiguous:
        alerts.append(
            _alert(
                "track-membership-unexpected",
                f"car state is {car.state} but track {track_claims[0]['code']} still stacks it",
                source="ref",
                severity="critical",
                track=track_claims[0]["code"],
                state=str(car.state),
            )
        )
    for claim in track_claims:
        if claim["state"] == "MAINTENANCE":
            alerts.append(
                _alert(
                    "track-in-maintenance",
                    f"car sits on {claim['code']} which is in MAINTENANCE",
                    source="ref",
                    severity="warning",
                    track=claim["code"],
                )
            )


def _check_outbound_consistency(
    car: Any,
    physical: dict[str, Any],
    outbound_claims: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> None:
    active = [claim for claim in outbound_claims if claim["state"] in {"DRAFT", "PLANNED", "READY"}]
    planned_live = [claim for claim in active if claim["planned"]]
    committed = [claim for claim in planned_live if claim["state"] in {"PLANNED", "READY"}]
    assembled_claims = [claim for claim in outbound_claims if claim["assembled"]]

    # RESERVED must be backed by a committed outbound ticket (a DRAFT ticket
    # alone is normal and does not reserve cars).
    if car.state == CarState.RESERVED and not committed:
        alerts.append(
            _alert(
                "reservation-without-ticket",
                "car is RESERVED but no PLANNED/READY outbound train plans it",
                source="ref",
                severity="critical",
            )
        )
    if car.state == CarState.STANDING and physical.get("bay") is None and committed:
        alerts.append(
            _alert(
                "ticket-without-reservation",
                f"car is STANDING but committed outbound {sorted(c['code'] for c in committed)} still plans it",
                source="ref",
                severity="critical",
                outbounds=sorted(c["code"] for c in committed),
            )
        )
    if car.state == CarState.ASSEMBLED and not assembled_claims:
        alerts.append(
            _alert(
                "assembled-without-train",
                "car is ASSEMBLED but no outbound train consist contains it",
                source="ref",
                severity="critical",
            )
        )
    ready_or_departed = [claim for claim in outbound_claims if claim["state"] in {"READY", "DEPARTED"}]
    if car.state == CarState.RESERVED and ready_or_departed:
        alerts.append(
            _alert(
                "train-ready-car-not-assembled",
                f"car is still RESERVED but outbound {ready_or_departed[0]['code']} "
                f"is {ready_or_departed[0]['state']}",
                source="ref",
                severity="critical",
                outbound=ready_or_departed[0]["code"],
            )
        )
    if car.state == CarState.DEPARTED:
        active_assembled = [claim for claim in assembled_claims if claim["state"] != "DEPARTED"]
        if active_assembled:
            alerts.append(
                _alert(
                    "departed-on-active-train",
                    f"car is DEPARTED but remains on the consist of {active_assembled[0]['code']} "
                    f"({active_assembled[0]['state']})",
                    source="ref",
                    severity="critical",
                    outbound=active_assembled[0]["code"],
                )
            )
        if not [claim for claim in assembled_claims if claim["state"] == "DEPARTED"]:
            alerts.append(
                _alert(
                    "departed-without-train",
                    "car is DEPARTED but no departed outbound train consist contains it",
                    source="ref",
                    severity="warning",
                )
            )
    committed_codes = [claim["code"] for claim in committed]
    if len(committed_codes) > 1:
        alerts.append(
            _alert(
                "duplicate-reservation",
                f"car is planned on {len(committed_codes)} committed outbound trains: "
                f"{sorted(committed_codes)}",
                source="ref",
                severity="critical",
                outbounds=sorted(committed_codes),
            )
        )
    for claim in outbound_claims:
        if claim["planned"] and not claim["assembled"] and claim["state"] in {"READY", "DEPARTED"}:
            alerts.append(
                _alert(
                    "planned-not-assembled",
                    f"outbound {claim['code']} is {claim['state']} but the car never reached its consist",
                    source="ref",
                    severity="critical",
                    outbound=claim["code"],
                )
            )


def _check_run_consistency(
    workspace: Any,
    code: str,
    car: Any,
    physical: dict[str, Any],
    run_steps: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> None:
    def step_key(step: dict[str, Any]) -> tuple[str, int]:
        return (step["run_code"], step["step_index"])

    track_codes = {claim["code"] for claim in physical.get("containers", []) if claim["kind"] == "standing_track"}
    bay_codes = {claim["code"] for claim in physical.get("containers", []) if claim["kind"] == "buffer_bay"}

    pending = [step for step in run_steps if not step["executed"]]
    if pending:
        step = min(pending, key=step_key)
        verb = step["verb"]
        # When the car is already in a later lifecycle state the pending step
        # is stale, but the outbound/state checks already name that precisely.
        if verb == "PULL":
            if car.state in {CarState.STANDING, CarState.RECEIVED}:
                alerts.append(
                    _alert(
                        "run-step-state-conflict",
                        f"pending PULL in {step['run_code']} expects the car RESERVED, found {car.state}",
                        source="run",
                        severity="critical",
                        run=step["run_code"],
                        state=str(car.state),
                    )
                )
            if step["source_code"] not in track_codes:
                alerts.append(
                    _alert(
                        "run-step-source-conflict",
                        f"pending PULL in {step['run_code']} expects the car on {step['source_code']}, "
                        f"found {sorted(track_codes) or 'nowhere'}",
                        source="run",
                        severity="critical",
                        run=step["run_code"],
                        expected_location=step["source_code"],
                    )
                )
        elif verb == "BUFFER":
            if car.state in {CarState.RESERVED, CarState.ASSEMBLED, CarState.DEPARTED}:
                alerts.append(
                    _alert(
                        "run-step-state-conflict",
                        f"pending BUFFER in {step['run_code']} expects a STANDING car, found {car.state}",
                        source="run",
                        severity="critical",
                        run=step["run_code"],
                        state=str(car.state),
                    )
                )
            if step["source_code"] not in track_codes and car.state not in {
                CarState.ASSEMBLED,
                CarState.DEPARTED,
                CarState.REMOVED,
            }:
                alerts.append(
                    _alert(
                        "run-step-source-conflict",
                        f"pending BUFFER in {step['run_code']} expects the car on {step['source_code']}",
                        source="run",
                        severity="critical",
                        run=step["run_code"],
                        expected_location=step["source_code"],
                    )
                )
        elif verb == "RETURN":
            if car.state != CarState.STANDING or step["source_code"] not in bay_codes:
                if car.state in {CarState.STANDING, CarState.RESERVED, CarState.RECEIVED}:
                    alerts.append(
                        _alert(
                            "run-step-source-conflict",
                            f"pending RETURN in {step['run_code']} expects the car in bay "
                            f"{step['source_code']}, found {sorted(bay_codes) or car.location}",
                            source="run",
                            severity="critical",
                            run=step["run_code"],
                            expected_location=step["source_code"],
                        )
                    )

    executed = [step for step in run_steps if step["executed"]]
    if executed:
        last_step = max(executed, key=step_key)
        verb = last_step["verb"]
        expected_container = last_step["target_code"]
        if verb == "BUFFER":
            present = expected_container in bay_codes
        elif verb == "PULL":
            present = expected_container in {
                claim["code"]
                for claim in physical.get("containers", [])
                if claim["kind"] == "outbound_train"
            }
            # A pulled car leaves with the train once departed; that is fine.
            outbound = workspace.outbounds.get(expected_container)
            if outbound is not None and str(outbound.state) == "DEPARTED":
                present = True
        else:  # RETURN
            present = expected_container in track_codes
        if not present and car.state not in {CarState.DEPARTED, CarState.REMOVED}:
            alerts.append(
                _alert(
                    "run-step-location-conflict",
                    f"the last executed {verb} for the car ended at {expected_container} "
                    f"but it is not physically there",
                    source="run",
                    severity="critical",
                    run=last_step["run_code"],
                    claimed_location=car.location,
                    expected_location=expected_container,
                )
            )


def _check_intake_consistency(
    car: Any,
    intake_claims: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> None:
    for claim in intake_claims:
        if claim["unplaced"] and car.state in {CarState.STANDING, CarState.RESERVED, CarState.ASSEMBLED}:
            alerts.append(
                _alert(
                    "intake-unplaced-stale",
                    f"intake {claim['code']} still lists the car as unplaced but its state is {car.state}",
                    source="intake",
                    severity="warning",
                    intake=claim["code"],
                    state=str(car.state),
                )
            )
        if claim["state"] == "CANCELLED" and car.state not in {CarState.REMOVED, CarState.DEPARTED}:
            alerts.append(
                _alert(
                    "intake-cancelled-car-active",
                    f"intake {claim['code']} was cancelled but the car is still {car.state}",
                    source="intake",
                    severity="critical",
                    intake=claim["code"],
                    state=str(car.state),
                )
            )
        if claim["state"] == "CLASSIFIED" and car.state == CarState.RECEIVED:
            alerts.append(
                _alert(
                    "intake-classified-car-received",
                    f"intake {claim['code']} is CLASSIFIED but the car is still RECEIVED",
                    source="intake",
                    severity="critical",
                    intake=claim["code"],
                )
            )


# ---------------------------------------------------------------------------
# Events, actions, ownership, plans
# ---------------------------------------------------------------------------


def _is_token_char(char: str) -> bool:
    return char.isalnum() or char in {"_", "-"}


def _payload_references(payload: Any, token: str) -> bool:
    if isinstance(payload, dict):
        return any(_payload_references(value, token) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_payload_references(value, token) for value in payload)
    if isinstance(payload, str):
        start = 0
        while True:
            index = payload.find(token, start)
            if index < 0:
                return False
            before = payload[index - 1] if index > 0 else ""
            after_pos = index + len(token)
            after = payload[after_pos] if after_pos < len(payload) else ""
            if not _is_token_char(before) and not _is_token_char(after):
                return True
            start = index + 1
    return False


def _message_references(message: str, tokens: set[str]) -> bool:
    for token in tokens:
        start = 0
        while True:
            index = message.find(token, start)
            if index < 0:
                break
            before = message[index - 1] if index > 0 else ""
            after_pos = index + len(token)
            after = message[after_pos] if after_pos < len(message) else ""
            if not _is_token_char(before) and not _is_token_char(after):
                return True
            start = index + 1
    return False


def _referenced_events(
    workspace: Any,
    code: str,
    intake_claims: list[dict[str, Any]],
    outbound_claims: list[dict[str, Any]],
    run_steps: list[dict[str, Any]],
) -> list[Any]:
    # Event payloads/messages do not always name the car directly (a pull run
    # advance only names the run), so correlate through the specific entities
    # that reference this car.  Matching only those entities keeps events from
    # unrelated trains on the same shift out of the car's trail.
    intake_codes = {claim["code"] for claim in intake_claims}
    outbound_codes = {claim["code"] for claim in outbound_claims}
    run_codes = {step["run_code"] for step in run_steps}
    # A run points back at its outbound, so a run-only event (started/advanced)
    # is admitted when the run belongs to an outbound linked to this car.
    run_to_outbound = {
        step["run_code"]: step["outbound_code"]
        for step in run_steps
    }

    result: list[Any] = []
    for event in workspace.events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if _payload_references(payload, code):
            result.append(event)
            continue
        kind = event.kind
        if kind == EventKind.TRAIN_RECEIVED or kind == EventKind.TRAIN_CLASSIFIED:
            if _message_references(event.message, intake_codes):
                result.append(event)
        elif kind == EventKind.TRAIN_CREATED:
            if _message_references(event.message, outbound_codes):
                result.append(event)
        elif kind == EventKind.PULL_PLANNED:
            if _message_references(event.message, run_codes):
                result.append(event)
        elif kind in {EventKind.PULL_RUN_STARTED, EventKind.PULL_RUN_ADVANCED}:
            mentioned_run = next(
                (run_code for run_code in run_codes if _message_references(event.message, {run_code})),
                None,
            )
            if mentioned_run is not None and run_to_outbound.get(mentioned_run) in outbound_codes:
                result.append(event)
        elif kind == EventKind.PULL_RUN_COMPLETED:
            mentioned_run = next(
                (run_code for run_code in run_codes if _message_references(event.message, {run_code})),
                None,
            )
            assembled = payload.get("assembled_car_codes", [])
            if mentioned_run is not None and run_to_outbound.get(mentioned_run) in outbound_codes:
                if not isinstance(assembled, list) or not assembled or code in {str(item) for item in assembled}:
                    result.append(event)
        elif kind == EventKind.TRAIN_DEPARTED:
            if _message_references(event.message, outbound_codes):
                result.append(event)
    return result


def _last_action(car: Any, physical: dict[str, Any], last_event: Any) -> dict[str, Any]:
    if last_event is not None:
        return {
            "at": last_event.at,
            "kind": str(last_event.kind),
            "message": last_event.message,
            "shift_code": last_event.shift_code,
            "sequence": last_event.sequence,
            "derived": False,
        }
    return {
        "at": None,
        "kind": None,
        "message": f"no recorded action; car currently {car.state} at {physical.get('code')}",
        "shift_code": None,
        "sequence": None,
        "derived": True,
    }


def _state_timeline(
    workspace: Any,
    code: str,
    events: list[Any],
    intake_claims: list[dict[str, Any]],
    outbound_claims: list[dict[str, Any]],
    run_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Phases reconstructed from the correlated event trail."""
    intake_codes = {claim["code"] for claim in intake_claims}
    planned_runs = {step["run_code"] for step in run_steps}
    assembled_outbounds = {claim["code"] for claim in outbound_claims if claim["assembled"]}

    timeline: list[dict[str, Any]] = []
    for event in events:
        phase: str | None = None
        kind = event.kind
        if kind == EventKind.TRAIN_RECEIVED and _message_references(event.message, intake_codes):
            phase = "RECEIVED"
        elif kind == EventKind.TRAIN_CLASSIFIED and _message_references(event.message, intake_codes):
            spotted = {
                str(item.get("car_code"))
                for item in event.payload.get("spots", [])
                if isinstance(item, dict)
            }
            unplaced = {str(item) for item in event.payload.get("unplaced", [])}
            if code in spotted or (not spotted and not unplaced):
                phase = "STANDING"
            elif code in unplaced:
                phase = "RECEIVED"
        elif kind == EventKind.PULL_PLANNED:
            mentioned_run = next(
                (run_code for run_code in planned_runs if _message_references(event.message, {run_code})),
                None,
            )
            if mentioned_run is not None:
                run = workspace.runs.get(mentioned_run)
                if run is not None and any(
                    claim["planned"] and claim["code"] == run.outbound_code for claim in outbound_claims
                ):
                    phase = "RESERVED"
        elif kind == EventKind.PULL_RUN_COMPLETED:
            assembled = event.payload.get("assembled_car_codes", [])
            run_mentioned = _message_references(event.message, planned_runs)
            car_assembled = isinstance(assembled, list) and (
                not assembled or code in {str(item) for item in assembled}
            )
            if run_mentioned and car_assembled:
                phase = "ASSEMBLED"
        elif kind == EventKind.TRAIN_DEPARTED and _message_references(event.message, assembled_outbounds):
            phase = "DEPARTED"
        if phase is not None:
            timeline.append(
                {
                    "at": event.at,
                    "phase": phase,
                    "via": str(kind),
                    "sequence": event.sequence,
                    "shift_code": event.shift_code,
                }
            )
    return timeline


def _move_history(workspace: Any, run_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for step in run_steps:
        run = workspace.runs.get(step["run_code"])
        history.append(
            {
                "verb": step["verb"],
                "label": _MOVE_VERB_LABEL.get(MoveVerb.parse(step["verb"]), step["verb"].lower()),
                "source": step["source_code"],
                "target": step["target_code"],
                "run_code": step["run_code"],
                "outbound_code": step["outbound_code"],
                "step_index": step["step_index"],
                "status": "EXECUTED" if step["executed"] else "PENDING",
                "run_state": step["run_state"],
                "planned_at": run.created_at if run is not None else None,
            }
        )
    history.sort(key=lambda item: (item["run_code"], item["step_index"]))
    return history


def _resolve_shift(workspace: Any, events: list[Any]) -> dict[str, Any]:
    shift_code: str | None = events[-1].shift_code if events else None
    if shift_code is None:
        open_shifts = [code for code, shift in workspace.shifts.items() if str(shift.state) == "OPEN"]
        shift_code = open_shifts[0] if open_shifts else None
    if shift_code is None:
        return {"code": None, "state": None, "dispatcher": None, "basis": "none"}
    shift = workspace.shifts.get(shift_code)
    if shift is None:
        return {"code": shift_code, "state": None, "dispatcher": None, "basis": "event"}
    return {
        "code": shift.code,
        "state": str(shift.state),
        "dispatcher": shift.dispatcher,
        "basis": "last_event" if events else "active_open_shift",
    }


def _ownership(
    intake_claims: list[dict[str, Any]],
    outbound_claims: list[dict[str, Any]],
    shift: dict[str, Any],
) -> dict[str, Any]:
    ticket: str | None = None
    active = [claim for claim in outbound_claims if claim["state"] in {"DRAFT", "PLANNED", "READY"}]
    assembled_active = [claim for claim in active if claim["assembled"]]
    planned_active = [claim for claim in active if claim["planned"]]
    if assembled_active:
        ticket = assembled_active[0]["code"]
    elif planned_active:
        ticket = planned_active[0]["code"]
    else:
        departed = [claim for claim in outbound_claims if claim["state"] == "DEPARTED" and claim["assembled"]]
        if departed:
            ticket = departed[0]["code"]
    return {
        "shift": shift,
        "intake_train": intake_claims[0]["code"] if intake_claims else None,
        "intake_state": intake_claims[0]["state"] if intake_claims else None,
        "ticket_outbound": ticket,
        "referenced_outbounds": [claim["code"] for claim in outbound_claims],
    }


def _plans(
    workspace: Any,
    outbound_claims: list[dict[str, Any]],
    run_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    tickets: list[dict[str, Any]] = []
    for claim in outbound_claims:
        train = workspace.outbounds.get(claim["code"])
        tickets.append(
            {
                "code": claim["code"],
                "destination": claim["destination"],
                "state": claim["state"],
                "planned_position": claim["planned_position"],
                "assembled_position": claim["assembled_position"],
                "run_codes": list(train.run_codes) if train is not None else [],
                "departed_at": train.departed_at if train is not None else None,
            }
        )

    run_codes = sorted({step["run_code"] for step in run_steps})
    runs: list[dict[str, Any]] = []
    active_run: dict[str, Any] | None = None
    for run_code in run_codes:
        run = workspace.runs.get(run_code)
        if run is None:
            continue
        car_steps = [step for step in run_steps if step["run_code"] == run_code]
        pending = [step for step in car_steps if not step["executed"]]
        summary = {
            "code": run.code,
            "outbound_code": run.outbound_code,
            "transfer_code": run.transfer_code,
            "state": str(run.state),
            "current_step": run.current_step,
            "total_steps": len(run.steps),
            "car_steps": len(car_steps),
            "pending_car_steps": len(pending),
            "next_car_action": pending[0]["verb"] if pending else None,
        }
        runs.append(summary)
        if active_run is None and run.state in {RunState.QUEUED, RunState.RUNNING}:
            active_run = summary

    return {
        "outbound_tickets": tickets,
        "pull_runs": runs,
        "active_pull_run": active_run,
    }


# ---------------------------------------------------------------------------
# Location block and yard cross-check
# ---------------------------------------------------------------------------


def _location_block(
    car: Any,
    physical: dict[str, Any],
    track_claims: list[dict[str, Any]],
    bay_claims: list[dict[str, Any]],
    outbound_claims: list[dict[str, Any]],
) -> dict[str, Any]:
    track = physical.get("track")
    bay = physical.get("bay")
    assembled = physical.get("assembled_on")
    return {
        "claimed": car.location,
        "observed_kind": physical["kind"],
        "observed": physical["code"],
        "standing_track": (
            {
                "code": track["code"],
                "state": track["state"],
                "purpose": track["purpose"],
                "index_from_top": track["index_from_top"],
                "stack_size": track["stack_size"],
            }
            if track
            else None
        ),
        "buffer_bay": (
            {
                "code": bay["code"],
                "index_from_top": bay["index_from_top"],
                "stack_size": bay["stack_size"],
            }
            if bay
            else None
        ),
        "outbound_train": (
            {
                "code": assembled["code"],
                "state": assembled["state"],
                "position": assembled["assembled_position"],
            }
            if assembled
            else None
        ),
        "all_references": {
            "standing_tracks": [claim["code"] for claim in track_claims],
            "buffer_bays": [claim["code"] for claim in bay_claims],
            "outbound_trains": [claim["code"] for claim in outbound_claims if claim["assembled"]],
        },
    }


def _physical_phase_counts(workspace: Any) -> dict[str, int]:
    """Count every car from physical references, not from its state field."""
    track_members: dict[str, list[str]] = {}
    for track in workspace.tracks.values():
        for item in track.stack:
            track_members.setdefault(item, []).append(track.code)
    bay_members: dict[str, list[str]] = {}
    for bay in workspace.buffer_bays.values():
        for item in bay.stack:
            bay_members.setdefault(item, []).append(bay.code)
    assembled_members: dict[str, list[str]] = {}
    for train in workspace.outbounds.values():
        for item in train.assembled_car_codes:
            assembled_members.setdefault(item, []).append(str(train.state))

    counts = {
        "received": 0,
        "standing": 0,
        "buffered": 0,
        "reserved": 0,
        "assembled": 0,
        "departed": 0,
        "removed": 0,
        "unknown": 0,
    }
    for code, car in workspace.cars.items():
        if car.state == CarState.REMOVED:
            counts["removed"] += 1
            continue
        train_states = assembled_members.get(code, [])
        if train_states:
            if "DEPARTED" in train_states:
                counts["departed"] += 1
            else:
                counts["assembled"] += 1
            continue
        if code in bay_members:
            counts["buffered"] += 1
            continue
        if code in track_members:
            if car.state == CarState.RESERVED:
                counts["reserved"] += 1
            else:
                counts["standing"] += 1
            continue
        if car.location == "INTAKE" or car.state == CarState.RECEIVED:
            counts["received"] += 1
        elif car.state == CarState.DEPARTED:
            counts["departed"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _yard_crosscheck(
    workspace: Any,
    car: Any,
    claimed_phase: str,
    observed_phase: str,
) -> dict[str, Any]:
    """Reconcile this car and the whole yard against GET /api/yard metrics."""
    metrics = yard_metrics(workspace)
    state_counts = dict(metrics["car_state_counts"])
    physical_counts = _physical_phase_counts(workspace)
    alerts: list[dict[str, Any]] = []

    # The yard overview counts STANDING cars by state field; buffered cars are
    # physically in a bay yet still state STANDING, so standing+buffered must
    # equal the overview's standing bucket.
    expected_state_standing = physical_counts["standing"] + physical_counts["buffered"]
    reconciliations = [
        ("received", physical_counts["received"]),
        ("standing", expected_state_standing),
        ("reserved", physical_counts["reserved"]),
        ("assembled", physical_counts["assembled"]),
        ("departed", physical_counts["departed"]),
        ("removed", physical_counts["removed"]),
    ]
    mismatches: list[dict[str, Any]] = []
    for bucket, physical_value in reconciliations:
        overview_value = state_counts.get(bucket, 0)
        if overview_value != physical_value:
            mismatches.append(
                {
                    "bucket": bucket,
                    "by_state_field": overview_value,
                    "by_physical_reference": physical_value,
                }
            )
    if mismatches:
        alerts.append(
            _alert(
                "yard-overview-mismatch",
                "yard overview state counts do not match physical references",
                source="yard",
                severity="critical",
                buckets=mismatches,
            )
        )

    observed_bucket = {
        PHASE_IN_YARD: "standing",
        PHASE_BUFFERED: "buffered",
        PHASE_RESERVED: "reserved",
        PHASE_ASSEMBLED: "assembled",
        PHASE_DEPARTED: "departed",
        PHASE_REMOVED: "removed",
    }.get(observed_phase)
    # RECEIVED cars show up as phase IN_YARD but live in the received bucket
    # on the overview.
    claimed_bucket = str(car.state).lower() if claimed_phase == PHASE_IN_YARD else claimed_phase.lower()

    return {
        "car_observed_phase": observed_phase,
        "car_claimed_phase": claimed_phase,
        "car_in_yard": observed_phase in IN_YARD_PHASES,
        "car_counted_by_state_in": claimed_bucket,
        "car_counted_physically_in": observed_bucket,
        "state_counts": state_counts,
        "physical_phase_counts": physical_counts,
        "active_intakes": metrics["active_intakes"],
        "active_outbounds": metrics["active_outbounds"],
        "active_runs": metrics["active_runs"],
        "open_shifts": metrics["open_shifts"],
        "version": metrics["version"],
        "alerts": alerts,
    }


def _alert(
    code: str,
    message: str,
    *,
    source: str,
    severity: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "source": source,
        "severity": severity,
        "details": details,
    }


__all__ = [
    "IN_YARD_PHASES",
    "PHASE_ASSEMBLED",
    "PHASE_BUFFERED",
    "PHASE_DEPARTED",
    "PHASE_IN_YARD",
    "PHASE_REMOVED",
    "PHASE_RESERVED",
    "PHASE_UNKNOWN",
    "build_car_view",
]
