"""Encoding and decoding for the workspace root object."""

from __future__ import annotations

from typing import Any

from ..domain.car import FreightCar
from ..domain.intake import IntakeTrain
from ..domain.outbound import OutboundTrain
from ..domain.pull import PullRun, YardEvent
from ..domain.shift import YardShift
from ..domain.track import BufferBay, StandingTrack


def encode_workspace(workspace: Any) -> dict[str, Any]:
    return {
        "schema_version": workspace.schema_version,
        "version": workspace.version,
        "next_event_sequence": workspace.next_event_sequence,
        "tracks": [track.to_dict() for track in workspace.tracks.values()],
        "buffer_bays": [bay.to_dict() for bay in workspace.buffer_bays.values()],
        "cars": [car.to_dict() for car in workspace.cars.values()],
        "intakes": [train.to_dict() for train in workspace.intakes.values()],
        "outbounds": [train.to_dict() for train in workspace.outbounds.values()],
        "pull_runs": [run.to_dict() for run in workspace.runs.values()],
        "shifts": [shift.to_dict() for shift in workspace.shifts.values()],
        "events": [event.to_dict() for event in workspace.events],
        "closure_snapshots": workspace.closure_snapshots,
        "commits": list(workspace.commits),
    }


def decode_workspace(raw: dict[str, Any]) -> Any:
    from .workspace import YardWorkspace

    tracks = {str(item["code"]): StandingTrack.from_dict(item) for item in raw.get("tracks", [])}
    bays = {str(item["code"]): BufferBay.from_dict(item) for item in raw.get("buffer_bays", [])}
    cars = {str(item["code"]): FreightCar.from_dict(item) for item in raw.get("cars", [])}
    intakes = {str(item["code"]): IntakeTrain.from_dict(item) for item in raw.get("intakes", [])}
    outbounds = {str(item["code"]): OutboundTrain.from_dict(item) for item in raw.get("outbounds", [])}
    runs = {str(item["code"]): PullRun.from_dict(item) for item in raw.get("pull_runs", [])}
    shifts = {str(item["code"]): YardShift.from_dict(item) for item in raw.get("shifts", [])}
    events = [YardEvent.from_dict(item) for item in raw.get("events", [])]
    workspace = YardWorkspace(
        tracks=tracks,
        buffer_bays=bays,
        cars=cars,
        intakes=intakes,
        outbounds=outbounds,
        runs=runs,
        shifts=shifts,
        events=events,
    )
    workspace.schema_version = int(raw.get("schema_version", workspace.schema_version))
    workspace.version = int(raw.get("version", 1))
    workspace.next_event_sequence = int(raw.get("next_event_sequence", workspace.next_event_sequence))
    workspace.closure_snapshots = list(raw.get("closure_snapshots", []))
    workspace.commits = [dict(item) for item in raw.get("commits", [])]
    return workspace


__all__ = ["decode_workspace", "encode_workspace"]
