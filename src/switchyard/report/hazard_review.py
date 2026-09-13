"""Hazardous goods compliance review over a workspace snapshot.

The review is a pure derivation: it reads car hazard classes, load states,
track hazard ratings, destinations, and in-stack neighbors, then reports
findings without mutating the yard. Repeating a review over the same
workspace always returns the same document, and non-hazardous cars are
never flagged, whatever their codes or notes say.
"""

from __future__ import annotations

from typing import Any

from ..domain.car import FreightCar
from ..domain.enums import TrackPurpose
from ..domain.rules import hazard_rank
from ..domain.track import StandingTrack

SEVERITY_BLOCKING = "BLOCKING"
SEVERITY_WATCH = "WATCH"

RULE_UNRATED_TRACK = "HAZARD_ON_UNRATED_TRACK"
RULE_DESTINATION_MISMATCH = "HAZARD_DESTINATION_MISMATCH"
RULE_NEIGHBOR_NONHAZARD = "HAZARD_NEIGHBOR_NONHAZARD"
RULE_CLASS_MIX = "HAZARD_CLASS_MIX_NEIGHBOR"

RULE_BASIS = {
    RULE_UNRATED_TRACK: "hazardous cars require a hazard-rated track",
    RULE_DESTINATION_MISMATCH: "a hazardous car must match the destination of its destination track",
    RULE_NEIGHBOR_NONHAZARD: "a hazardous car next to a non-hazardous car needs duty review",
    RULE_CLASS_MIX: "adjacent hazardous cars must share one danger class",
}


def hazard_compliance_review(workspace: Any) -> dict[str, Any]:
    cars = workspace.cars
    findings: list[dict[str, Any]] = []
    hazard_cars = 0
    for track_code in sorted(workspace.tracks):
        track = workspace.tracks[track_code]
        stack = [cars.get(code) for code in track.stack]
        for index, car in enumerate(stack):
            if car is None or not car.is_hazardous():
                continue
            hazard_cars += 1
            findings.extend(_track_findings(track, car, index))
        for index in range(len(stack) - 1):
            lower = stack[index]
            upper = stack[index + 1]
            if lower is None or upper is None:
                continue
            finding = _neighbor_finding(track, lower, index, upper, index + 1)
            if finding is not None:
                findings.append(finding)
    findings.sort(key=_finding_sort_key)
    blocking = sum(1 for item in findings if item["severity"] == SEVERITY_BLOCKING)
    return {
        "summary": {
            "tracks_reviewed": len(workspace.tracks),
            "hazard_cars_on_tracks": hazard_cars,
            "finding_count": len(findings),
            "blocking_count": blocking,
            "watch_count": len(findings) - blocking,
        },
        "findings": findings,
    }


def _track_findings(track: StandingTrack, car: FreightCar, index: int) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not track.hazard_rated:
        severity = SEVERITY_BLOCKING if car.loaded else SEVERITY_WATCH
        load_text = "loaded" if car.loaded else "empty"
        findings.append(
            _finding(
                RULE_UNRATED_TRACK,
                severity,
                car,
                track,
                index,
                f"hazardous car {car.code} ({car.danger_class}, {load_text}) stands on unrated track {track.code}",
                {
                    "car_danger_class": car.danger_class,
                    "car_loaded": car.loaded,
                    "track_hazard_rated": track.hazard_rated,
                    "track_purpose": str(track.purpose),
                },
            )
        )
    if track.purpose == TrackPurpose.DESTINATION and track.destination != car.destination:
        findings.append(
            _finding(
                RULE_DESTINATION_MISMATCH,
                SEVERITY_BLOCKING,
                car,
                track,
                index,
                f"hazardous car {car.code} for {car.destination} stands on destination track {track.code} for {track.destination}",
                {
                    "car_danger_class": car.danger_class,
                    "car_destination": car.destination,
                    "track_destination": track.destination,
                    "track_purpose": str(track.purpose),
                },
            )
        )
    return findings


def _neighbor_finding(
    track: StandingTrack,
    lower: FreightCar,
    lower_index: int,
    upper: FreightCar,
    upper_index: int,
) -> dict[str, Any] | None:
    lower_hazard = lower.is_hazardous()
    upper_hazard = upper.is_hazardous()
    if not lower_hazard and not upper_hazard:
        return None
    if lower_hazard and upper_hazard:
        if lower.danger_class.upper() == upper.danger_class.upper():
            return None
        car, index, neighbor, neighbor_index = _higher_ranked(lower, lower_index, upper, upper_index)
        return _finding(
            RULE_CLASS_MIX,
            SEVERITY_WATCH,
            car,
            track,
            index,
            f"hazardous car {car.code} ({car.danger_class}) stands next to {neighbor.code} "
            f"({neighbor.danger_class}) on track {track.code}",
            {
                "car_danger_class": car.danger_class,
                "car_loaded": car.loaded,
                "neighbor_code": neighbor.code,
                "neighbor_danger_class": neighbor.danger_class,
                "neighbor_stack_index": neighbor_index,
                "track_hazard_rated": track.hazard_rated,
            },
        )
    if upper_hazard:
        car, index, neighbor, neighbor_index = upper, upper_index, lower, lower_index
    else:
        car, index, neighbor, neighbor_index = lower, lower_index, upper, upper_index
    return _finding(
        RULE_NEIGHBOR_NONHAZARD,
        SEVERITY_WATCH,
        car,
        track,
        index,
        f"hazardous car {car.code} ({car.danger_class}) stands next to non-hazardous car "
        f"{neighbor.code} on track {track.code}",
        {
            "car_danger_class": car.danger_class,
            "car_loaded": car.loaded,
            "neighbor_code": neighbor.code,
            "neighbor_danger_class": neighbor.danger_class,
            "neighbor_loaded": neighbor.loaded,
            "neighbor_stack_index": neighbor_index,
            "track_hazard_rated": track.hazard_rated,
        },
    )


def _higher_ranked(
    lower: FreightCar,
    lower_index: int,
    upper: FreightCar,
    upper_index: int,
) -> tuple[FreightCar, int, FreightCar, int]:
    if hazard_rank(upper.danger_class) >= hazard_rank(lower.danger_class):
        return upper, upper_index, lower, lower_index
    return lower, lower_index, upper, upper_index


def _finding(
    rule: str,
    severity: str,
    car: FreightCar,
    track: StandingTrack,
    stack_index: int,
    message: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "rule": rule,
        "severity": severity,
        "car_code": car.code,
        "track_code": track.code,
        "stack_index": stack_index,
        "message": message,
        "evidence": {"basis": RULE_BASIS[rule], **evidence},
    }


def _finding_sort_key(item: dict[str, Any]) -> tuple[str, int, str, str, str]:
    return (
        str(item["track_code"]),
        int(item["stack_index"]),
        str(item["car_code"]),
        str(item["rule"]),
        str(item["evidence"].get("neighbor_code", "")),
    )


__all__ = [
    "RULE_BASIS",
    "RULE_CLASS_MIX",
    "RULE_DESTINATION_MISMATCH",
    "RULE_NEIGHBOR_NONHAZARD",
    "RULE_UNRATED_TRACK",
    "SEVERITY_BLOCKING",
    "SEVERITY_WATCH",
    "hazard_compliance_review",
]
