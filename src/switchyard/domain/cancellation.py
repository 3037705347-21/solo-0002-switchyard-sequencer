"""Cancellation of an inbound train before classification completes.

A cancellation ends an OPEN or PARTIAL intake. Cars that never left the intake
flow and cars still standing on tracks (without an outbound commitment) are
removed from yard circulation: standing cars are popped from their track stack
and every affected car moves to the REMOVED state. Cars that already belong to
an outbound plan, assembly, or have departed stay exactly where they are, so
outbound work is never disturbed by an intake cancellation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .car import FreightCar
from .enums import CarState, IntakeState, OutboundState
from .errors import ConflictError, ResourceBusyError
from .intake import IntakeTrain
from .track import StandingTrack
from .transitions import transition_car, transition_intake

REMOVED_LOCATION = "REMOVED"
OUTCOME_REMOVED = "REMOVED"
OUTCOME_RETAINED = "RETAINED"


@dataclass(slots=True)
class CarDisposition:
    """Where a single consist car ended up after cancellation."""

    car_code: str
    outcome: str
    prior_state: str
    prior_location: str | None
    final_location: str | None
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "car_code": self.car_code,
            "outcome": self.outcome,
            "prior_state": self.prior_state,
            "prior_location": self.prior_location,
            "final_location": self.final_location,
            "detail": self.detail,
        }


def _committed_car_codes(workspace: Any) -> set[str]:
    """Cars referenced by an outbound train that has not terminated."""
    committed: set[str] = set()
    for outbound in workspace.outbounds.values():
        if outbound.state in {OutboundState.DEPARTED, OutboundState.ABANDONED}:
            continue
        committed.update(outbound.planned_car_codes)
        committed.update(outbound.assembled_car_codes)
    return committed


def cancel_intake(
    train: IntakeTrain,
    workspace: Any,
    reason: str,
) -> list[CarDisposition]:
    if train.state == IntakeState.CLASSIFIED:
        raise ConflictError(
            f"intake {train.code} is already classified and cannot be cancelled",
            intake_code=train.code,
            state=train.state.value,
        )
    if train.state == IntakeState.CANCELLED:
        raise ConflictError(
            f"intake {train.code} is already cancelled",
            intake_code=train.code,
            state=train.state.value,
        )
    if train.state not in {IntakeState.OPEN, IntakeState.PARTIAL}:
        raise ConflictError(
            f"intake {train.code} in state {train.state.value} cannot be cancelled",
            intake_code=train.code,
            state=train.state.value,
        )

    cars: dict[str, FreightCar] = workspace.cars
    tracks: dict[str, StandingTrack] = workspace.tracks
    bays = workspace.buffer_bays
    committed = _committed_car_codes(workspace)
    dispositions: list[CarDisposition] = []

    for code in train.consist:
        car = cars.get(code)
        prior_state = str(car.state) if car is not None else "MISSING"
        prior_location = car.location if car is not None else None

        if car is None:
            raise ResourceBusyError(
                f"cannot cancel intake {train.code}: consist car {code} is missing from the yard",
                car_code=code,
            )

        # A car parked in a transfer bay belongs to an in-progress pull run and
        # is physically mid-move; refuse instead of corrupting the run.
        if prior_location in bays:
            raise ResourceBusyError(
                f"cannot cancel intake {train.code}: car {code} is parked in transfer bay {prior_location}",
                car_code=code,
                bay_code=prior_location,
            )

        if code in committed or car.state in {
            CarState.RESERVED,
            CarState.ASSEMBLED,
            CarState.DEPARTED,
            CarState.REMOVED,
        }:
            dispositions.append(
                CarDisposition(
                    code,
                    OUTCOME_RETAINED,
                    prior_state,
                    prior_location,
                    prior_location,
                    detail=f"car is {prior_state} and stays outside intake control",
                )
            )
            continue

        if car.state == CarState.RECEIVED:
            transition_car(car, CarState.REMOVED, "intake cancelled before placement")
            car.location = REMOVED_LOCATION
        elif car.state == CarState.STANDING:
            track = tracks.get(prior_location or "")
            if track is None or code not in track.stack:
                raise ResourceBusyError(
                    f"cannot cancel intake {train.code}: car {code} is not stacked on its recorded track",
                    car_code=code,
                    location=prior_location,
                )
            # The yard already permits non-top administrative rollback (see
            # classify rollback); cars above remain in their stack order.
            track.stack.remove(code)
            transition_car(car, CarState.REMOVED, "intake cancelled after placement")
            car.location = REMOVED_LOCATION
        else:  # pragma: no cover - defensive: every state is handled above
            raise ResourceBusyError(
                f"cannot cancel intake {train.code}: car {code} is in state {prior_state}",
                car_code=code,
            )

        dispositions.append(
            CarDisposition(code, OUTCOME_REMOVED, prior_state, prior_location, REMOVED_LOCATION)
        )

    transition_intake(train, IntakeState.CANCELLED, reason)
    train.unplaced = []
    train.car_dispositions = [item.to_dict() for item in dispositions]
    return dispositions


__all__ = [
    "CarDisposition",
    "OUTCOME_REMOVED",
    "OUTCOME_RETAINED",
    "REMOVED_LOCATION",
    "cancel_intake",
]
