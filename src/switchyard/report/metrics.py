"""Read-only metrics derived from a persisted workspace."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, OutboundState, RunState, ShiftState

CAR_STATE_KEYS = (
    "received",
    "standing",
    "reserved",
    "assembled",
    "departed",
    "removed",
)

OUTBOUND_STATE_KEYS = (
    "draft",
    "planned",
    "ready",
    "departed",
    "abandoned",
)

RUN_STATE_KEYS = ("queued", "running", "completed", "failed")

OUTBOUND_OPEN_STATES = {
    OutboundState.DRAFT,
    OutboundState.PLANNED,
    OutboundState.READY,
}
RUN_UNFINISHED_STATES = {RunState.QUEUED, RunState.RUNNING, RunState.FAILED}


def yard_metrics(workspace: Any) -> dict[str, Any]:
    cars = workspace.cars
    car_lengths = {code: car.length_m for code, car in cars.items()}
    standing = sum(1 for car in cars.values() if car.state == CarState.STANDING)
    reserved = sum(1 for car in cars.values() if car.state == CarState.RESERVED)
    assembled = sum(1 for car in cars.values() if car.state == CarState.ASSEMBLED)
    departed = sum(1 for car in cars.values() if car.state == CarState.DEPARTED)
    received = sum(1 for car in cars.values() if car.state == CarState.RECEIVED)
    removed = sum(1 for car in cars.values() if car.state == CarState.REMOVED)
    track_metrics = []
    for code, track in sorted(workspace.tracks.items()):
        count = len(track.stack)
        length = sum(car_lengths.get(item, 0) for item in track.stack)
        track_metrics.append(
            {
                "code": code,
                "purpose": str(track.purpose),
                "state": str(track.state),
                "cars": count,
                "length_m": length,
                "capacity_cars": track.capacity_cars,
                "capacity_length_m": track.capacity_length_m,
                "car_utilization": _percentage(count, track.capacity_cars),
                "length_utilization": _percentage(length, track.capacity_length_m),
                "top_car": track.top_code(),
            }
        )
    bay_metrics = [
        {
            "code": bay.code,
            "cars": len(bay.stack),
            "capacity_cars": bay.capacity_cars,
            "top_car": bay.top_code(),
        }
        for bay in workspace.buffer_bays.values()
    ]
    active_intakes = [code for code, train in workspace.intakes.items() if train.state.value in {"OPEN", "PARTIAL"}]
    outbound_state_counts = {key: 0 for key in OUTBOUND_STATE_KEYS}
    open_outbounds: list[str] = []
    completed_outbounds: list[str] = []
    for code, train in sorted(workspace.outbounds.items()):
        outbound_state_counts[str(train.state).lower()] += 1
        if train.state in OUTBOUND_OPEN_STATES:
            open_outbounds.append(code)
        elif train.state == OutboundState.DEPARTED:
            completed_outbounds.append(code)
    run_state_counts = {key: 0 for key in RUN_STATE_KEYS}
    unfinished_runs: list[dict[str, Any]] = []
    for code, run in sorted(workspace.runs.items()):
        run_state_counts[str(run.state).lower()] += 1
        if run.state in RUN_UNFINISHED_STATES:
            unfinished_runs.append(
                {
                    "code": code,
                    "outbound_code": run.outbound_code,
                    "state": str(run.state),
                    "current_step": run.current_step,
                    "total_steps": len(run.steps),
                    "remaining_steps": run.remaining(),
                }
            )
    active_outbounds = [
        code
        for code, train in workspace.outbounds.items()
        if train.state.value not in {"DEPARTED", "ABANDONED"}
    ]
    active_runs = [code for code, run in workspace.runs.items() if run.state in {RunState.QUEUED, RunState.RUNNING}]
    open_shifts = [code for code, shift in workspace.shifts.items() if shift.state == ShiftState.OPEN]
    return {
        "total_cars": len(cars),
        "car_state_counts": {
            "received": received,
            "standing": standing,
            "reserved": reserved,
            "assembled": assembled,
            "departed": departed,
            "removed": removed,
        },
        "track_metrics": track_metrics,
        "track_occupancy": _track_occupancy(track_metrics),
        "transfer_bays": bay_metrics,
        "active_intakes": sorted(active_intakes),
        "active_outbounds": sorted(active_outbounds),
        "active_runs": sorted(active_runs),
        "open_outbounds": sorted(open_outbounds),
        "completed_outbounds": sorted(completed_outbounds),
        "outbound_state_counts": outbound_state_counts,
        "run_state_counts": run_state_counts,
        "unfinished_runs": unfinished_runs,
        "open_shifts": sorted(open_shifts),
        "event_count": len(workspace.events),
        "version": workspace.version,
    }


def _track_occupancy(track_metrics: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        item["code"]: {
            "state": item["state"],
            "cars": item["cars"],
            "length_m": item["length_m"],
            "capacity_cars": item["capacity_cars"],
            "capacity_length_m": item["capacity_length_m"],
            "car_utilization": item["car_utilization"],
            "length_utilization": item["length_utilization"],
            "top_car": item["top_car"],
        }
        for item in track_metrics
    }


def _percentage(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((value / total) * 100, 2)


__all__ = [
    "CAR_STATE_KEYS",
    "OUTBOUND_STATE_KEYS",
    "RUN_STATE_KEYS",
    "yard_metrics",
]
