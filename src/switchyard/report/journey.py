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

        ordered = sorted(entries, key=_entry_sort_key)
        for internal in ("_tier", "_seq", "_sub", "_fallback"):
            for entry in ordered:
                entry.pop(internal, None)
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
        """Assign run lifecycle events to runs under hard identity constraints.

        Run journal events carry no run code, so an event is attached only when
        its owner is forced:

        * PULL_RUN_COMPLETED identifies the run through the exact completed
          consist and step count, checked against the persisted run timestamp.
        * PULL_PLANNED / PULL_RUN_STARTED / PULL_RUN_ADVANCED must fall inside
          the plan < start < advance (strictly increasing cursor) < completion
          sequence window of one run and match that run's fields/timestamps.

        Feasibility is solved to a fixed point by uniqueness (event with one
        feasible run) and necessity (run that needs an event and has one
        feasible event). Anything that stays ambiguous is left unassigned and
        recorded on every still-feasible run as an evidence gap; it is never
        guessed onto another car.
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

        def assign(run: Any, kind: EventKind, event: Any) -> None:
            run_bond = bond(run)
            if kind == EventKind.PULL_PLANNED:
                run_bond["plan"] = event
            elif kind == EventKind.PULL_RUN_STARTED:
                run_bond["started"] = event
            elif kind == EventKind.PULL_RUN_ADVANCED:
                run_bond["advances"].append(event)
            else:
                run_bond["completed"] = event

        # Completion anchors: identity by completed consist + step count.
        unassigned = {
            EventKind.PULL_PLANNED: list(plans),
            EventKind.PULL_RUN_STARTED: list(starts),
            EventKind.PULL_RUN_ADVANCED: list(advances),
            EventKind.PULL_RUN_COMPLETED: list(completions),
        }

        for event in list(unassigned[EventKind.PULL_RUN_COMPLETED]):
            candidates = self._completion_candidates(event, bond)
            if len(candidates) == 1:
                assign(candidates[0], EventKind.PULL_RUN_COMPLETED, event)
                unassigned[EventKind.PULL_RUN_COMPLETED].remove(event)
            else:
                for run in candidates:
                    bond(run)["ambiguous"][EventKind.PULL_RUN_COMPLETED].append(event)

        kinds = (
            EventKind.PULL_PLANNED,
            EventKind.PULL_RUN_STARTED,
            EventKind.PULL_RUN_ADVANCED,
        )
        necessity_kinds = (EventKind.PULL_PLANNED, EventKind.PULL_RUN_STARTED)

        def needs_event(run: Any, kind: EventKind) -> bool:
            run_bond = bond(run)
            if kind == EventKind.PULL_PLANNED:
                return run_bond["plan"] is None and (
                    run_bond["started"] is not None or run_bond["completed"] is not None
                )
            if kind == EventKind.PULL_RUN_STARTED:
                return run_bond["started"] is None and (
                    run_bond["completed"] is not None
                    or bool(run_bond["advances"])
                    or run.started_at is not None
                )
            # Advance events are never forced: a run can finish in a single
            # advance call without ever emitting a PULL_RUN_ADVANCED event.
            return False

        progressed = True
        while progressed and any(unassigned[kind] for kind in kinds):
            progressed = False
            # Uniqueness: event feasible for exactly one run.
            for kind in kinds:
                kept: list[Any] = []
                for event in unassigned[kind]:
                    candidates = self._event_candidate_runs(kind, event, bond)
                    if len(candidates) == 1:
                        assign(candidates[0], kind, event)
                        progressed = True
                    else:
                        kept.append(event)
                unassigned[kind] = kept
            # Necessity: a run that must have this kind of event and sees
            # exactly one still-feasible event claims it.
            for kind in necessity_kinds:
                for run in self.runs:
                    run_bond = bond(run)
                    if not needs_event(run, kind):
                        continue
                    options = [
                        event
                        for event in unassigned[kind]
                        if run in self._event_candidate_runs(kind, event, bond)
                    ]
                    if len(options) == 1:
                        assign(run, kind, options[0])
                        unassigned[kind].remove(options[0])
                        progressed = True

        # Everything still unassigned here is genuinely ambiguous: record it
        # on each run that could plausibly own it, never picking one.
        for kind in kinds:
            for event in unassigned[kind]:
                for run in self._event_candidate_runs(kind, event, bond):
                    ambiguous = bond(run)["ambiguous"][kind]
                    if event not in ambiguous:
                        ambiguous.append(event)

        for run in self.runs:
            bond(run)["advances"].sort(key=lambda item: item.sequence)
        return bond_for

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

    def _event_candidate_runs(self, kind: EventKind, event: Any, bond: Any) -> list[Any]:
        payload = event.payload or {}
        candidates: list[Any] = []
        for run in self.runs:
            run_bond = bond(run)
            if kind == EventKind.PULL_PLANNED:
                if run_bond["plan"] is not None:
                    continue
                if not _timestamp_consistent(run.created_at, event.at):
                    continue
                if payload.get("transfer_code") is not None and run.transfer_code != str(payload["transfer_code"]):
                    continue
                if payload.get("steps") is not None and len(run.steps) != int(payload["steps"]):
                    continue
                if run_bond["started"] is not None and event.sequence > run_bond["started"].sequence:
                    continue
                if run_bond["completed"] is not None and event.sequence > run_bond["completed"].sequence:
                    continue
            elif kind == EventKind.PULL_RUN_STARTED:
                if run_bond["started"] is not None:
                    continue
                if not _timestamp_consistent(run.started_at, event.at):
                    continue
                if payload.get("total_steps") is not None and len(run.steps) != int(payload["total_steps"]):
                    continue
                if run_bond["plan"] is not None and event.sequence < run_bond["plan"].sequence:
                    continue
                if run_bond["completed"] is not None and event.sequence > run_bond["completed"].sequence:
                    continue
            else:  # PULL_RUN_ADVANCED
                cursor = payload.get("current_step")
                if cursor is None or int(cursor) >= len(run.steps):
                    continue
                if run.started_at is None:
                    continue
                if run_bond["plan"] is not None and event.sequence < run_bond["plan"].sequence:
                    continue
                if run_bond["started"] is not None and event.sequence < run_bond["started"].sequence:
                    continue
                if run_bond["completed"] is not None and event.sequence > run_bond["completed"].sequence:
                    continue
                if run_bond["advances"]:
                    last_cursor = int((run_bond["advances"][-1].payload or {}).get("current_step", -1))
                    if int(cursor) <= last_cursor:
                        continue
            candidates.append(run)
        return candidates

    def _link_depart_events(self) -> tuple[dict[int, Any], dict[int, list[Any]]]:
        """Assign TRAIN_DEPARTED events only when the owner is unique.

        Returns the unique event per train and, for every train that could
        plausibly own an unassigned event, the candidate events kept as an
        evidence gap instead of being guessed.
        """

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
            # Entity-only fragments are ordered by causal phase first so the
            # trajectory stays readable even when wall clocks disagree.
            _anchor_fallback_key(
                entry,
                (
                    _PHASE_RANK["RECEIVED"],
                    0,
                    self.intakes.index(intake),
                    intake.code,
                    "RECEIVED",
                ),
            )
        entry["intake_code"] = intake.code
        entry["location"] = "INTAKE"
        entry["detail"] = f"received with intake {intake.code} from route {intake.route}"
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
        if event is not None:
            _anchor_event(entry, event)
            entry["_sub"] = list(intake.consist).index(car_code) if car_code in intake.consist else 0
        else:
            intake_order = self.intakes.index(intake) if intake is not None and intake in self.intakes else 9999
            _anchor_fallback_key(
                entry,
                (
                    _PHASE_RANK["CLASSIFIED"],
                    0,
                    intake_order,
                    track_code,
                    "CLASSIFIED",
                ),
            )
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

        if car_code in planned_codes:
            entries.append(self._reserved_entry(car_code, run, bond, gaps))

        boundaries = self._step_boundaries(run, bond, executed)
        pull_count = 0
        missing_time = False
        ambiguous_time = False
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
                entry["_sub"] = step_index
            else:
                entry["evidence"] = "entity"
                ambiguous_events = bond["ambiguous"][EventKind.PULL_RUN_ADVANCED]
                if bond["completed"] is None and bond["ambiguous"][EventKind.PULL_RUN_COMPLETED]:
                    ambiguous_events = ambiguous_events + bond["ambiguous"][EventKind.PULL_RUN_COMPLETED]
                if ambiguous_events:
                    ambiguous_time = True
                else:
                    missing_time = True
                # All moves of one run share the run-group anchor (reserved)
                # and sort within the group by step index.
                _anchor_fallback_key(
                    entry,
                    (
                        _PHASE_RANK["RESERVED"],
                        self.runs.index(run),
                        step_index,
                        run.code,
                        phase,
                    ),
                )
            entries.append(entry)
        if ambiguous_time:
            gaps.append(
                _gap(
                    "AMBIGUOUS_MOVE_EVENT",
                    car_code,
                    f"executed move(s) of run {run.code} could not be matched to a unique "
                    "advance/completion journal event; no other run's event is used",
                    run_code=run.code,
                )
            )
        elif missing_time:
            gaps.append(
                _gap(
                    "MISSING_MOVE_TIME",
                    car_code,
                    f"executed move(s) of run {run.code} have no advance/completion journal event",
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
            entry["_sub"] = self._planned_position(run, car_code) or 0
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
            _anchor_fallback_key(
                entry,
                (
                    _PHASE_RANK["RESERVED"],
                    self.runs.index(run),
                    self._run_first_step_index(run, car_code),
                    f"0-{run.code}",
                    "RESERVED",
                ),
            )
        return entry

    def _run_first_step_index(self, run: Any, car_code: str) -> int:
        """First step index involving the car, for causal fallback ordering."""

        for index, step in enumerate(run.steps):
            if step.car_code == car_code:
                return index
        return len(run.steps)

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
                _anchor_fallback_key(
                    entry,
                    (
                        _PHASE_RANK["DEPARTED"],
                        self.outbounds.index(outbound),
                        outbound.assembled_car_codes.index(car_code),
                        outbound.code,
                        "DEPARTED",
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
        """Map each executed step index to the event that completed its batch."""

        boundary_events: list[tuple[int, Any]] = []
        for event in bond.get("advances", []):
            cursor = int((event.payload or {}).get("current_step", 0))
            boundary_events.append((cursor, event))
        completed = bond.get("completed")
        if completed is not None:
            boundary_events.append((len(run.steps), completed))
        boundary_events.sort(key=lambda item: item[0])
        result: dict[int, Any] = {}
        for step_index in range(executed):
            match = next((event for cursor, event in boundary_events if cursor > step_index), None)
            if match is not None:
                result[step_index] = match
        return result


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
        # Internal ordering fields, stripped before the document is returned.
        "_tier": 1,
        "_seq": 0,
        "_sub": 0,
        "_fallback": (999, 9999, 9999, "", ""),
    }


def _anchor_event(entry: dict[str, Any], event: Any) -> None:
    entry["at"] = event.at
    entry["event_sequence"] = event.sequence
    entry["shift_code"] = event.shift_code or None
    entry["evidence"] = "event"
    entry["_tier"] = 0
    entry["_seq"] = event.sequence


def _anchor_fallback_key(entry: dict[str, Any], key: tuple[Any, ...]) -> None:
    entry["_tier"] = 1
    entry["_fallback"] = tuple(key)


def _entry_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (entry["_tier"], entry["_seq"], entry["_sub"], *entry["_fallback"])


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
