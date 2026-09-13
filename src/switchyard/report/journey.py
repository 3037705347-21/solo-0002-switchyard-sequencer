"""Per-car journey profiles reconstructed from a persisted workspace.

The profile is a pure, read-only projection: it never mutates cars, runs,
trains, or events and returns the same document whenever the workspace is
unchanged.

Evidence rules
--------------
Only structured journal events that describe car-relevant facts are used:

* TRAIN_RECEIVED / TRAIN_CLASSIFIED  - intake and classification
* PULL_PLANNED / PULL_RUN_STARTED / PULL_RUN_ADVANCED / PULL_RUN_COMPLETED
* TRAIN_DEPARTED

Every other event kind (shift lifecycle, blocked closures, yard views, ...) is
operational noise and never becomes a trajectory entry. Event ``message`` text
is never parsed; linking uses event kind, sequence, shift, and payload fields
plus entity records.

When an old workspace is missing intermediate events, surviving entity
fragments (intake arrival time, run creation time, ...) are used as weaker,
explicitly labelled ``entity`` evidence, and the missing journal evidence is
reported under ``evidence_gaps``. Missing fragments are never invented.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import EventKind, MoveVerb
from ..domain.timeutil import parse_iso

# Event kinds that may carry car-relevant evidence. Everything else is noise.
_CAR_EVENT_KINDS = {
    EventKind.TRAIN_RECEIVED,
    EventKind.TRAIN_CLASSIFIED,
    EventKind.PULL_PLANNED,
    EventKind.PULL_RUN_STARTED,
    EventKind.PULL_RUN_ADVANCED,
    EventKind.PULL_RUN_COMPLETED,
    EventKind.TRAIN_DEPARTED,
}

_PHASE_RANK = {
    "RECEIVED": 10,
    "CLASSIFIED": 20,
    "RESERVED": 30,
    "BUFFERED": 40,
    "ASSEMBLED": 50,
    "RETURNED": 60,
    "DEPARTED": 70,
}

# Causal ordering groups, independent of which evidence backs an entry.
_GROUP_RECEIVED = 0
_GROUP_CLASSIFIED = 1
_GROUP_RUN = 2
_GROUP_DEPARTED = 3

# Entity timestamps and their journal events are written by the same command,
# but a second boundary can make them differ by one second. Any larger skew
# means the event and the entity describe different facts.
_TIMESTAMP_TOLERANCE_SECONDS = 2


def _timestamp_consistent(entity_at: str | None, event_at: str | None) -> bool:
    entity_at = entity_at or None
    event_at = event_at or None
    if entity_at is None or event_at is None:
        return entity_at is None and event_at is None
    try:
        delta = abs((parse_iso(event_at) - parse_iso(entity_at)).total_seconds())
    except ValueError:
        return entity_at == event_at
    return delta <= _TIMESTAMP_TOLERANCE_SECONDS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_car_journey(workspace: Any, car_code: str) -> dict[str, Any] | None:
    """Rebuild one car's journey, or ``None`` when the car does not exist."""

    if car_code not in workspace.cars:
        return None
    context = _JourneyContext(workspace)
    return context.profile_for(car_code)


def build_car_journey_index(workspace: Any) -> dict[str, Any]:
    """Compact, deterministic index of every car journey in the workspace."""

    context = _JourneyContext(workspace)
    items = []
    for code in sorted(workspace.cars):
        journey = context.profile_for(code)
        car = journey["car"]
        items.append(
            {
                "code": code,
                "state": car["state"],
                "location": car["location"],
                "destination": car["destination"],
                "phases": journey["phases"],
                "entry_count": len(journey["entries"]),
                "flag_count": len(journey["flags"]),
                "evidence_gap_count": len(journey["evidence_gaps"]),
                "consistent": journey["consistent"],
            }
        )
    return {"car_count": len(items), "cars": items}


# ---------------------------------------------------------------------------
# Reconstruction context (event linking is computed once per workspace)
# ---------------------------------------------------------------------------


class _JourneyContext:
    def __init__(self, workspace: Any):
        self.workspace = workspace
        self.intakes = sorted(workspace.intakes.values(), key=lambda item: (item.arrival_at, item.code))
        self.outbounds = sorted(workspace.outbounds.values(), key=lambda item: (item.created_at, item.code))
        self.runs = sorted(workspace.runs.values(), key=lambda item: (item.created_at, item.code))
        self.events = sorted(workspace.events, key=lambda item: item.sequence)
        self.event_by_seq = {event.sequence: event for event in self.events}
        self.receive_links = self._link_receive_events()
        self.classify_links = self._link_classify_events()
        self.run_links = self._link_run_events()
        self.depart_links, self.depart_ambiguous = self._link_depart_events()

    def profile_for(self, car_code: str) -> dict[str, Any]:
        car = self.workspace.cars[car_code]
        intake = next((train for train in self.intakes if car_code in train.consist), None)
        related_outbounds = [
            train
            for train in self.outbounds
            if car_code in train.planned_car_codes or car_code in train.assembled_car_codes
        ]
        related_runs = [run for run in self.runs if self._run_touches_car(run, car_code)]

        entries: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []

        self._received_entry(car_code, intake, entries, gaps)
        self._classified_entry(car_code, intake, entries, gaps)
        for run in related_runs:
            self._run_entries(car_code, run, entries, gaps)
        self._departed_entry(car_code, related_outbounds, entries, gaps)

        ordered = self._order_entries(entries)
        for entry in ordered:
            entry.pop("_order", None)
        for index, entry in enumerate(ordered):
            entry["index"] = index

        flags = _consistency_flags(self.workspace, car_code, ordered)
        flags.extend(_time_order_flags(car_code, ordered))

        return {
            "car": car.to_dict(),
            "current": {"state": str(car.state), "location": car.location},
            "associations": _associations(car_code, intake, related_outbounds, related_runs, ordered),
            "entries": ordered,
            "phases": [entry["phase"] for entry in ordered],
            "expected_terminal": _expected_terminal(ordered),
            "flags": flags,
            "evidence_gaps": gaps,
            "consistent": not flags and not gaps,
        }

    # -- ordering ----------------------------------------------------------

    def _order_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Order by causal position, then journal sequence within a position."""

        def key(entry: dict[str, Any]) -> tuple[Any, ...]:
            order = entry["_order"]
            seq = entry["event_sequence"] if entry["event_sequence"] is not None else 10**9
            return (*order, seq)

        return sorted(entries, key=key)

    # -- event linking -----------------------------------------------------

    def _events_of(self, kind: EventKind) -> list[Any]:
        return [event for event in self.events if event.kind == kind]

    def _link_receive_events(self) -> dict[int, Any]:
        """Greedily map TRAIN_RECEIVED events to intake trains."""

        links: dict[int, Any] = {}
        remaining = list(self.intakes)
        for event in self._events_of(EventKind.TRAIN_RECEIVED):
            payload = event.payload or {}
            route = payload.get("route")
            car_count = payload.get("car_count")
            match = next(
                (
                    train
                    for train in remaining
                    if route is not None
                    and train.route == str(route)
                    and car_count is not None
                    and len(train.consist) == int(car_count)
                ),
                None,
            )
            if match is None:
                match = next(iter(remaining), None)
            if match is not None:
                links[id(match)] = event
                remaining.remove(match)
        return links

    def _link_classify_events(self) -> dict[int, dict[str, Any]]:
        """Map TRAIN_CLASSIFIED events to intakes; keep parsed spot records."""

        links: dict[int, dict[str, Any]] = {}
        remaining = list(self.intakes)
        for event in self._events_of(EventKind.TRAIN_CLASSIFIED):
            payload = event.payload or {}
            spots = payload.get("spots") if isinstance(payload.get("spots"), list) else []
            unplaced = payload.get("unplaced") if isinstance(payload.get("unplaced"), list) else []
            spotted = payload.get("spotted")
            match = None
            for train in remaining:
                placed_count = len(train.consist) - len(train.unplaced)
                if spotted is not None and int(spotted) != placed_count:
                    continue
                if unplaced and {str(code) for code in unplaced} != set(train.unplaced):
                    continue
                match = train
                break
            if match is None:
                match = next(iter(remaining), None)
            if match is not None:
                links[id(match)] = {"event": event, "spots": [dict(spot) for spot in spots]}
                remaining.remove(match)
        return links

    def _link_run_events(self) -> dict[int, dict[str, Any]]:
        """Globally match run lifecycle events to runs.

        Completion events identify their run uniquely through the completed
        consist. The remaining plan/start/advance events carry no run code, so
        the matcher enumerates every assignment of those events to runs that is

        * one-to-one (each event belongs to at most one run),
        * field-compatible (transfer, step count, cursor bounds),
        * timestamp-compatible with the run record, and
        * chronologically consistent per run
          (plan < start < advances with strictly increasing cursor < completion).

        An event is bound only when it has the same owner in every consistent
        assignment. Anything else stays unbound and is recorded on each run it
        could belong to as an ambiguity gap; no run ever borrows another run's
        start, advance, completion, or departure.
        """

        plans = self._events_of(EventKind.PULL_PLANNED)
        starts = self._events_of(EventKind.PULL_RUN_STARTED)
        advances = self._events_of(EventKind.PULL_RUN_ADVANCED)
        completions = self._events_of(EventKind.PULL_RUN_COMPLETED)

        bond_for: dict[int, dict[str, Any]] = {
            id(run): {
                "plan": None,
                "started": None,
                "advances": [],
                "completed": None,
                "ambiguous": {
                    EventKind.PULL_PLANNED: [],
                    EventKind.PULL_RUN_STARTED: [],
                    EventKind.PULL_RUN_ADVANCED: [],
                    EventKind.PULL_RUN_COMPLETED: [],
                },
            }
            for run in self.runs
        }

        def bond(run: Any) -> dict[str, Any]:
            return bond_for[id(run)]

        # Completion anchors: identity by exact completed consist.
        for event in completions:
            candidates = self._completion_candidates(event, bond)
            if len(candidates) == 1:
                bond(candidates[0])["completed"] = event
            else:
                for run in candidates:
                    bond(run)["ambiguous"][EventKind.PULL_RUN_COMPLETED].append(event)

        runs = list(self.runs)
        plan_compat = {event.sequence: self._plan_compatible_runs(event, bond) for event in plans}
        start_compat = {event.sequence: self._start_compatible_runs(event, bond) for event in starts}
        advance_compat = {event.sequence: self._advance_compatible_runs(event, bond) for event in advances}

        assignments = self._solve_run_assignment(
            runs, plans, starts, advances, plan_compat, start_compat, advance_compat, bond
        )

        bound: dict[int, Any] = {}
        if assignments:
            owners_by_event: dict[int, set[int]] = {}
            for assignment in assignments:
                for event_seq, owner_run in assignment.items():
                    owners_by_event.setdefault(event_seq, set()).add(id(owner_run))
            events_by_seq = {
                event.sequence: event for event in plans + starts + advances
            }
            for event_seq, owner_ids in owners_by_event.items():
                if len(owner_ids) == 1:
                    bound[event_seq] = next(run for run in runs if id(run) == next(iter(owner_ids)))
                else:
                    event = events_by_seq[event_seq]
                    kind = self._kind_of_run_event(event)
                    for run in runs:
                        compat = self._compat_for(kind, event, plan_compat, start_compat, advance_compat)
                        if run in compat and event not in bond(run)["ambiguous"][kind]:
                            bond(run)["ambiguous"][kind].append(event)
            for event_seq, owner_run in bound.items():
                event = events_by_seq[event_seq]
                kind = self._kind_of_run_event(event)
                run_bond = bond(owner_run)
                if kind == EventKind.PULL_PLANNED:
                    run_bond["plan"] = event
                elif kind == EventKind.PULL_RUN_STARTED:
                    run_bond["started"] = event
                else:
                    run_bond["advances"].append(event)
        else:
            # No globally consistent full assignment (typically old data with
            # missing events). Nothing is guessed: every compatible event is an
            # ambiguity candidate for each run it could belong to.
            for event in plans:
                self._mark_ambiguous(event, EventKind.PULL_PLANNED, plan_compat[event.sequence], bond)
            for event in starts:
                self._mark_ambiguous(event, EventKind.PULL_RUN_STARTED, start_compat[event.sequence], bond)
            for event in advances:
                self._mark_ambiguous(event, EventKind.PULL_RUN_ADVANCED, advance_compat[event.sequence], bond)

        for run in runs:
            bond(run)["advances"].sort(key=lambda item: item.sequence)
        return bond_for

    def _solve_run_assignment(
        self,
        runs: list[Any],
        plans: list[Any],
        starts: list[Any],
        advances: list[Any],
        plan_compat: dict[Any, list[Any]],
        start_compat: dict[Any, list[Any]],
        advance_compat: dict[Any, list[Any]],
        bond: Any,
    ) -> list[dict[int, Any]]:
        """Enumerate consistent event->run assignments up to a safety cap."""

        events: list[Any] = list(plans) + list(starts) + list(advances)
        events.sort(key=lambda item: item.sequence)
        owners_by_run: dict[int, list[Any]] = {id(run): [] for run in runs}
        assignments: list[dict[int, Any]] = []
        cap = 512

        def kind_of(event: Any) -> EventKind:
            return self._kind_of_run_event(event)

        def search(index: int, mapping: dict[int, Any]) -> None:
            if len(assignments) >= cap:
                return
            if index == len(events):
                if self._assignment_complete(runs, mapping, bond):
                    assignments.append(dict(mapping))
                return
            event = events[index]
            kind = kind_of(event)
            compat = self._compat_for(kind, event, plan_compat, start_compat, advance_compat)
            # Each event is visited exactly once, so assigning it never
            # conflicts with another event; a run legitimately owns many
            # events (one plan, one start, and several advances).
            for run in compat:
                if self._assignment_accepts(run, kind, event, owners_by_run[id(run)], bond, mapping):
                    mapping[event.sequence] = run
                    owners_by_run[id(run)].append(event)
                    search(index + 1, mapping)
                    owners_by_run[id(run)].pop()
                    del mapping[event.sequence]
                    if len(assignments) >= cap:
                        return
            # Advance events may legitimately stay unassigned (e.g. a run that
            # finished in a single advance call); plan/start events cannot.
            if kind == EventKind.PULL_RUN_ADVANCED:
                search(index + 1, mapping)

        search(0, {})
        return assignments

    def _assignment_complete(self, runs: list[Any], mapping: dict[int, Any], bond: Any) -> bool:
        """Every run that executed must have its required plan/start event."""

        owned_by_run: dict[int, list[Any]] = {id(run): [] for run in runs}
        for event_seq, owner_run in mapping.items():
            owned_by_run[id(owner_run)].append(self.event_by_seq[event_seq])
        for run in runs:
            owned = owned_by_run[id(run)]
            has_plan = any(self._kind_of_run_event(item) == EventKind.PULL_PLANNED for item in owned)
            has_start = any(self._kind_of_run_event(item) == EventKind.PULL_RUN_STARTED for item in owned)
            owned_advances = [
                item for item in owned if self._kind_of_run_event(item) == EventKind.PULL_RUN_ADVANCED
            ]
            has_advance = bool(owned_advances)
            needs_lifecycle = bond(run)["completed"] is not None or run.started_at is not None or has_advance
            if needs_lifecycle and not (has_plan and has_start):
                return False
            if not self._advances_feasible(run, sorted(owned_advances, key=lambda item: item.sequence), bond(run)):
                return False
            # A completed run's whole step list must be timed either by its own
            # advance cursors or its single-call completion event. With an
            # advance chain, the last cursor must reach the final step; without
            # any advance, the run must be a single-call completion.
            if bond(run)["completed"] is not None:
                if owned_advances:
                    cursors = [
                        int((item.payload or {}).get("current_step", 0)) for item in owned_advances
                    ]
                    # Strictly increasing cursors; the run's own completion
                    # event times every remaining step after the last advance,
                    # so any final cursor below the total is acceptable.
                    if any(cursors[i] <= cursors[i - 1] for i in range(1, len(cursors))):
                        return False
                    if cursors[-1] >= len(run.steps):
                        return cursors[-1] == len(run.steps)
                elif len(run.steps) > 1:
                    return False
        return True

    def _assignment_accepts(
        self,
        run: Any,
        kind: EventKind,
        event: Any,
        owned: list[Any],
        bond: Any,
        mapping: dict[int, Any],
    ) -> bool:
        """Check per-run chronological and cursor constraints incrementally."""

        def is_available(candidate: Any) -> bool:
            # A candidate plan/start event can anchor this run only if another
            # run has not already claimed it in the current partial mapping.
            return mapping.get(candidate.sequence, run) is run

        owned_kinds = {self._kind_of_run_event(item): item for item in owned}
        plan_event = owned_kinds.get(EventKind.PULL_PLANNED)
        start_event = owned_kinds.get(EventKind.PULL_RUN_STARTED)
        owned_advances = [
            item for item in owned if self._kind_of_run_event(item) == EventKind.PULL_RUN_ADVANCED
        ]
        completion = bond(run)["completed"]
        seq = event.sequence
        if kind == EventKind.PULL_PLANNED:
            if plan_event is not None:
                return False
            if start_event is not None and seq > start_event.sequence:
                return False
            if completion is not None and seq > completion.sequence:
                return False
            return True
        if kind == EventKind.PULL_RUN_STARTED:
            if start_event is not None:
                return False
            if plan_event is not None and seq < plan_event.sequence:
                return False
            if completion is not None and seq > completion.sequence:
                return False
            if plan_event is None:
                plan_candidates = [
                    item
                    for item in self._events_of(EventKind.PULL_PLANNED)
                    if item.sequence < seq
                    and is_available(item)
                    and (item.payload or {}).get("transfer_code") in (None, run.transfer_code)
                    and (item.payload or {}).get("steps") in (None, len(run.steps))
                ]
                if not plan_candidates:
                    return False
            return True
        # advance
        cursor = int((event.payload or {}).get("current_step", 0))
        if plan_event is not None and seq < plan_event.sequence:
            return False
        if start_event is not None and seq < start_event.sequence:
            return False
        if completion is not None and seq > completion.sequence:
            return False
        # A plan/start event not owned yet must still exist, unclaimed and
        # before this advance in the journal; otherwise this advance could
        # never belong to the run.
        if start_event is None:
            start_candidates = [
                item
                for item in self._events_of(EventKind.PULL_RUN_STARTED)
                if item.sequence < seq
                and is_available(item)
                and (completion is None or item.sequence < completion.sequence)
                and _timestamp_consistent(run.started_at, item.at)
                and (item.payload or {}).get("total_steps") in (None, len(run.steps))
            ]
            if not start_candidates:
                return False
        if plan_event is None:
            plan_candidates = [
                item
                for item in self._events_of(EventKind.PULL_PLANNED)
                if item.sequence < seq
                and is_available(item)
                and (item.payload or {}).get("transfer_code") in (None, run.transfer_code)
                and (item.payload or {}).get("steps") in (None, len(run.steps))
            ]
            if not plan_candidates:
                return False
        sorted_advances = sorted(
            owned_advances + [event],
            key=lambda item: (item.sequence, int((item.payload or {}).get("current_step", 0))),
        )
        return self._advances_feasible(run, sorted_advances, bond(run))

    def _advances_feasible(self, run: Any, advances: list[Any], run_bond: dict[str, Any]) -> bool:
        """Validate the cursor chain a run would own.

        Cursors must start at 1 and increase by one per advance call (a call
        can batch several steps, so jumps are allowed only when the missing
        boundary is the run's own completion). For a completed run the last
        owned cursor, together with its completion event, must account for the
        whole step list; for a running run it must not exceed current_step.
        """

        if not advances:
            return True
        cursors = [int((item.payload or {}).get("current_step", 0)) for item in advances]
        total_steps = len(run.steps)
        if cursors[0] < 1:
            return False
        previous = cursors[0]
        for cursor in cursors[1:]:
            if cursor <= previous:
                return False
            previous = cursor
        if cursors[-1] > total_steps:
            return False
        completion = run_bond["completed"]
        if completion is None and str(run.state) != "COMPLETED":
            if cursors[-1] > max(0, run.current_step):
                return False
        return True

    def _mark_ambiguous(self, event: Any, kind: EventKind, candidates: list[Any], bond: Any) -> None:
        for run in candidates:
            bucket = bond(run)["ambiguous"][kind]
            if event not in bucket:
                bucket.append(event)

    @staticmethod
    def _kind_of_run_event(event: Any) -> EventKind:
        return event.kind

    @staticmethod
    def _compat_for(kind, event, plan_compat, start_compat, advance_compat):
        if kind == EventKind.PULL_PLANNED:
            return plan_compat[event.sequence]
        if kind == EventKind.PULL_RUN_STARTED:
            return start_compat[event.sequence]
        return advance_compat[event.sequence]

    def _completion_candidates(self, event: Any, bond: Any) -> list[Any]:
        payload = event.payload or {}
        assembled = payload.get("assembled_car_codes")
        step_count = payload.get("steps")
        wanted = [str(code) for code in assembled] if isinstance(assembled, list) else None
        return [
            run
            for run in self.runs
            if bond(run)["completed"] is None
            and (wanted is None or self._run_planned_codes(run) == wanted)
            and (step_count is None or len(run.steps) == int(step_count))
            and _timestamp_consistent(run.completed_at, event.at)
        ]

    def _plan_compatible_runs(self, event: Any, bond: Any) -> list[Any]:
        payload = event.payload or {}
        result = []
        for run in self.runs:
            if bond(run)["plan"] is not None:
                continue
            if not _timestamp_consistent(run.created_at, event.at):
                continue
            if payload.get("transfer_code") is not None and run.transfer_code != str(payload["transfer_code"]):
                continue
            if payload.get("steps") is not None and len(run.steps) != int(payload["steps"]):
                continue
            completion = bond(run)["completed"]
            if completion is not None and event.sequence > completion.sequence:
                continue
            result.append(run)
        return result

    def _start_compatible_runs(self, event: Any, bond: Any) -> list[Any]:
        payload = event.payload or {}
        result = []
        for run in self.runs:
            if bond(run)["started"] is not None:
                continue
            if not _timestamp_consistent(run.started_at, event.at):
                continue
            if payload.get("total_steps") is not None and len(run.steps) != int(payload["total_steps"]):
                continue
            completion = bond(run)["completed"]
            if completion is not None and event.sequence > completion.sequence:
                continue
            result.append(run)
        return result

    def _advance_compatible_runs(self, event: Any, bond: Any) -> list[Any]:
        payload = event.payload or {}
        cursor = payload.get("current_step")
        result = []
        for run in self.runs:
            if cursor is None or not (0 < int(cursor) <= len(run.steps)):
                continue
            if run.started_at is None:
                continue
            completion = bond(run)["completed"]
            if completion is not None and event.sequence > completion.sequence:
                continue
            result.append(run)
        return result

    def _link_depart_events(self) -> tuple[dict[int, Any], dict[int, list[Any]]]:
        """Assign TRAIN_DEPARTED events only when the owner is unique."""

        links: dict[int, Any] = {}
        ambiguous: dict[int, list[Any]] = {}
        departed = [
            train
            for train in self.outbounds
            if str(train.state) == "DEPARTED" and train.assembled_car_codes
        ]
        for event in self._events_of(EventKind.TRAIN_DEPARTED):
            payload = event.payload or {}
            car_count = payload.get("car_count")
            departed_at = payload.get("departed_at")
            candidates = [
                train
                for train in departed
                if id(train) not in links
                and (car_count is None or len(train.assembled_car_codes) == int(car_count))
                and _timestamp_consistent(train.departed_at, event.at)
                and (departed_at is None or train.departed_at == str(departed_at))
            ]
            if len(candidates) == 1:
                links[id(candidates[0])] = event
            else:
                for train in candidates:
                    ambiguous.setdefault(id(train), []).append(event)
        return links, ambiguous

    # -- entity helpers ----------------------------------------------------

    def _run_planned_codes(self, run: Any) -> list[str]:
        outbound = self.workspace.outbounds.get(run.outbound_code)
        return list(outbound.planned_car_codes) if outbound is not None else []

    def _run_touches_car(self, run: Any, car_code: str) -> bool:
        if any(step.car_code == car_code for step in run.steps):
            return True
        return car_code in self._run_planned_codes(run)

    def _planned_position(self, run: Any, car_code: str) -> int | None:
        codes = self._run_planned_codes(run)
        return codes.index(car_code) + 1 if car_code in codes else None

    def _car_track(self, car_code: str) -> str | None:
        for code, track in sorted(self.workspace.tracks.items()):
            if car_code in track.stack:
                return code
        car = self.workspace.cars.get(car_code)
        if car is not None and car.location in self.workspace.tracks:
            return str(car.location)
        return None

    # -- entry construction ------------------------------------------------

    def _received_entry(
        self,
        car_code: str,
        intake: Any,
        entries: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
    ) -> None:
        if intake is None:
            gaps.append(_gap("MISSING_RECEPTION", car_code, "car is not listed on any intake train"))
            return
        entry = _base_entry("RECEIVED")
        event = self.receive_links.get(id(intake))
        if event is not None:
            _anchor_event(entry, event)
        else:
            entry["at"] = intake.arrival_at or None
            entry["evidence"] = "entity"
            entry["shift_code"] = None
            gaps.append(
                _gap(
                    "EVIDENCE_FALLBACK",
                    car_code,
                    f"RECEIVED entry for {intake.code} uses intake arrival time; journal event missing",
                    phase="RECEIVED",
                )
            )
        entry["intake_code"] = intake.code
        entry["location"] = "INTAKE"
        entry["detail"] = f"received with intake {intake.code} from route {intake.route}"
        _set_order(entry, (_GROUP_RECEIVED, self.intakes.index(intake), 0))
        entries.append(entry)

    def _classified_entry(
        self,
        car_code: str,
        intake: Any,
        entries: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
    ) -> None:
        car = self.workspace.cars[car_code]
        track_code = None
        event = None
        evidence = "entity"
        if intake is not None:
            link = self.classify_links.get(id(intake))
            if link is not None:
                event = link["event"]
                spot = next((item for item in link["spots"] if str(item.get("car_code")) == car_code), None)
                if spot is not None:
                    track_code = str(spot.get("track_code"))
                    evidence = "event"
                elif car_code not in intake.unplaced:
                    # Train-level event proves classification happened; the
                    # per-car spot comes from the persisted stack record.
                    track_code = self._car_track(car_code)
                    evidence = "event-train"
        if track_code is None and car_code not in (intake.unplaced if intake is not None else []):
            # Entity-only evidence for older data without classification
            # events: the car's own record / track stack proves the spotting.
            track_code = self._car_track(car_code)
        if track_code is None:
            if str(car.state) != "RECEIVED":
                gaps.append(
                    _gap(
                        "MISSING_CLASSIFICATION",
                        car_code,
                        "car moved past RECEIVED without classification evidence",
                    )
                )
            return
        entry = _base_entry("CLASSIFIED")
        if event is not None:
            _anchor_event(entry, event)
        entry["evidence"] = evidence
        if event is None:
            gaps.append(
                _gap(
                    "EVIDENCE_FALLBACK",
                    car_code,
                    f"CLASSIFIED entry onto {track_code} uses current car record; journal event missing",
                    phase="CLASSIFIED",
                )
            )
        entry["intake_code"] = intake.code if intake is not None else None
        entry["track_code"] = track_code
        entry["location"] = track_code
        entry["detail"] = f"classified onto standing track {track_code}"
        if intake is not None:
            entry["detail"] = f"classified from intake {intake.code} onto standing track {track_code}"
        intake_order = self.intakes.index(intake) if intake is not None and intake in self.intakes else 9999
        _set_order(entry, (_GROUP_CLASSIFIED, intake_order, 0))
        entries.append(entry)

    def _run_entries(
        self,
        car_code: str,
        run: Any,
        entries: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
    ) -> None:
        bond = self.run_links.get(id(run))
        if bond is None:
            return
        planned_codes = self._run_planned_codes(run)
        executed = self._executed_step_count(run, bond)

        ambiguous_start = bond["ambiguous"][EventKind.PULL_RUN_STARTED]
        run_started = bond.get("started") is not None
        run_executed = bond.get("completed") is not None or run.started_at is not None
        involves_car = car_code in planned_codes or any(step.car_code == car_code for step in run.steps[:executed])
        if not run_started and run_executed and involves_car:
            if ambiguous_start:
                gaps.append(
                    _gap(
                        "AMBIGUOUS_START_EVENT",
                        car_code,
                        f"start event for run {run.code} could not be uniquely attributed; "
                        "no other run's start record is used",
                        run_code=run.code,
                        candidate_event_sequences=[event.sequence for event in ambiguous_start],
                    )
                )
            else:
                gaps.append(
                    _gap(
                        "MISSING_START_EVENT",
                        car_code,
                        f"run {run.code} executed but has no start journal event",
                        run_code=run.code,
                    )
                )

        run_order = self.runs.index(run)
        if car_code in planned_codes:
            reserved = self._reserved_entry(car_code, run, bond, gaps)
            _set_order(reserved, (_GROUP_RUN, run_order, -1))
            entries.append(reserved)

        boundaries = self._step_boundaries(run, bond, executed)
        pull_count = 0
        missing_time = False
        ambiguous_time = False
        move_candidates: list[int] = []
        for step_index, step in enumerate(run.steps):
            if step_index >= executed or step.car_code != car_code:
                continue
            verb = str(step.verb)
            if verb == str(MoveVerb.PULL):
                pull_count += 1
                phase = "ASSEMBLED"
            elif verb == str(MoveVerb.BUFFER):
                phase = "BUFFERED"
            else:
                phase = "RETURNED"
            entry = _base_entry(phase)
            entry["run_code"] = run.code
            entry["outbound_code"] = run.outbound_code
            entry["transfer_code"] = run.transfer_code
            entry["track_code"] = step.source_code if verb != str(MoveVerb.RETURN) else step.target_code
            entry["move_verb"] = verb
            if verb == str(MoveVerb.BUFFER):
                entry["from_location"] = step.source_code
                entry["location"] = step.target_code
                entry["detail"] = f"buffered from {step.source_code} into bay {step.target_code}"
            elif verb == str(MoveVerb.RETURN):
                entry["from_location"] = step.source_code
                entry["location"] = step.target_code
                entry["detail"] = f"returned from bay {step.source_code} to {step.target_code}"
            else:
                entry["from_location"] = step.source_code
                entry["location"] = step.target_code
                entry["assembly_position"] = pull_count
                entry["detail"] = f"pulled from {step.source_code} onto outbound {step.target_code}"
            boundary = boundaries.get(step_index)
            if boundary is not None:
                _anchor_event(entry, boundary)
            else:
                # No advance event uniquely attributable to this run for the
                # step. Another run's event is never borrowed: either a matching
                # event exists but is ambiguous, or no advance was logged
                # (single-call completion / old data).
                entry["evidence"] = "entity"
                matching = self._matching_advance_events(run, bond, step_index)
                if matching:
                    ambiguous_time = True
                    move_candidates.extend(item.sequence for item in matching)
                else:
                    missing_time = True
            _set_order(entry, (_GROUP_RUN, run_order, step_index))
            entries.append(entry)
        if ambiguous_time:
            gaps.append(
                _gap(
                    "AMBIGUOUS_MOVE_EVENT",
                    car_code,
                    f"executed move(s) of run {run.code} could not be matched to a unique "
                    "advance journal event; no other run's event is used",
                    run_code=run.code,
                    candidate_event_sequences=sorted(set(move_candidates)),
                )
            )
        elif missing_time:
            gaps.append(
                _gap(
                    "MISSING_MOVE_TIME",
                    car_code,
                    f"executed move(s) of run {run.code} have no uniquely attributable "
                    "advance journal event",
                    run_code=run.code,
                )
            )

    def _reserved_entry(self, car_code: str, run: Any, bond: dict[str, Any], gaps: list[dict[str, Any]]) -> dict[str, Any]:
        entry = _base_entry("RESERVED")
        entry["run_code"] = run.code
        entry["outbound_code"] = run.outbound_code
        pull_step = next((step for step in run.steps if str(step.verb) == str(MoveVerb.PULL) and step.car_code == car_code), None)
        track_code = pull_step.source_code if pull_step is not None else self._car_track(car_code)
        entry["track_code"] = track_code
        entry["location"] = track_code
        entry["detail"] = f"reserved by pull plan {run.code} for outbound {run.outbound_code}"
        plan_event = bond.get("plan")
        if plan_event is not None:
            _anchor_event(entry, plan_event)
        else:
            entry["at"] = run.created_at or None
            entry["evidence"] = "entity"
            if bond["ambiguous"][EventKind.PULL_PLANNED]:
                gaps.append(
                    _gap(
                        "AMBIGUOUS_PLAN_EVENT",
                        car_code,
                        f"plan event for run {run.code} could not be uniquely attributed; "
                        "reservation time falls back to the run record",
                        phase="RESERVED",
                        run_code=run.code,
                        candidate_event_sequences=[
                            event.sequence for event in bond["ambiguous"][EventKind.PULL_PLANNED]
                        ],
                    )
                )
            else:
                gaps.append(
                    _gap(
                        "EVIDENCE_FALLBACK",
                        car_code,
                        f"RESERVED entry for run {run.code} uses run creation time; journal event missing",
                        phase="RESERVED",
                        run_code=run.code,
                    )
                )
        return entry

    def _departed_entry(
        self,
        car_code: str,
        related_outbounds: list[Any],
        entries: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
    ) -> None:
        for outbound in related_outbounds:
            if car_code not in outbound.assembled_car_codes or str(outbound.state) != "DEPARTED":
                continue
            entry = _base_entry("DEPARTED")
            entry["outbound_code"] = outbound.code
            entry["location"] = outbound.code
            entry["assembly_position"] = (
                outbound.assembled_car_codes.index(car_code) + 1 if car_code in outbound.assembled_car_codes else None
            )
            entry["detail"] = f"departed on outbound {outbound.code} for {outbound.destination}"
            event = self.depart_links.get(id(outbound))
            ambiguous_events = self.depart_ambiguous.get(id(outbound), [])
            if event is not None:
                _anchor_event(entry, event)
            else:
                entry["at"] = outbound.departed_at
                entry["evidence"] = "entity"
                if ambiguous_events:
                    gaps.append(
                        _gap(
                            "AMBIGUOUS_DEPARTURE_EVENT",
                            car_code,
                            f"departure event for {outbound.code} could not be uniquely attributed; "
                            "departure time falls back to the outbound record",
                            phase="DEPARTED",
                            outbound_code=outbound.code,
                            candidate_event_sequences=[item.sequence for item in ambiguous_events],
                        )
                    )
                else:
                    gaps.append(
                        _gap(
                            "EVIDENCE_FALLBACK",
                            car_code,
                            f"DEPARTED entry on {outbound.code} uses outbound departure time; journal event missing",
                            phase="DEPARTED",
                            outbound_code=outbound.code,
                        )
                    )
            _set_order(
                entry,
                (
                    _GROUP_DEPARTED,
                    self.outbounds.index(outbound),
                    outbound.assembled_car_codes.index(car_code),
                ),
            )
            entries.append(entry)

    def _executed_step_count(self, run: Any, bond: dict[str, Any]) -> int:
        state = str(run.state)
        if state == "COMPLETED":
            return len(run.steps)
        if state in {"RUNNING", "FAILED"}:
            return max(0, run.current_step)
        return 0

    def _step_boundaries(self, run: Any, bond: dict[str, Any], executed: int) -> dict[int, Any]:
        """Map each executed step to an event uniquely attributed to this run.

        An advance with cursor K times the batch ending at step K-1. If the run
        finished in a single advance call there is no advance event at all, so
        the whole step list is timed by this run's own completion event. When
        advances exist, the completion event times only the final tail batch
        (the step at total-1); earlier steps must have their own advance
        event. Events owned by another run, and ambiguous events, are never
        used here.
        """

        boundary_events: list[tuple[int, Any]] = []
        for event in bond.get("advances", []):
            cursor = int((event.payload or {}).get("current_step", 0))
            boundary_events.append((cursor, event))
        boundary_events.sort(key=lambda item: item[0])
        result: dict[int, Any] = {}
        previous_cursor = 0
        for cursor, event in boundary_events:
            for step_index in range(previous_cursor, min(cursor, executed)):
                result[step_index] = event
            previous_cursor = max(previous_cursor, cursor)
        completion = bond.get("completed")
        if completion is not None and previous_cursor < executed:
            ambiguous_advances = bond["ambiguous"][EventKind.PULL_RUN_ADVANCED]
            if not boundary_events and not ambiguous_advances:
                # Genuinely single-call completion: the run's own completion
                # event times every step.
                for step_index in range(previous_cursor, executed):
                    result[step_index] = completion
            elif not boundary_events:
                # Advance events exist but none is uniquely attributable to
                # this run. Only the final tail step is timed by completion;
                # earlier steps stay untimed and are reported as gaps.
                result[executed - 1] = completion
            else:
                # Owned advances cover the prefix; the run's own completion
                # event times every remaining step of the final batch.
                for step_index in range(previous_cursor, executed):
                    result.setdefault(step_index, completion)
        return result

    def _matching_advance_events(self, run: Any, bond: dict[str, Any], step_index: int) -> list[Any]:
        """Ambiguous advance events that could have timed the given step."""

        needed_cursor = step_index + 1
        return [
            event
            for event in bond["ambiguous"][EventKind.PULL_RUN_ADVANCED]
            if int((event.payload or {}).get("current_step", 0)) == needed_cursor
        ]


# ---------------------------------------------------------------------------
# Entry shaping and ordering
# ---------------------------------------------------------------------------


def _base_entry(phase: str) -> dict[str, Any]:
    return {
        "index": 0,
        "phase": phase,
        "at": None,
        "event_sequence": None,
        "shift_code": None,
        "intake_code": None,
        "run_code": None,
        "outbound_code": None,
        "track_code": None,
        "transfer_code": None,
        "from_location": None,
        "location": None,
        "move_verb": None,
        "assembly_position": None,
        "evidence": "event",
        "detail": "",
        # Internal causal position tuple, stripped before the document is
        # returned; independent of which evidence backs the entry.
        "_order": (99, 9999, 9999),
    }


def _set_order(entry: dict[str, Any], order: tuple[Any, ...]) -> None:
    entry["_order"] = order


def _anchor_event(entry: dict[str, Any], event: Any) -> None:
    entry["at"] = event.at
    entry["event_sequence"] = event.sequence
    entry["shift_code"] = event.shift_code or None
    entry["evidence"] = "event"


# ---------------------------------------------------------------------------
# Consistency evaluation
# ---------------------------------------------------------------------------


def _expected_terminal(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None
    state: str | None = None
    location: str | None = None
    for entry in entries:
        phase = entry["phase"]
        if phase == "RECEIVED":
            state, location = "RECEIVED", "INTAKE"
        elif phase == "CLASSIFIED":
            state, location = "STANDING", entry["track_code"]
        elif phase == "RESERVED":
            state, location = "RESERVED", entry["track_code"]
        elif phase == "BUFFERED":
            state, location = "STANDING", entry["transfer_code"]
        elif phase == "RETURNED":
            state, location = "STANDING", entry["track_code"]
        elif phase == "ASSEMBLED":
            state, location = "ASSEMBLED", entry["outbound_code"]
        elif phase == "DEPARTED":
            state, location = "DEPARTED", entry["outbound_code"]
    return {"state": state, "location": location}


def _consistency_flags(workspace: Any, car_code: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    car = workspace.cars.get(car_code)
    if car is None or not entries:
        return flags
    terminal = _expected_terminal(entries)
    if terminal is not None:
        if terminal["state"] is not None and str(car.state) != terminal["state"]:
            flags.append(
                _flag(
                    "CAR_STATE_MISMATCH",
                    car_code,
                    f"trajectory ends in {terminal['state']} but current car state is {car.state}",
                    expected_state=terminal["state"],
                    current_state=str(car.state),
                )
            )
        if terminal["location"] is not None and car.location != terminal["location"]:
            flags.append(
                _flag(
                    "CAR_LOCATION_MISMATCH",
                    car_code,
                    f"trajectory ends at {terminal['location']} but current car location is {car.location}",
                    expected_location=terminal["location"],
                    current_location=car.location,
                )
            )
    flags.extend(_membership_flags(entries[-1], car_code, workspace))
    phases = {entry["phase"] for entry in entries}
    state = str(car.state)
    if state == "ASSEMBLED" and "ASSEMBLED" not in phases:
        flags.append(_flag("MISSING_ASSEMBLY", car_code, "car is ASSEMBLED but no pull entry was found"))
    if state == "DEPARTED" and "DEPARTED" not in phases:
        flags.append(_flag("MISSING_DEPARTURE", car_code, "car is DEPARTED but no departure entry was found"))
    return flags


def _membership_flags(last: dict[str, Any], car_code: str, workspace: Any) -> list[dict[str, Any]]:
    phase = last["phase"]
    if phase == "CLASSIFIED":
        track = workspace.tracks.get(last["track_code"])
        if track is not None and car_code not in track.stack:
            return [_flag("NOT_ON_TRACK_STACK", car_code, f"car is not stacked on {track.code}")]
    if phase == "RESERVED":
        active = {"DRAFT", "PLANNED", "READY"}
        if not any(
            car_code in train.planned_car_codes
            for train in workspace.outbounds.values()
            if str(train.state) in active
        ):
            return [_flag("NOT_PLANNED", car_code, "no active outbound train still reserves this car")]
    if phase == "RETURNED":
        track = workspace.tracks.get(last["track_code"])
        if track is not None and car_code not in track.stack:
            return [_flag("NOT_ON_TRACK_STACK", car_code, f"car is not stacked on {track.code}")]
    if phase == "BUFFERED":
        bay = workspace.buffer_bays.get(last["transfer_code"])
        if bay is not None and car_code not in bay.stack:
            return [_flag("NOT_IN_BUFFER_STACK", car_code, f"car is not parked in bay {bay.code}")]
    if phase == "ASSEMBLED":
        outbound = workspace.outbounds.get(last["outbound_code"])
        if outbound is not None and car_code not in outbound.assembled_car_codes:
            return [_flag("NOT_IN_ASSEMBLED", car_code, f"car is not in consist of {outbound.code}")]
    if phase == "DEPARTED":
        outbound = workspace.outbounds.get(last["outbound_code"])
        if outbound is None or car_code not in outbound.assembled_car_codes:
            return [_flag("NOT_DEPARTED_RECORD", car_code, "departure has no matching outbound consist record")]
    return []


def _time_order_flags(car_code: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Only journal-event timestamps are compared. Entity fallback timestamps
    # mix user-supplied schedule times and server clocks, so a decreasing
    # pair across evidence sources is not evidence of a broken trajectory.
    previous_time = None
    for entry in entries:
        at = entry["at"]
        if at is None or not str(entry.get("evidence", "")).startswith("event"):
            continue
        try:
            moment = parse_iso(at)
        except ValueError:
            continue
        if previous_time is not None and moment < previous_time:
            return [
                _flag(
                    "TIME_ORDER_ANOMALY",
                    car_code,
                    f"{entry['phase']} at {at} precedes an earlier trajectory entry",
                    event_sequence=entry["event_sequence"],
                )
            ]
        previous_time = moment
    return []


# ---------------------------------------------------------------------------
# Associations and issue shaping
# ---------------------------------------------------------------------------


def _associations(
    car_code: str,
    intake: Any,
    outbounds: list[Any],
    runs: list[Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    shift_codes = {entry["shift_code"] for entry in entries if entry.get("shift_code")}
    return {
        "intake": None
        if intake is None
        else {"code": intake.code, "route": intake.route, "arrival_at": intake.arrival_at},
        "outbounds": [
            {
                "code": train.code,
                "destination": train.destination,
                "state": str(train.state),
                "planned_position": _position(train.planned_car_codes, car_code),
                "assembled_position": _position(train.assembled_car_codes, car_code),
                "run_codes": list(train.run_codes),
                "created_at": train.created_at,
                "departed_at": train.departed_at,
            }
            for train in outbounds
        ],
        "pull_runs": [
            {
                "code": run.code,
                "outbound_code": run.outbound_code,
                "transfer_code": run.transfer_code,
                "state": str(run.state),
                "car_role": _run_car_role(run, car_code),
                "step_indexes": [index for index, step in enumerate(run.steps) if step.car_code == car_code],
            }
            for run in runs
        ],
        "shifts": sorted(shift_codes),
    }


def _position(codes: list[str], car_code: str) -> int | None:
    return codes.index(car_code) + 1 if car_code in codes else None


def _run_car_role(run: Any, car_code: str) -> str:
    is_pull = any(str(step.verb) == str(MoveVerb.PULL) and step.car_code == car_code for step in run.steps)
    is_block = any(
        str(step.verb) in {str(MoveVerb.BUFFER), str(MoveVerb.RETURN)} and step.car_code == car_code
        for step in run.steps
    )
    if is_pull and is_block:
        return "PLANNED_AND_BLOCKER"
    if is_pull:
        return "PLANNED"
    return "BLOCKER"


def _flag(code: str, car_code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "car_code": car_code, "message": message, "details": details}


def _gap(code: str, car_code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "car_code": car_code, "message": message, "details": details}


__all__ = ["build_car_journey", "build_car_journey_index"]
