"""Pull run advancement, move recording, reconciliation, and departure commands."""

from __future__ import annotations

from typing import Any

from ..domain.enums import AssemblyStatus, CarState, EventKind, OutboundState, RunState
from ..domain.errors import ConflictError, NotFoundError, ResourceBusyError, ValidationError
from ..domain.executor import execute_step
from ..domain.pull import MoveRecord, MoveStep, PullRun
from ..domain.reconciliation import AssemblyReport, reconcile_assembly
from ..domain.timeutil import now_iso
from ..domain.transitions import transition_car, transition_outbound, transition_run
from ..domain.validators import build_move_payload, parse_advance_steps
from .context import YardApplication


def _ensure_shift_open(workspace: Any) -> str:
    for shift in workspace.shifts.values():
        if str(shift.state) == "OPEN":
            return shift.code
    raise ResourceBusyError("no open shift", message_hint="open a shift before moving cars")


def _append_move_record(run: PullRun, step: MoveStep, origin: str) -> MoveRecord:
    record = MoveRecord(
        index=len(run.actual_moves),
        verb=step.verb,
        car_code=step.car_code,
        source_code=step.source_code,
        target_code=step.target_code,
        origin=origin,
        at=now_iso(),
    )
    run.actual_moves.append(record)
    return record


def _divergence_event(workspace: Any, shift_code: str, run: PullRun, report: AssemblyReport) -> Any:
    return workspace.record_event(
        shift_code,
        EventKind.ASSEMBLY_DIVERGED,
        f"pull run {run.code} diverges from plan at position {report.first_deviation_position}",
        {
            "discrepancies": [item.to_dict() for item in report.discrepancies],
            "first_deviation_position": report.first_deviation_position,
        },
    )


def _complete_if_aligned(
    workspace: Any,
    shift_code: str,
    run: PullRun,
    outbound: Any,
    report: AssemblyReport,
    events: list[Any],
) -> bool:
    """Complete the run only when every planned step executed and the consist aligns.

    A diverged run stays RUNNING: the completed physical actions are kept for
    manual review and later correction moves can bring the consist back in
    line, at which point reconciliation recomputes clean.
    """
    if run.current_step < len(run.steps):
        return False
    if report.status != AssemblyStatus.ALIGNED:
        events.append(_divergence_event(workspace, shift_code, run, report))
        return False
    run.state = RunState.COMPLETED
    run.completed_at = now_iso()
    transition_outbound(outbound, OutboundState.READY)
    events.append(
        workspace.record_event(
            shift_code,
            EventKind.PULL_RUN_COMPLETED,
            f"pull run {run.code} completed",
            {
                "assembled_car_codes": list(outbound.assembled_car_codes),
                "steps": len(run.steps),
                "reconciliation": str(report.status),
            },
        )
    )
    return True


def advance_run(app: YardApplication, run_code: str, payload: Any) -> dict[str, Any]:
    requested_steps = parse_advance_steps(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    if run.state == RunState.COMPLETED:
        raise ConflictError("pull run is already complete", code=run_code)
    if run.state == RunState.FAILED:
        raise ConflictError("pull run has failed", code=run_code)
    events: list[Any] = []
    if run.state == RunState.QUEUED:
        transition_run(run, RunState.RUNNING)
        run.started_at = now_iso()
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_STARTED,
                f"pull run {run_code} started",
                {"total_steps": len(run.steps)},
            )
        )
    outbound = workspace.outbounds.get(run.outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", run.outbound_code)
    executed = 0
    report: AssemblyReport | None = None
    while executed < requested_steps and run.current_step < len(run.steps):
        step = run.steps[run.current_step]
        execute_step(workspace, run, step)
        _append_move_record(run, step, "plan")
        run.current_step += 1
        executed += 1
        report = reconcile_assembly(outbound, run, final=run.current_step >= len(run.steps))
        if report.status == AssemblyStatus.DIVERGED:
            # Stop the batch at the action where the consist starts to
            # diverge; the completed physical actions are kept for review.
            break
    if report is None:
        report = reconcile_assembly(outbound, run, final=run.current_step >= len(run.steps))
    completed = _complete_if_aligned(workspace, shift_code, run, outbound, report, events)
    if run.current_step < len(run.steps):
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_ADVANCED,
                f"pull run {run_code} advanced {executed} steps",
                {
                    "current_step": run.current_step,
                    "remaining": run.remaining(),
                    "reconciliation": str(report.status),
                },
            )
        )
        if report.status == AssemblyStatus.DIVERGED:
            events.append(_divergence_event(workspace, shift_code, run, report))
    app.commit(workspace, events)
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "executed_steps": executed,
        "completed": completed,
        "reconciliation": report.to_dict(),
    }


def record_move(app: YardApplication, run_code: str, payload: Any) -> dict[str, Any]:
    """Record a physical shunting move reported by the crew.

    The move is validated against the physical yard state and applied like a
    planned step, but it does not advance the planned cursor. It is appended
    to the run's move log so deviations and corrections stay auditable.
    """
    step = build_move_payload(payload)
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    if run.state in {RunState.COMPLETED, RunState.FAILED}:
        raise ConflictError("pull run is finished", code=run_code)
    events: list[Any] = []
    if run.state == RunState.QUEUED:
        transition_run(run, RunState.RUNNING)
        run.started_at = now_iso()
        events.append(
            workspace.record_event(
                shift_code,
                EventKind.PULL_RUN_STARTED,
                f"pull run {run_code} started",
                {"total_steps": len(run.steps)},
            )
        )
    execute_step(workspace, run, step)
    record = _append_move_record(run, step, "report")
    outbound = workspace.outbounds.get(run.outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", run.outbound_code)
    report = reconcile_assembly(outbound, run, final=run.current_step >= len(run.steps))
    events.append(
        workspace.record_event(
            shift_code,
            EventKind.MOVE_RECORDED,
            f"recorded {step.verb} {step.car_code} from {step.source_code} to {step.target_code}",
            {"move": record.to_dict(), "reconciliation": str(report.status)},
        )
    )
    completed = _complete_if_aligned(workspace, shift_code, run, outbound, report, events)
    if not completed and run.current_step < len(run.steps) and report.status == AssemblyStatus.DIVERGED:
        events.append(_divergence_event(workspace, shift_code, run, report))
    app.commit(workspace, events)
    return {
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
        "completed": completed,
        "reconciliation": report.to_dict(),
    }


def get_reconciliation(app: YardApplication, run_code: str) -> dict[str, Any]:
    workspace = app.load()
    run = workspace.runs.get(run_code)
    if run is None:
        raise NotFoundError("pull run", run_code)
    outbound = workspace.outbounds.get(run.outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", run.outbound_code)
    report = reconcile_assembly(outbound, run, final=run.current_step >= len(run.steps))
    return {
        "reconciliation": report.to_dict(),
        "pull_run": run.to_dict(),
        "outbound": outbound.to_dict(),
    }


def depart_outbound(app: YardApplication, outbound_code: str) -> dict[str, Any]:
    workspace = app.load()
    shift_code = _ensure_shift_open(workspace)
    outbound = workspace.outbounds.get(outbound_code)
    if outbound is None:
        raise NotFoundError("outbound train", outbound_code)
    if outbound.state != OutboundState.READY:
        raise ValidationError(
            "outbound train is not ready",
            **{"outbound_code": [f"current state is {outbound.state.value}"]},
        )
    run = workspace.runs.get(outbound.run_codes[-1]) if outbound.run_codes else None
    if run is not None:
        report = reconcile_assembly(outbound, run, final=True)
        if report.status != AssemblyStatus.ALIGNED:
            raise ConflictError(
                "outbound assembly diverges from the pull plan",
                discrepancies=[item.to_dict() for item in report.discrepancies],
                first_deviation_position=report.first_deviation_position,
            )
    if not outbound.assembly_complete():
        raise ValidationError("outbound assembly is incomplete", **{"assembled": outbound.assembled_car_codes})
    for code in outbound.assembled_car_codes:
        car = workspace.cars.get(code)
        if car is None or car.state != CarState.ASSEMBLED:
            raise ValidationError(
                f"assembled car {code} is not in assembled state",
                **{"assembled": [code]},
            )
    departed_at = now_iso()
    transition_outbound(outbound, OutboundState.DEPARTED)
    outbound.departed_at = departed_at
    for code in outbound.assembled_car_codes:
        transition_car(workspace.cars[code], CarState.DEPARTED)
    event = workspace.record_event(
        shift_code,
        EventKind.TRAIN_DEPARTED,
        f"outbound {outbound.code} departed for {outbound.destination}",
        {"car_count": len(outbound.assembled_car_codes), "departed_at": departed_at},
    )
    app.commit(workspace, event)
    return {
        "outbound": outbound.to_dict(),
        "departed_car_count": len(outbound.assembled_car_codes),
    }


__all__ = ["advance_run", "depart_outbound", "get_reconciliation", "record_move"]
