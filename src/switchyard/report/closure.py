"""Closure blocker collection for shift handoff."""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, OutboundState, RunState, TrackState


def closure_blockers(workspace: Any) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for code, train in workspace.intakes.items():
        if train.state.value in {"OPEN", "PARTIAL"}:
            blockers.append(
                {
                    "code": code,
                    "kind": "intake",
                    "message": f"intake {code} is still {train.state.value}",
                }
            )
    for code, train in workspace.outbounds.items():
        if train.state not in {OutboundState.DEPARTED, OutboundState.ABANDONED}:
            blockers.append(
                {
                    "code": code,
                    "kind": "outbound",
                    "message": f"outbound {code} has not departed",
                }
            )
    for code, run in workspace.runs.items():
        if run.state in {RunState.QUEUED, RunState.RUNNING}:
            blockers.append(
                {
                    "code": code,
                    "kind": "pull_run",
                    "message": f"pull run {code} is {run.state.value}",
                }
            )
    for code, track in workspace.tracks.items():
        if track.state == TrackState.MAINTENANCE and track.stack:
            blockers.append(
                {
                    "code": code,
                    "kind": "maintenance_track",
                    "message": f"maintenance track {code} still holds cars",
                }
            )
    for code, car in workspace.cars.items():
        if car.state == CarState.RECEIVED:
            blockers.append(
                {
                    "code": code,
                    "kind": "unclassified_car",
                    "message": f"car {code} was never classified",
                }
            )
    return blockers


__all__ = ["closure_blockers"]
