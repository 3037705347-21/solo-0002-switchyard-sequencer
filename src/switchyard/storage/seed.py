"""Deterministic seed tracks and buffer bays for a fresh yard."""

from __future__ import annotations

import os
import re

from ..domain.enums import TrackPurpose, TrackState
from ..domain.track import BufferBay, StandingTrack
from .workspace import YardWorkspace

BAY_SPEC = re.compile(r"^\s*([A-Z][A-Z0-9]{0,8})\s*:\s*(\d{1,3})\s*$")


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
    """Seed transfer lines, optionally extended via SWITCHYARD_TRANSFER_BAYS.

    The default yard keeps the single X1 line. To add future transfer lines
    for a fresh data directory, set the variable to comma-separated
    ``CODE:CAPACITY`` entries, e.g. ``X1:10,X2:6``. Naming X1 overrides its
    default capacity; other codes add lines.
    """
    by_code: dict[str, BufferBay] = {"X1": BufferBay("X1", 10)}
    raw_spec = os.environ.get("SWITCHYARD_TRANSFER_BAYS", "").strip()
    if not raw_spec:
        return list(by_code.values())
    for item in raw_spec.split(","):
        match = BAY_SPEC.match(item)
        if match is None:
            raise ValueError(f"invalid SWITCHYARD_TRANSFER_BAYS entry {item!r}; expected CODE:CAPACITY")
        code, capacity_text = match.groups()
        capacity = int(capacity_text)
        if capacity <= 0:
            raise ValueError(f"invalid SWITCHYARD_TRANSFER_BAYS entry {item!r}; capacity must be positive")
        by_code[code] = BufferBay(code, capacity)
    return [by_code[code] for code in sorted(by_code)]


def build_seed_workspace() -> YardWorkspace:
    workspace = YardWorkspace()
    for track in seed_tracks():
        workspace.tracks[track.code] = track
    for bay in seed_bays():
        workspace.buffer_bays[bay.code] = bay
    return workspace


__all__ = ["build_seed_workspace", "seed_bays", "seed_tracks"]
