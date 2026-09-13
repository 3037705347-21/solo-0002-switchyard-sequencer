"""Read-only closure precheck: the blocker list with resolution guidance."""

from __future__ import annotations

from typing import Any, Callable

from ..domain.enums import OutboundState, RunState
from .closure import closure_blockers


def closure_precheck(workspace: Any) -> list[dict[str, Any]]:
    """Return closure blockers enriched for dispatcher preview.

    Items derive from the same scan the close command uses, so the preview
    always matches a real closure attempt. Each item names the related
    object, its current state, and the next step that clears it. The report
    is computed from the workspace snapshot and never mutates it.
    """
    report: list[dict[str, Any]] = []
    for order, blocker in enumerate(closure_blockers(workspace), start=1):
        kind = str(blocker["kind"])
        code = str(blocker["code"])
        enricher = _ENRICHERS.get(kind, _unknown_step)
        state, related, next_step = enricher(workspace, code)
        report.append(
            {
                "order": order,
                "kind": kind,
                "code": code,
                "message": blocker["message"],
                "state": state,
                "related": related,
                "next_step": next_step,
            }
        )
    return report


def _intake_step(workspace: Any, code: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    train = workspace.intakes[code]
    related = {
        "route": train.route,
        "consist_cars": len(train.consist),
        "unplaced_cars": list(train.unplaced),
    }
    next_step = {
        "action": "classify_intake",
        "endpoint": f"POST /api/intake-trains/{code}/classify",
        "detail": f"classify intake {code} so every received car is placed",
    }
    return str(train.state), related, next_step


def _outbound_step(workspace: Any, code: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    train = workspace.outbounds[code]
    related = {
        "destination": train.destination,
        "planned_car_codes": list(train.planned_car_codes),
        "assembled_car_codes": list(train.assembled_car_codes),
        "run_codes": list(train.run_codes),
    }
    if train.state == OutboundState.DRAFT:
        next_step = {
            "action": "sequence_outbound",
            "endpoint": f"POST /api/outbound-trains/{code}/sequencer",
            "detail": f"plan a pull run for outbound {code}",
        }
    elif train.state == OutboundState.READY:
        next_step = {
            "action": "depart_outbound",
            "endpoint": f"POST /api/outbound-trains/{code}/depart",
            "detail": f"depart outbound {code} for {train.destination}",
        }
    else:
        run_code = _active_run_code(workspace, train)
        if run_code is None:
            next_step = {
                "action": "review_pull_run",
                "endpoint": None,
                "detail": f"review the pull runs for outbound {code} before closing",
            }
        else:
            next_step = {
                "action": "advance_pull_run",
                "endpoint": f"POST /api/pull-runs/{run_code}/advance",
                "detail": f"advance pull run {run_code} until outbound {code} is ready",
            }
    return str(train.state), related, next_step


def _active_run_code(workspace: Any, train: Any) -> str | None:
    for run_code in train.run_codes:
        run = workspace.runs.get(run_code)
        if run is not None and run.state in {RunState.QUEUED, RunState.RUNNING}:
            return run_code
    return None


def _pull_run_step(workspace: Any, code: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    run = workspace.runs[code]
    related = {
        "outbound_code": run.outbound_code,
        "transfer_code": run.transfer_code,
        "current_step": run.current_step,
        "total_steps": len(run.steps),
        "remaining_steps": run.remaining(),
    }
    next_step = {
        "action": "advance_pull_run",
        "endpoint": f"POST /api/pull-runs/{code}/advance",
        "detail": f"advance pull run {code} through its remaining {run.remaining()} step(s)",
    }
    return str(run.state), related, next_step


def _maintenance_track_step(workspace: Any, code: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    track = workspace.tracks[code]
    related = {"cars": list(track.stack), "car_count": len(track.stack)}
    cars_text = ", ".join(track.stack)
    next_step = {
        "action": "clear_maintenance_track",
        "endpoint": None,
        "detail": f"move cars {cars_text} off track {code} and release it from maintenance",
    }
    return str(track.state), related, next_step


def _unclassified_car_step(workspace: Any, code: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    car = workspace.cars[code]
    intake_code = _owning_intake_code(workspace, code)
    related = {
        "intake_code": intake_code,
        "location": car.location,
        "destination": car.destination,
    }
    if intake_code is None:
        next_step = {
            "action": "place_car",
            "endpoint": None,
            "detail": f"place car {code} onto a standing track or remove it from the yard",
        }
    else:
        next_step = {
            "action": "classify_intake",
            "endpoint": f"POST /api/intake-trains/{intake_code}/classify",
            "detail": f"classify intake {intake_code} to place car {code}",
        }
    return str(car.state), related, next_step


def _owning_intake_code(workspace: Any, car_code: str) -> str | None:
    for code, train in workspace.intakes.items():
        if car_code in train.consist and not train.is_terminal():
            return code
    for code, train in workspace.intakes.items():
        if car_code in train.consist:
            return code
    return None


def _unknown_step(workspace: Any, code: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return "UNKNOWN", {}, {"action": "review", "endpoint": None, "detail": f"review {code} before closing"}


_ENRICHERS: dict[str, Callable[[Any, str], tuple[str, dict[str, Any], dict[str, Any]]]] = {
    "intake": _intake_step,
    "outbound": _outbound_step,
    "pull_run": _pull_run_step,
    "maintenance_track": _maintenance_track_step,
    "unclassified_car": _unclassified_car_step,
}

__all__ = ["closure_precheck"]
