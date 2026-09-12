"""Encoding and decoding for the workspace root object."""

from __future__ import annotations

from typing import Any

from ..domain.car import FreightCar
from ..domain.dispatch import DispatchTicket
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
        "dispatch_tickets": [ticket.to_dict() for ticket in workspace.dispatch_tickets.values()],
        "next_dispatch_order": workspace.next_dispatch_order,
        "events": [event.to_dict() for event in workspace.events],
        "closure_snapshots": workspace.closure_snapshots,
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
    tickets = {
        str(item["code"]): DispatchTicket.from_dict(item) for item in raw.get("dispatch_tickets", [])
    }
    events = [YardEvent.from_dict(item) for item in raw.get("events", [])]
    workspace = YardWorkspace(
        tracks=tracks,
        buffer_bays=bays,
        cars=cars,
        intakes=intakes,
        outbounds=outbounds,
        runs=runs,
        shifts=shifts,
        dispatch_tickets=tickets,
        events=events,
    )
    workspace.schema_version = int(raw.get("schema_version", workspace.schema_version))
    workspace.version = int(raw.get("version", 1))
    workspace.next_event_sequence = int(raw.get("next_event_sequence", workspace.next_event_sequence))
    workspace.next_dispatch_order = int(
        raw.get("next_dispatch_order", _next_dispatch_order(tickets))
    )
    workspace.closure_snapshots = list(raw.get("closure_snapshots", []))
    return workspace


def _next_dispatch_order(tickets: dict[str, DispatchTicket]) -> int:
    if not tickets:
        return 1
    return max(ticket.queue_order for ticket in tickets.values()) + 1


__all__ = ["decode_workspace", "encode_workspace"]
