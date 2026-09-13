"""Transfer line capacity scheduling.

A transfer line (modeled by :class:`~switchyard.domain.track.BufferBay`) is a
short LIFO lead that buffers the non-target cars sitting above a planned car.
Several transfer lines may be registered, and every pull ticket must be placed
on a single line that can hold its *peak* concurrent demand for the whole
duration of the run.

Capacity accounting keeps two distinct numbers apart:

``physical``
    Cars physically on the line right now (the bay stack). Always matches the
    live vehicle positions.

``committed``
    Slots promised to *queued* pull runs, computed from each run's step shape
    (the maximum number of cars that run buffers at once). A queued run has no
    cars on the line yet, but its slots are reserved so two plans cannot be
    sold the same capacity.

A *running* run is charged as ``max(its cars physically on the line, its
remaining peak demand)``, so a partially executed, interrupted, or mid-return
run keeps holding exactly the slots it may still need. Committed occupancy is
derived from the persisted runs on every reconciliation rather than trusted
blindly, which is what lets a restart, a cancellation, or a partial execution
recompute holds that agree with the yard positions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import MoveVerb, RunState, TrackState
from .errors import DomainError
from .pull import MoveStep, PullRun
from .timeutil import now_iso
from .track import BufferBay

# Lifecycle of a tracked occupancy reservation.
HOLD_ACTIVE = "HOLDING"
HOLD_RELEASED = "RELEASED"

# Stable machine-readable reasons a line cannot serve a ticket.
REASON_OCCUPIED = "existing_occupancy"
REASON_LINE_LIMIT = "single_line_limit"
REASON_UNAVAILABLE = "line_unavailable"
REASON_UNKNOWN = "unknown_line"

ACTIVE_RUN_STATES = {RunState.QUEUED, RunState.RUNNING}


@dataclass(slots=True)
class TransferReservation:
    """Audit trail of capacity promised to (and used by) one pull run."""

    run_code: str
    outbound_code: str
    transfer_code: str
    required_slots: int
    observed_peak_slots: int = 0
    state: str = HOLD_ACTIVE
    selected_slack: int = 0
    created_at: str = field(default_factory=now_iso)
    released_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_code": self.run_code,
            "outbound_code": self.outbound_code,
            "transfer_code": self.transfer_code,
            "required_slots": self.required_slots,
            "observed_peak_slots": self.observed_peak_slots,
            "state": self.state,
            "selected_slack": self.selected_slack,
            "created_at": self.created_at,
            "released_at": self.released_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "TransferReservation":
        return cls(
            run_code=str(raw["run_code"]),
            outbound_code=str(raw.get("outbound_code", "")),
            transfer_code=str(raw.get("transfer_code", "")),
            required_slots=int(raw.get("required_slots", 0)),
            observed_peak_slots=int(raw.get("observed_peak_slots", 0)),
            state=str(raw.get("state", HOLD_ACTIVE)),
            selected_slack=int(raw.get("selected_slack", 0)),
            created_at=str(raw.get("created_at", "")),
            released_at=None if raw.get("released_at") is None else str(raw["released_at"]),
        )


@dataclass(slots=True)
class HeldBy:
    run_code: str
    outbound_code: str
    run_state: str
    held_slots: int
    physical_slots: int

    def to_dict(self) -> dict[str, object]:
        return {
            "run_code": self.run_code,
            "outbound_code": self.outbound_code,
            "run_state": self.run_state,
            "held_slots": self.held_slots,
            "physical_slots": self.physical_slots,
        }


@dataclass(slots=True)
class LineCapacity:
    """Read-only capacity view for one transfer line."""

    code: str
    state: str
    capacity_cars: int
    physical_cars: int
    committed_cars: int
    available_cars: int
    registered_order: int
    held_by: list[HeldBy] = field(default_factory=list)
    orphan_cars: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "state": self.state,
            "available": self.state == TrackState.OPERATIONAL.value,
            "capacity_cars": self.capacity_cars,
            "physical_cars": self.physical_cars,
            "committed_cars": self.committed_cars,
            "available_cars": self.available_cars,
            "registered_order": self.registered_order,
            "orphan_cars": self.orphan_cars,
            "held_by": [item.to_dict() for item in self.held_by],
        }


@dataclass(slots=True)
class LineEvaluation:
    """Result of testing one line against a ticket."""

    code: str
    feasible: bool
    reason: str | None
    capacity_cars: int
    physical_cars: int
    committed_cars: int
    available_cars: int
    required_slots: int
    slack: int
    registered_order: int


def step_delta(step: MoveStep) -> int:
    """Change in transfer-line occupancy caused by one step."""
    if step.verb == MoveVerb.BUFFER:
        return 1
    if step.verb == MoveVerb.RETURN:
        return -1
    return 0


def peak_occupancy(steps: list[MoveStep]) -> int:
    """Peak number of cars the steps put on a transfer line at once."""
    level = 0
    peak = 0
    for step in steps:
        level += step_delta(step)
        if level > peak:
            peak = level
    return max(0, peak)


def projected_peak_occupancy(run: PullRun, current_level: int) -> int:
    """Full peak occupancy the run still reaches, replayed from now.

    The remaining steps are replayed starting from ``current_level`` (the
    cars physically attributable to this run on the line right now), not from
    zero. A peak-2 ticket that has buffered one car still issues another
    BUFFER before any RETURN, so its level climbs back to 2 and the run must
    keep holding 2 slots. Taking only the peak *additional* demand would drop
    the hold to 1 after the first BUFFER and wrongly admit another plan.
    """
    level = max(0, current_level)
    peak = level
    for step in run.steps[run.current_step :]:
        level += step_delta(step)
        if level > peak:
            peak = level
    return max(0, peak)


def _attributable_cars(run: PullRun, bay: BufferBay) -> int:
    """Cars physically on ``bay`` that this running run is responsible for.

    Conservative on purpose: every car the run has buffered but not yet
    returned counts, intersected with the cars actually present on the line.
    """
    buffered_not_returned = set()
    for index, step in enumerate(run.steps[: run.current_step]):
        if step.verb == MoveVerb.BUFFER:
            buffered_not_returned.add(step.car_code)
        elif step.verb == MoveVerb.RETURN:
            buffered_not_returned.discard(step.car_code)
    on_line = set(bay.stack)
    return len(buffered_not_returned & on_line)


def _run_held_slots(run: PullRun, bay: BufferBay) -> tuple[int, int]:
    """Return (committed hold, physical cars) charged to an active run."""
    if run.state == RunState.QUEUED:
        return peak_occupancy(run.steps), 0
    physical = _attributable_cars(run, bay)
    # Replay the remaining steps from the cars physically on the line so the
    # hold tracks the full peak still ahead, not just the next net delta.
    return projected_peak_occupancy(run, physical), physical


def line_capacities(
    bays: dict[str, BufferBay],
    runs: dict[str, PullRun],
    reservations: dict[str, TransferReservation] | None = None,
) -> dict[str, LineCapacity]:
    """Pure capacity view derived from live runs and bay positions."""
    result: dict[str, LineCapacity] = {}
    for code, bay in bays.items():
        active = [
            run
            for run in runs.values()
            if run.state in ACTIVE_RUN_STATES and run.transfer_code == code
        ]
        held_by: list[HeldBy] = []
        committed = 0
        attributed_physical = 0
        for run in sorted(active, key=lambda item: item.code):
            held, physical = _run_held_slots(run, bay)
            committed += held
            attributed_physical += physical
            held_by.append(
                HeldBy(
                    run_code=run.code,
                    outbound_code=run.outbound_code,
                    run_state=str(run.state),
                    held_slots=held,
                    physical_slots=physical,
                )
            )
        physical_total = len(bay.stack)
        orphan = max(0, physical_total - attributed_physical)
        # Physical cars must always be covered by capacity too; a running line
        # with no active runs (manual move) still occupies real slots.
        charge = max(committed, physical_total)
        available = max(0, bay.capacity_cars - charge) if bay.can_operate() else 0
        result[code] = LineCapacity(
            code=code,
            state=str(bay.state),
            capacity_cars=bay.capacity_cars,
            physical_cars=physical_total,
            committed_cars=charge,
            available_cars=available,
            registered_order=bay.registered_order,
            held_by=held_by,
            orphan_cars=orphan,
        )
    return result


def _evaluation(code: str, required: int, capacity: LineCapacity | None, bay: BufferBay | None) -> LineEvaluation:
    if capacity is None:
        return LineEvaluation(
            code=code,
            feasible=False,
            reason=REASON_UNKNOWN,
            capacity_cars=0,
            physical_cars=0,
            committed_cars=0,
            available_cars=0,
            required_slots=required,
            slack=0,
            registered_order=(bay.registered_order if bay is not None else 0),
        )
    if capacity.state != TrackState.OPERATIONAL.value:
        reason = REASON_UNAVAILABLE
        feasible = False
    elif required > capacity.capacity_cars:
        reason = REASON_LINE_LIMIT
        feasible = False
    elif required > capacity.available_cars:
        reason = REASON_OCCUPIED
        feasible = False
    else:
        reason = None
        feasible = True
    return LineEvaluation(
        code=code,
        feasible=feasible,
        reason=reason,
        capacity_cars=capacity.capacity_cars,
        physical_cars=capacity.physical_cars,
        committed_cars=capacity.committed_cars,
        available_cars=capacity.available_cars,
        required_slots=required,
        slack=capacity.available_cars - required,
        registered_order=capacity.registered_order,
    )


def evaluate_lines(
    required: int,
    bays: dict[str, BufferBay],
    runs: dict[str, PullRun],
    reservations: dict[str, TransferReservation] | None = None,
    candidate_codes: list[str] | None = None,
) -> list[LineEvaluation]:
    capacities = line_capacities(bays, runs, reservations)
    codes = candidate_codes if candidate_codes is not None else list(bays.keys())
    return [_evaluation(code, required, capacities.get(code), bays.get(code)) for code in codes]


_SELECTION_KEY: Any = (
    lambda item: (
        item.slack,
        -item.committed_cars,
        -item.physical_cars,
        item.capacity_cars,
        item.registered_order,
        item.code,
    )
)


def select_transfer_line(
    required: int,
    bays: dict[str, BufferBay],
    runs: dict[str, PullRun],
    preferred_code: str | None = None,
    reservations: dict[str, TransferReservation] | None = None,
) -> tuple[str | None, LineEvaluation, list[LineEvaluation]]:
    """Choose the line for a ticket.

    A feasible line is one that is operational and whose *currently free*
    slots cover the ticket's peak demand. Among feasible lines the choice is
    the deterministic best-fit ordering: tightest remaining slack after
    placement (pack the small line first), then highest committed load, then
    highest physical load, then smallest single-line capacity, then the stable
    registration order, and finally the line code. Name or dict iteration
    order alone never decides the outcome.

    With an explicit ``preferred_code`` the ticket is admitted against that
    line only; its classified failure reason is reported otherwise.

    Returns ``(chosen_code, chosen_evaluation, all_evaluations)``.
    """
    codes = [preferred_code] if preferred_code is not None else sorted(
        bays.keys(), key=lambda item: (bays[item].registered_order, item)
    )
    evaluations = evaluate_lines(required, bays, runs, reservations, codes)
    feasible = [item for item in evaluations if item.feasible]
    if not feasible:
        return None, evaluations[0], evaluations
    chosen = min(feasible, key=_SELECTION_KEY)
    return chosen.code, chosen, evaluations


def reconcile_transfer_occupancy(
    workspace: Any,
) -> dict[str, LineCapacity]:
    """Rebuild reservation audit records from persisted runs.

    Active runs get a HOLDING reservation (created if missing, e.g. after a
    restart with an older state file); terminal runs are marked RELEASED.
    Observed physical peaks of running runs are refreshed from bay positions.
    The returned capacity view is the source of truth for planning; the
    reservations only provide traceability across restarts.
    """
    reservations: dict[str, TransferReservation] = workspace.transfer_reservations
    capacities = line_capacities(workspace.buffer_bays, workspace.runs, reservations)
    active_codes = set()
    for run in workspace.runs.values():
        if run.state not in ACTIVE_RUN_STATES:
            reservation = reservations.get(run.code)
            if reservation is not None and reservation.state == HOLD_ACTIVE:
                reservation.state = HOLD_RELEASED
                reservation.released_at = now_iso()
            continue
        active_codes.add(run.code)
        view = capacities.get(run.transfer_code)
        held = 0
        physical = 0
        if view is not None:
            for entry in view.held_by:
                if entry.run_code == run.code:
                    held = entry.held_slots
                    physical = entry.physical_slots
                    break
        required = peak_occupancy(run.steps)
        reservation = reservations.get(run.code)
        if reservation is None:
            reservation = TransferReservation(
                run_code=run.code,
                outbound_code=run.outbound_code,
                transfer_code=run.transfer_code,
                required_slots=required,
                observed_peak_slots=max(held, physical),
            )
            reservations[run.code] = reservation
        else:
            reservation.transfer_code = run.transfer_code
            reservation.outbound_code = run.outbound_code
            reservation.state = HOLD_ACTIVE
            reservation.released_at = None
            reservation.observed_peak_slots = max(
                reservation.observed_peak_slots, held, physical
            )
    for code, reservation in list(reservations.items()):
        if reservation.state == HOLD_ACTIVE and code not in active_codes:
            reservation.state = HOLD_RELEASED
            reservation.released_at = now_iso()
    return capacities


def record_reservation(
    workspace: Any,
    run: PullRun,
    required: int,
    slack: int,
) -> TransferReservation:
    reservation = TransferReservation(
        run_code=run.code,
        outbound_code=run.outbound_code,
        transfer_code=run.transfer_code,
        required_slots=required,
        observed_peak_slots=0,
        state=HOLD_ACTIVE,
        selected_slack=slack,
    )
    workspace.transfer_reservations[run.code] = reservation
    return reservation


def release_reservation(workspace: Any, run_code: str) -> TransferReservation | None:
    reservation = workspace.transfer_reservations.get(run_code)
    if reservation is not None and reservation.state == HOLD_ACTIVE:
        reservation.state = HOLD_RELEASED
        reservation.released_at = now_iso()
    return reservation


def refresh_observed_peaks(workspace: Any) -> None:
    capacities = line_capacities(
        workspace.buffer_bays, workspace.runs, workspace.transfer_reservations
    )
    for run in workspace.runs.values():
        if run.state != RunState.RUNNING:
            continue
        reservation = workspace.transfer_reservations.get(run.code)
        view = capacities.get(run.transfer_code)
        if reservation is None or view is None:
            continue
        physical = 0
        for entry in view.held_by:
            if entry.run_code == run.code:
                physical = entry.physical_slots
                break
        reservation.observed_peak_slots = max(reservation.observed_peak_slots, physical)


class TransferCapacityError(DomainError):
    """No transfer line can serve the whole ticket, with per-line reasons."""

    def __init__(self, required: int, evaluations: list[LineEvaluation], requested_code: str | None = None):
        lines = [
            {
                "code": item.code,
                "feasible": item.feasible,
                "reason": item.reason,
                "required_slots": item.required_slots,
                "capacity_cars": item.capacity_cars,
                "physical_cars": item.physical_cars,
                "committed_cars": item.committed_cars,
                "available_cars": item.available_cars,
            }
            for item in evaluations
        ]
        if requested_code is not None and evaluations:
            item = evaluations[0]
            message = _reason_message(item)
        else:
            message = (
                f"no transfer line can serve the ticket peak of {required} car(s)"
            )
        super().__init__(
            message,
            code="TRANSFER_CAPACITY",
            status=422,
            payload={
                "required_slots": required,
                "requested_transfer_code": requested_code,
                "lines": lines,
            },
        )


def _reason_message(item: LineEvaluation) -> str:
    if item.reason == REASON_UNAVAILABLE:
        return f"transfer line {item.code} is unavailable"
    if item.reason == REASON_UNKNOWN:
        return f"transfer line {item.code} is not registered"
    if item.reason == REASON_LINE_LIMIT:
        return (
            f"ticket needs {item.required_slots} slot(s) but transfer line {item.code} "
            f"has a single-line limit of {item.capacity_cars}"
        )
    if item.reason == REASON_OCCUPIED:
        return (
            f"transfer line {item.code} has {item.available_cars} free of "
            f"{item.capacity_cars} slot(s); ticket needs {item.required_slots}"
        )
    return f"transfer line {item.code} cannot serve the ticket"


__all__ = [
    "HOLD_ACTIVE",
    "HOLD_RELEASED",
    "REASON_LINE_LIMIT",
    "REASON_OCCUPIED",
    "REASON_UNAVAILABLE",
    "REASON_UNKNOWN",
    "HeldBy",
    "LineCapacity",
    "LineEvaluation",
    "TransferCapacityError",
    "TransferReservation",
    "evaluate_lines",
    "line_capacities",
    "peak_occupancy",
    "projected_peak_occupancy",
    "reconcile_transfer_occupancy",
    "record_reservation",
    "refresh_observed_peaks",
    "release_reservation",
    "select_transfer_line",
    "step_delta",
]
