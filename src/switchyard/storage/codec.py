"""Encoding and decoding for the workspace root object."""

from __future__ import annotations

from typing import Any

from ..domain.car import FreightCar
from ..domain.intake import IntakeTrain
from ..domain.outbound import OutboundTrain
from ..domain.pull import PullRun, YardEvent
from ..domain.shift import YardShift
from ..domain.track import BufferBay, StandingTrack
from ..domain.transfer import TransferReservation


def encode_workspace(workspace: Any) -> dict[str, Any]:
    return {
        "schema_version": workspace.schema_version,
        "version": workspace.version,
        "next_event_sequence": workspace.next_event_sequence,
        "tracks": [track.to_dict() for track in workspace.tracks.values()],
        "buffer_bays": [bay.to_dict() for bay in workspace.buffer_bays.values()],
        "transfer_reservations": [
            reservation.to_dict() for reservation in workspace.transfer_reservations.values()
        ],
        "cars": [car.to_dict() for car in workspace.cars.values()],
        "intakes": [train.to_dict() for train in workspace.intakes.values()],
        "outbounds": [train.to_dict() for train in workspace.outbounds.values()],
        "pull_runs": [run.to_dict() for run in workspace.runs.values()],
        "shifts": [shift.to_dict() for shift in workspace.shifts.values()],
        "events": [event.to_dict() for event in workspace.events],
        "closure_snapshots": workspace.closure_snapshots,
    }


def decode_workspace(raw: dict[str, Any]) -> Any:
    from .workspace import YardWorkspace

    tracks = {str(item["code"]): StandingTrack.from_dict(item) for item in raw.get("tracks", [])}
    bays = {str(item["code"]): BufferBay.from_dict(item) for item in raw.get("buffer_bays", [])}
    # Legacy state files predate registration ordering; assign a deterministic
    # order (sorted by code) so multi-line scheduling stays reproducible.
    if bays and all(bay.registered_order == 0 for bay in bays.values()):
        for index, code in enumerate(sorted(bays), start=1):
            bays[code].registered_order = index
    cars = {str(item["code"]): FreightCar.from_dict(item) for item in raw.get("cars", [])}
    intakes = {str(item["code"]): IntakeTrain.from_dict(item) for item in raw.get("intakes", [])}
    outbounds = {str(item["code"]): OutboundTrain.from_dict(item) for item in raw.get("outbounds", [])}
    runs = {str(item["code"]): PullRun.from_dict(item) for item in raw.get("pull_runs", [])}
    shifts = {str(item["code"]): YardShift.from_dict(item) for item in raw.get("shifts", [])}
    reservations = {
        str(item["run_code"]): TransferReservation.from_dict(item)
        for item in raw.get("transfer_reservations", [])
    }
    events = [YardEvent.from_dict(item) for item in raw.get("events", [])]
    workspace = YardWorkspace(
        tracks=tracks,
        buffer_bays=bays,
        transfer_reservations=reservations,
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
    return workspace


__all__ = ["decode_workspace", "encode_workspace"]
