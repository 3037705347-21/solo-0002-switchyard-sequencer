"""Persisted-state inventory detail for the yard view.

Unlike :mod:`switchyard.report.metrics`, which only reports counts and the top
car of each stack, this module reconstructs *where every car physically sits*
straight from the persisted workspace:

* ``track``   - cars inside a standing track LIFO stack (reserved cars that have
                not been pulled yet are still physically on the stack and are
                flagged rather than counted a second time);
* ``buffer``  - cars parked in a transfer bay (X1) while a deeper car is pulled;
* ``outbound``- cars already assembled onto an outbound train consist;
* ``intake``  - cars that have been received but never classified;
* ``departed``/``removed`` - cars no longer physically in the yard.

The four physical buckets are mutually exclusive: each car is located through
exactly one container, so the same car number can never be counted twice.
Stacks are always emitted bottom-to-top (pull order, i.e. first-to-pull first),
which is the order dispatch and handoff crews read the track.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import CarState, OutboundState


def _car_entry(car: Any) -> dict[str, Any]:
    return {
        "code": car.code,
        "kind": str(car.kind),
        "destination": car.destination,
        "loaded": car.loaded,
        "length_m": car.length_m,
        "danger_class": car.danger_class,
        "state": str(car.state),
    }


def _reservations(workspace: Any) -> dict[str, str]:
    """Map reserved/planned car code -> outbound code from persisted plans."""
    reserved_for: dict[str, str] = {}
    for outbound in workspace.outbounds.values():
        if outbound.state in {OutboundState.DRAFT, OutboundState.PLANNED, OutboundState.READY}:
            for code in outbound.planned_car_codes:
                reserved_for.setdefault(code, outbound.code)
    return reserved_for


def yard_inventory(workspace: Any) -> dict[str, Any]:
    cars = workspace.cars
    reserved_for = _reservations(workspace)

    # First pass: register the physical container of every car we can locate.
    # placement[code] is one of track/buffer/outbound; anything missing is
    # reconciled later from the car state itself.
    placement: dict[str, str] = {}
    container_of: dict[str, str] = {}
    track_detail: list[dict[str, Any]] = []
    for track_code in sorted(workspace.tracks):
        track = workspace.tracks[track_code]
        stack_detail: list[dict[str, Any]] = []
        # Persisted stack is bottom ... top. Bottom-to-top is the natural
        # read order for sorting work, pre-assembly checks, and handoff.
        for position, code in enumerate(track.stack):
            car = cars.get(code)
            entry = _car_entry(car) if car is not None else {"code": code, "state": "UNKNOWN"}
            entry["position"] = position
            entry["from_bottom"] = position
            entry["from_top"] = len(track.stack) - 1 - position
            entry["reserved"] = code in reserved_for
            entry["reserved_for"] = reserved_for.get(code)
            stack_detail.append(entry)
            placement[code] = "track"
            container_of[code] = track_code
        track_detail.append(
            {
                "code": track.code,
                "purpose": str(track.purpose),
                "state": str(track.state),
                "destination": track.destination,
                "hazard_rated": track.hazard_rated,
                "capacity_cars": track.capacity_cars,
                "capacity_length_m": track.capacity_length_m,
                "order": "bottom-to-top",
                "car_count": len(stack_detail),
                "bottom_car": stack_detail[0]["code"] if stack_detail else None,
                "top_car": track.top_code(),
                "cars": stack_detail,
            }
        )

    buffer_detail: list[dict[str, Any]] = []
    for bay_code in sorted(workspace.buffer_bays):
        bay = workspace.buffer_bays[bay_code]
        stack_detail: list[dict[str, Any]] = []
        for position, code in enumerate(bay.stack):
            car = cars.get(code)
            entry = _car_entry(car) if car is not None else {"code": code, "state": "UNKNOWN"}
            entry["position"] = position
            entry["from_bottom"] = position
            entry["from_top"] = len(bay.stack) - 1 - position
            stack_detail.append(entry)
            placement[code] = "buffer"
            container_of[code] = bay.code
        buffer_detail.append(
            {
                "code": bay.code,
                "capacity_cars": bay.capacity_cars,
                "order": "bottom-to-top",
                "car_count": len(stack_detail),
                "bottom_car": stack_detail[0]["code"] if stack_detail else None,
                "top_car": bay.top_code(),
                "cars": stack_detail,
            }
        )

    outbound_detail: list[dict[str, Any]] = []
    assembled_in_yard = 0
    for outbound_code in sorted(workspace.outbounds):
        outbound = workspace.outbounds[outbound_code]
        # A departed/abandoned train keeps its consist as a historical record
        # but its cars are no longer physically in the yard (they are counted
        # in the departed bucket), so only active trains claim placements.
        in_yard = outbound.state not in {OutboundState.DEPARTED, OutboundState.ABANDONED}
        consist_detail: list[dict[str, Any]] = []
        # Assembled consist order is head-to-tail in planned departure order.
        for position, code in enumerate(outbound.assembled_car_codes):
            car = cars.get(code)
            entry = _car_entry(car) if car is not None else {"code": code, "state": "UNKNOWN"}
            entry["position"] = position
            entry["in_yard"] = in_yard
            consist_detail.append(entry)
            if in_yard:
                placement[code] = "outbound"
                container_of[code] = outbound.code
                assembled_in_yard += 1
        assembled_set = set(outbound.assembled_car_codes)
        pending_codes = [code for code in outbound.planned_car_codes if code not in assembled_set]
        pending_detail: list[dict[str, Any]] = []
        for code in pending_codes:
            pending_detail.append(
                {
                    "code": code,
                    "state": str(cars[code].state) if code in cars else "UNKNOWN",
                    "location_kind": placement.get(code, "unlocated"),
                    "location": container_of.get(code, cars[code].location if code in cars else None),
                }
            )
        assembled_in_yard_for_train = len(consist_detail) if in_yard else 0
        outbound_detail.append(
            {
                "code": outbound.code,
                "destination": outbound.destination,
                "state": str(outbound.state),
                "in_yard": in_yard,
                "order": "head-to-tail",
                "planned_car_codes": list(outbound.planned_car_codes),
                "assembled_count": assembled_in_yard_for_train,
                "recorded_assembled_count": len(consist_detail),
                "pending_count": len(pending_detail),
                "assembled_cars": consist_detail,
                "pending_cars": pending_detail,
            }
        )

    # Second pass: every car that was not found in a physical container is
    # reconciled purely from its persisted state (received/departed/removed),
    # so totals always partition the full car registry.
    intake_detail: list[dict[str, Any]] = []
    departed_codes: list[str] = []
    removed_codes: list[str] = []
    for code in sorted(cars):
        car = cars[code]
        if code in placement:
            continue
        if car.state == CarState.RECEIVED:
            intake_detail.append(
                {
                    "code": code,
                    "destination": car.destination,
                    "length_m": car.length_m,
                    "state": str(car.state),
                    "location": car.location,
                }
            )
        elif car.state == CarState.DEPARTED:
            departed_codes.append(code)
        elif car.state == CarState.REMOVED:
            removed_codes.append(code)

    buckets = {
        "on_tracks": sum(item["car_count"] for item in track_detail),
        "in_buffers": sum(item["car_count"] for item in buffer_detail),
        "assembled": assembled_in_yard,
        "pending_intake": len(intake_detail),
        "departed": len(departed_codes),
        "removed": len(removed_codes),
    }

    anomalies = _reconcile(workspace, placement, container_of, reserved_for)

    return {
        "tracks": track_detail,
        "transfer_bays": buffer_detail,
        "outbound_trains": outbound_detail,
        "pending_intake_cars": intake_detail,
        "departed_cars": departed_codes,
        "removed_cars": removed_codes,
        "buckets": buckets,
        "total_cars": len(cars),
        "anomalies": anomalies,
    }


def _reconcile(
    workspace: Any,
    placement: dict[str, str],
    container_of: dict[str, str],
    reserved_for: dict[str, str],
) -> list[dict[str, Any]]:
    """Cross-check physical placement against each car's persisted state."""
    anomalies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for track in workspace.tracks.values():
        for code in track.stack:
            if code in seen:
                anomalies.append({"kind": "duplicate-placement", "car_code": code})
            seen.add(code)
            car = workspace.cars.get(code)
            if car is None:
                anomalies.append({"kind": "unknown-car", "car_code": code, "location": track.code})
                continue
            if car.state not in {CarState.STANDING, CarState.RESERVED}:
                anomalies.append(
                    {
                        "kind": "state-location-mismatch",
                        "car_code": code,
                        "state": str(car.state),
                        "location": track.code,
                    }
                )
            if car.state == CarState.RESERVED and code not in reserved_for:
                anomalies.append({"kind": "missing-reservation", "car_code": code, "location": track.code})
            if car.state == CarState.STANDING and code in reserved_for:
                anomalies.append({"kind": "unexpected-reservation", "car_code": code, "location": track.code})
    for bay in workspace.buffer_bays.values():
        for code in bay.stack:
            if code in seen:
                anomalies.append({"kind": "duplicate-placement", "car_code": code})
            seen.add(code)
            car = workspace.cars.get(code)
            if car is None:
                anomalies.append({"kind": "unknown-car", "car_code": code, "location": bay.code})
                continue
            if car.state != CarState.STANDING:
                anomalies.append(
                    {
                        "kind": "state-location-mismatch",
                        "car_code": code,
                        "state": str(car.state),
                        "location": bay.code,
                    }
                )
    for outbound in workspace.outbounds.values():
        if outbound.state in {OutboundState.DEPARTED, OutboundState.ABANDONED}:
            # Consist is retained only as a historical record; the cars are
            # departed/removed and reconciled through the car registry instead.
            continue
        for code in outbound.assembled_car_codes:
            if code in seen:
                anomalies.append({"kind": "duplicate-placement", "car_code": code})
            seen.add(code)
            car = workspace.cars.get(code)
            if car is None:
                anomalies.append({"kind": "unknown-car", "car_code": code, "location": outbound.code})
                continue
            if car.state != CarState.ASSEMBLED:
                anomalies.append(
                    {
                        "kind": "state-location-mismatch",
                        "car_code": code,
                        "state": str(car.state),
                        "location": outbound.code,
                    }
                )
    for code, car in workspace.cars.items():
        if code not in seen and car.state in {CarState.STANDING, CarState.RESERVED, CarState.ASSEMBLED}:
            anomalies.append(
                {
                    "kind": "missing-placement",
                    "car_code": code,
                    "state": str(car.state),
                    "recorded_location": car.location,
                }
            )
    return anomalies


__all__ = ["yard_inventory"]
