"""Deterministic seed tracks and buffer bays for a fresh yard."""

from __future__ import annotations

from ..domain.enums import TrackPurpose, TrackState
from ..domain.track import BufferBay, StandingTrack
from .workspace import YardWorkspace


def seed_tracks() -> list[StandingTrack]:
    return [
        StandingTrack("N4-A", TrackPurpose.DESTINATION, 10, 300, destination="N4"),
        StandingTrack("E7-A", TrackPurpose.DESTINATION, 10, 300, destination="E7"),
        StandingTrack("S2-A", TrackPurpose.DESTINATION, 10, 300, destination="S2"),
        StandingTrack("W9-A", TrackPurpose.DESTINATION, 10, 300, destination="W9"),
        StandingTrack("MIX-1", TrackPurpose.GENERAL, 18, 500, state=TrackState.OPERATIONAL),
        StandingTrack("HAZ-1", TrackPurpose.GENERAL, 8, 240, hazard_rated=True),
        StandingTrack("MAINT-1", TrackPurpose.GENERAL, 6, 180, state=TrackState.MAINTENANCE),
    ]


def seed_bays() -> list[BufferBay]:
    return [BufferBay("X1", 10)]


def build_seed_workspace() -> YardWorkspace:
    workspace = YardWorkspace()
    for track in seed_tracks():
        workspace.tracks[track.code] = track
    for bay in seed_bays():
        workspace.buffer_bays[bay.code] = bay
    return workspace


__all__ = ["build_seed_workspace", "seed_bays", "seed_tracks"]
