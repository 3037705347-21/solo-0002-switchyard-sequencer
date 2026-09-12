"""Locate a car across every yard structure and build executable conflict lists.

A car can be referenced from several places at once: a standing track stack, a
transfer bay, an intake consist, an outbound planned/assembled consist, and the
remaining steps of a pull run. Deactivation must confirm the real attribution
before changing anything, so all scans live here in one place.
"""

from __future__ import annotations

from typing import Any

from .enums import CarState, IntakeState, OutboundState, RunState
ACTIVE_OUTBOUND_STATES = {OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.READY}
ACTIVE_RUN_STATES = {RunState.QUEUED, RunState.RUNNING}
OPEN_INTAKE_STATES = {IntakeState.OPEN, IntakeState.PARTIAL}

_CONFLICT_ORDER = {
    "departed_train": 0,
    "pull_run": 1,
    "buffer_bay": 2,
    "outbound_assembly": 3,
    "outbound_plan": 4,
    "intake": 5,
    "inconsistent_location": 6,
}


def _active_run_codes(workspace: Any) -> list[str]:
    return sorted(
        code
        for code, run in workspace.runs.items()
        if run.state in ACTIVE_RUN_STATES
    )


def _run_step_indexes(run: Any, car_code: str, only_remaining: bool) -> list[int]:
    start = run.current_step if only_remaining else 0
    return [index for index in range(start, len(run.steps)) if run.steps[index].car_code == car_code]

def locate_car(workspace: Any, car_code: str) -> dict[str, Any]:
    """Return every factual sighting of the car across the workspace."""

    car = workspace.cars.get(car_code)
    standing_track: dict[str, Any] | None = None
    for track_code, track in workspace.tracks.items():
        if car_code in track.stack:
            index = track.stack.index(car_code)
            standing_track = {
                "track_code": track_code,
                "stack_index": index,
                "depth_from_top": len(track.stack) - 1 - index,
            }
            break
    buffer_bay: dict[str, Any] | None = None
    for bay_code, bay in workspace.buffer_bays.items():
        if car_code in bay.stack:
            buffer_bay = {"bay_code": bay_code, "stack_index": bay.stack.index(car_code)}
            break
    intake_refs = [
        {"code": code, "state": str(train.state)}
        for code, train in sorted(workspace.intakes.items())
        if car_code in train.consist
    ]
    outbound_refs = []
    for code, train in sorted(workspace.outbounds.items()):
        if car_code in train.planned_car_codes or car_code in train.assembled_car_codes:
            outbound_refs.append(
                {
                    "code": code,
                    "state": str(train.state),
                    "in_planned": car_code in train.planned_car_codes,
                    "in_assembled": car_code in train.assembled_car_codes,
                }
            )
    pull_run_refs = []
    for code, run in sorted(workspace.runs.items()):
        indexes = _run_step_indexes(run, car_code, only_remaining=False)
        if indexes:
            pull_run_refs.append(
                {
                    "code": code,
                    "state": str(run.state),
                    "step_indexes": indexes,
                    "remaining_step_indexes": _run_step_indexes(run, car_code, True),
                }
            )
    return {
        "exists": car is not None,
        "car_state": None if car is None else str(car.state),
        "car_location": None if car is None else car.location,
        "standing_track": standing_track,
        "buffer_bay": buffer_bay,
        "intake_refs": intake_refs,
        "outbound_refs": outbound_refs,
        "pull_run_refs": pull_run_refs,
    }


def _advance_action(run: Any) -> dict[str, Any]:
    remaining = max(1, len(run.steps) - run.current_step)
    return {
                "action_required": f"advance pull run {run.code} to completion to release the car",
                "action_method": "POST",
                "action_path": f"/api/pull-runs/{run.code}/advance",
                "action_payload": {"steps": remaining},
            }


def _conflict(item: dict[str, Any]) -> dict[str, Any]:
    item.setdefault("action_method", None)
    item.setdefault("action_path", None)
    item.setdefault("action_payload", None)
    return item


def collect_conflicts(workspace: Any, car_code: str) -> list[dict[str, Any]]:
    """Build the deterministic, executable conflict list for deactivation.

    An empty result means the car is free to be withdrawn. Conflicts are
    derived from persisted references only; nothing is mutated here.
    """

    car = workspace.cars.get(car_code)
    if car is None:
        return []
    sightings = locate_car(workspace, car_code)
    conflicts: list[dict[str, Any]] = []

    active_runs = [workspace.runs[code] for code in _active_run_codes(workspace)]

    # A running pull run is physically moving cars between track, bay and
    # assembly. Once it has started, "advance the run" is the single executable
    # resolution, so bay/outbound sightings owned by that run must not add a
    # competing abandon action. A queued (not yet started) run has not moved
    # anything, so its outbound plan remains abandonable.
    in_progress_runs = [run for run in active_runs if run.state == RunState.RUNNING]
    run_owned_outbounds: set[str] = set()
    run_owned_bays: set[str] = set()
    for run in in_progress_runs:
        indexes = _run_step_indexes(run, car_code, only_remaining=True)
        if not indexes:
            continue
        outbound = workspace.outbounds.get(run.outbound_code)
        if outbound is not None and (
            car_code in outbound.planned_car_codes or car_code in outbound.assembled_car_codes
        ):
            run_owned_outbounds.add(run.outbound_code)
        for step in run.steps[run.current_step :]:
            if step.car_code != car_code:
                continue
            if step.target_code in workspace.buffer_bays or step.source_code in workspace.buffer_bays:
                bay_code = step.target_code if step.target_code in workspace.buffer_bays else step.source_code
                run_owned_bays.add(bay_code)

    for run in active_runs:
        indexes = _run_step_indexes(run, car_code, only_remaining=True)
        if not indexes:
            continue
        conflicts.append(
            _conflict(
                {
                    "kind": "pull_run",
                    "reference": run.code,
                    "message": (
                        f"pull run {run.code} is {run.state.value} and still moves car {car_code} "
                        f"at remaining step(s) {indexes}"
                    ),
                    **_advance_action(run),
                }
            )
        )

    if car.state == CarState.DEPARTED:
        departed_outbound = None
        for ref in sightings["outbound_refs"]:
            if ref["state"] == str(OutboundState.DEPARTED) and ref["in_assembled"]:
                departed_outbound = ref["code"]
                break
        conflicts.append(
            _conflict(
                {
                    "kind": "departed_train",
                    "reference": departed_outbound or car.location or "DEPARTED",
                    "message": (
                        f"car {car_code} already departed with outbound {departed_outbound}"
                        if departed_outbound
                        else f"car {car_code} has already departed the yard"
                    ),
                    "action_required": "departed cars are sealed to their train and cannot be withdrawn",
                }
            )
        )

    # A car parked in a transfer bay is a dangling physical reference. When an
    # active run is responsible, its advance conflict already covers it.
    bay = sightings["buffer_bay"]
    if bay is not None and car.state != CarState.DEPARTED and bay["bay_code"] not in run_owned_bays:
        conflicts.append(
            _conflict(
                {
                    "kind": "buffer_bay",
                    "reference": bay["bay_code"],
                    "message": f"car {car_code} is parked in transfer bay {bay['bay_code']}",
                    "action_required": (
                        f"return car {car_code} from bay {bay['bay_code']} to its source track"
                    ),
                    "action_method": None,
                    "action_path": None,
                    "action_payload": None,
                }
            )
        )

    for ref in sightings["outbound_refs"]:
        state = ref["state"]
        outbound_code = ref["code"]
        if outbound_code in run_owned_outbounds:
            continue
        if state == str(OutboundState.DEPARTED):
            if car.state != CarState.DEPARTED:
                conflicts.append(
                    _conflict(
                        {
                            "kind": "departed_train",
                            "reference": outbound_code,
                            "message": f"car {car_code} is sealed to departed outbound {outbound_code}",
                            "action_required": "departed trains cannot be modified",
                        }
                    )
                )
            continue
        if state == str(OutboundState.ABANDONED):
            continue
        if state not in {item.value for item in ACTIVE_OUTBOUND_STATES}:
            continue
        if ref["in_assembled"]:
            conflicts.append(
                _conflict(
                    {
                        "kind": "outbound_assembly",
                        "reference": outbound_code,
                        "message": f"car {car_code} is assembled on outbound {outbound_code} ({state})",
                        "action_required": (
                            f"depart outbound {outbound_code}, or abandon it to release assembled cars"
                        ),
                        "action_method": "POST",
                        "action_path": f"/api/outbound-trains/{outbound_code}/abandon",
                        "action_payload": {},
                    }
                )
            )
        elif ref["in_planned"]:
            if state == str(OutboundState.DRAFT):
                hint = "abandon the draft to release the car"
            else:
                hint = f"abandon outbound {outbound_code} to release the reservation"
            conflicts.append(
                _conflict(
                    {
                        "kind": "outbound_plan",
                        "reference": outbound_code,
                        "message": f"car {car_code} is reserved by outbound {outbound_code} ({state})",
                        "action_required": hint,
                        "action_method": "POST",
                        "action_path": f"/api/outbound-trains/{outbound_code}/abandon",
                        "action_payload": {},
                    }
                )
            )

    for ref in sightings["intake_refs"]:
        state = ref["state"]
        if state not in {item.value for item in OPEN_INTAKE_STATES}:
            continue
        intake_code = ref["code"]
        if state == str(IntakeState.OPEN):
            conflicts.append(
                _conflict(
                    {
                        "kind": "intake",
                        "reference": intake_code,
                        "message": f"car {car_code} is still on the unclassified intake {intake_code}",
                        "action_required": f"cancel intake {intake_code} to remove the car, or classify it first",
                        "action_method": "POST",
                        "action_path": f"/api/intake-trains/{intake_code}/cancel",
                        "action_payload": {},
                    }
                )
            )
        else:
            conflicts.append(
                _conflict(
                    {
                        "kind": "intake",
                        "reference": intake_code,
                        "message": f"car {car_code} is unplaced on partially classified intake {intake_code}",
                        "action_required": f"retry classification for intake {intake_code} to place the car",
                        "action_method": "POST",
                        "action_path": f"/api/intake-trains/{intake_code}/classify",
                        "action_payload": {},
                    }
                )
            )

    if (
        car.state in {CarState.STANDING, CarState.RESERVED}
        and sightings["standing_track"] is None
        and sightings["buffer_bay"] is None
    ):
        conflicts.append(
            _conflict(
                {
                    "kind": "inconsistent_location",
                    "reference": car_code,
                    "message": (
                        f"car {car_code} is {car.state.value} but is present on no track or bay"
                    ),
                    "action_required": "repair the car location before deactivation",
                }
            )
        )

    conflicts.sort(key=lambda item: (_CONFLICT_ORDER[item["kind"]], item["reference"]))
    return conflicts


__all__ = ["ACTIVE_OUTBOUND_STATES", "ACTIVE_RUN_STATES", "collect_conflicts", "locate_car"]
