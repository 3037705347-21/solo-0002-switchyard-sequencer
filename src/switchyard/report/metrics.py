"""Read-only metrics derived from a persisted workspace."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, RunState, ShiftState


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
                "stack": list(track.stack),
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
    active_outbounds = [
        code
        for code, train in workspace.outbounds.items()
        if train.state.value not in {"DEPARTED", "ABANDONED"}
    ]
    active_runs = [code for code, run in workspace.runs.items() if run.state in {RunState.QUEUED, RunState.RUNNING}]
    active_reorders = [
        code for code, order in workspace.reorders.items() if order.state in {RunState.QUEUED, RunState.RUNNING}
    ]
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
        "transfer_bays": bay_metrics,
        "active_intakes": sorted(active_intakes),
        "active_outbounds": sorted(active_outbounds),
        "active_runs": sorted(active_runs),
        "active_reorder_orders": sorted(active_reorders),
        "open_shifts": sorted(open_shifts),
        "event_count": len(workspace.events),
        "version": workspace.version,
    }


def _percentage(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((value / total) * 100, 2)


__all__ = ["yard_metrics"]
