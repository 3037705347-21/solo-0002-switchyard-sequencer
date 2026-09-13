"""Transfer line (buffer bay) selection for pull planning.

Dispatchers may either name the transfer line explicitly or leave the choice to
the sequencer. An automatic choice never silently changes the occupancy already
promised to existing plans: every queued or running pull run keeps its peak
buffer reservation on its selected line, and new plans only see what is left.
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import RunState
from .errors import ValidationError
from .pull import PullRun
from .track import BufferBay

AUTO = "AUTO"
EXPLICIT = "EXPLICIT"
VALID_MODES = (AUTO, EXPLICIT)


@dataclass(frozen=True, slots=True)
class BayUsage:
    """Capacity accounting for one transfer line at planning time."""

    capacity_cars: int
    occupied_cars: int
    reserved_cars: int

    @property
    def committed_cars(self) -> int:
        """Slots already promised: buffered cars plus active plan reservations.

        Running runs have both buffered cars on the stack and an outstanding
        reservation, so the two figures can overlap; the max is the safe
        commitment that never under-counts existing plans.
        """
        return max(self.occupied_cars, self.reserved_cars)

    @property
    def available_cars(self) -> int:
        return max(0, self.capacity_cars - self.committed_cars)


@dataclass(frozen=True, slots=True)
class TransferSelection:
    """The transfer line a pull run was bound to, with its audit trail."""

    transfer_code: str
    mode: str
    reason: str
    required_cars: int
    capacity_cars: int
    occupied_cars: int
    reserved_cars: int
    available_cars: int
    considered: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "transfer_code": self.transfer_code,
            "mode": self.mode,
            "reason": self.reason,
            "required_cars": self.required_cars,
            "capacity_cars": self.capacity_cars,
            "occupied_cars": self.occupied_cars,
            "reserved_cars": self.reserved_cars,
            "available_cars": self.available_cars,
            "considered": list(self.considered),
        }


def bay_usage(bay: BufferBay, runs: dict[str, PullRun]) -> BayUsage:
    """Committed slots on a bay from its stack and active plans.

    Only queued and running runs reserve capacity: completed runs have returned
    every buffered car, and failed plans are retried, not continued.
    """
    reserved = 0
    for run in runs.values():
        if run.transfer_code != bay.code:
            continue
        if run.state not in {RunState.QUEUED, RunState.RUNNING}:
            continue
        reserved += max(0, int(getattr(run, "required_cars", 0) or 0))
    return BayUsage(
        capacity_cars=bay.capacity_cars,
        occupied_cars=len(bay.stack),
        reserved_cars=reserved,
    )


def select_transfer(
    requested_code: str | None,
    required_cars: int,
    bays: dict[str, BufferBay],
    runs: dict[str, PullRun],
) -> TransferSelection:
    """Bind a new plan to a transfer line.

    An explicit code is validated strictly against existence and available
    capacity. Without a code, the stable rule picks the feasible line with the
    most available slots, breaking ties by line code; this rule is evaluated
    against current state on every request and therefore never re-binds or
    reduces the reservation recorded for an existing plan.
    """
    if not bays:
        raise ValidationError(
            "no transfer line is configured",
            fields={"transfer_code": ["yard has no transfer lines"]},
        )
    usage = {code: bay_usage(bay, runs) for code, bay in bays.items()}
    if requested_code is not None:
        bay = bays.get(requested_code)
        if bay is None:
            raise ValidationError(
                "unknown transfer line",
                fields={"transfer_code": ["not found"]},
            )
        used = usage[requested_code]
        if used.available_cars < required_cars:
            raise ValidationError(
                f"transfer line {requested_code} needs {required_cars} slots but "
                f"only {used.available_cars} are available",
                fields={
                    "transfer_code": [
                        f"capacity {used.capacity_cars}, committed {used.committed_cars}, "
                        f"available {used.available_cars}, required {required_cars}"
                    ]
                },
            )
        return TransferSelection(
            transfer_code=bay.code,
            mode=EXPLICIT,
            reason="explicitly requested by dispatcher",
            required_cars=required_cars,
            capacity_cars=used.capacity_cars,
            occupied_cars=used.occupied_cars,
            reserved_cars=used.reserved_cars,
            available_cars=used.available_cars,
            considered=(bay.code,),
        )

    ranked = sorted(
        bays.values(),
        key=lambda bay: (-usage[bay.code].available_cars, bay.code),
    )
    feasible = [bay for bay in ranked if usage[bay.code].available_cars >= required_cars]
    if not feasible:
        detail = ", ".join(
            f"{bay.code}: {usage[bay.code].available_cars} free" for bay in ranked
        )
        raise ValidationError(
            f"no transfer line has {required_cars} available slots",
            fields={"transfer_code": [f"none fits; {detail}"]},
        )
    chosen = feasible[0]
    used = usage[chosen.code]
    if required_cars == 0:
        reason = "no buffering required; selected the line with the most available slots"
    else:
        reason = (
            f"most available capacity ({used.available_cars} of {used.capacity_cars} slots)"
        )
    return TransferSelection(
        transfer_code=chosen.code,
        mode=AUTO,
        reason=reason,
        required_cars=required_cars,
        capacity_cars=used.capacity_cars,
        occupied_cars=used.occupied_cars,
        reserved_cars=used.reserved_cars,
        available_cars=used.available_cars,
        considered=tuple(bay.code for bay in ranked),
    )


__all__ = [
    "AUTO",
    "EXPLICIT",
    "VALID_MODES",
    "BayUsage",
    "TransferSelection",
    "bay_usage",
    "select_transfer",
]
