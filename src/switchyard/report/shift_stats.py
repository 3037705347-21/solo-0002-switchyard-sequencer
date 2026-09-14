"""Per-shift operation statistics rebuilt from the raw event trail.

Every metric here is derived solely from shift-filtered yard events, sorted by
their journal sequence. Nothing is read from live car or track state, so the
same numbers can be recomputed from an archived journal.

Missing or malformed timestamps never collapse to zero: a timing value stays
``None`` and an entry is appended to ``issues``. Work that is still in progress
(no terminal event yet) stays in ``in_progress`` and is excluded from averages
and maxima. Failed attempts are counted as attempts but their timings are not
averaged.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.timeutil import parse_iso

_RUN_RE = re.compile(r"RUN-(.+?)(?:-R(\d+))?$")


def _event_view(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return {
            "sequence": event.get("sequence"),
            "at": event.get("at"),
            "shift_code": event.get("shift_code"),
            "kind": str(event.get("kind")),
            "message": str(event.get("message", "")),
            "payload": dict(event.get("payload") or {}),
        }
    return {
        "sequence": getattr(event, "sequence", None),
        "at": getattr(event, "at", ""),
        "shift_code": getattr(event, "shift_code", ""),
        "kind": str(getattr(event, "kind")),
        "message": str(getattr(event, "message", "")),
        "payload": dict(getattr(event, "payload", {}) or {}),
    }


def _parse_time(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError):
        return None


def _elapsed(start: str | None, end: str | None) -> int | None:
    if start is None or end is None:
        return None
    try:
        seconds = int((parse_iso(end) - parse_iso(start)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
    if seconds < 0:
        return None
    return seconds


def _avg_max(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"average_s": None, "maximum_s": None, "samples": 0}
    return {"average_s": round(sum(values) / len(values), 2), "maximum_s": max(values), "samples": len(values)}


def _stage_block() -> dict[str, Any]:
    return {
        "average_s": None,
        "maximum_s": None,
        "samples": 0,
        "completed": 0,
        "in_progress": 0,
        "failed": 0,
    }


def _move_counts(steps: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"buffer": 0, "return": 0, "pull": 0}
    for step in steps:
        verb = str(step.get("verb", "")).upper()
        if verb == "BUFFER":
            counts["buffer"] += 1
        elif verb == "RETURN":
            counts["return"] += 1
        elif verb == "PULL":
            counts["pull"] += 1
    return counts


def _payload_int(payload: dict[str, Any], key: str, default: int = 0) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _payload_car_codes(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("car_codes")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def shift_statistics_from_events(events: list[Any], shift_code: str) -> dict[str, Any]:
    raw = [view for view in (_event_view(event) for event in events) if view["shift_code"] == shift_code]
    raw.sort(key=lambda item: (item["sequence"] is None, item["sequence"] or 0))

    issues: list[dict[str, Any]] = []

    intakes: dict[str, dict[str, Any]] = {}
    outbounds: dict[str, dict[str, Any]] = {}
    runs: dict[str, dict[str, Any]] = {}
    cars: dict[str, dict[str, Any]] = {}
    tracks: dict[str, dict[str, Any]] = {}

    def intake_for(event: dict[str, Any], payload: dict[str, Any]) -> str | None:
        code = payload.get("intake_code")
        if isinstance(code, str) and code:
            return code
        match = re.search(r"intake\s+([A-Za-z0-9_-]+)", event["message"])
        if match:
            return match.group(1)
        issues.append({"scope": "intake", "code": None, "reason": "event missing intake_code", "kind": event["kind"]})
        return None

    def outbound_for(payload: dict[str, Any]) -> str | None:
        code = payload.get("outbound_code")
        if isinstance(code, str) and code:
            return code
        return None

    def run_for(payload: dict[str, Any], message: str) -> tuple[str | None, int]:
        code = payload.get("run_code")
        attempt = payload.get("attempt")
        attempt_value = attempt if isinstance(attempt, int) and attempt >= 1 else None
        if isinstance(code, str) and code:
            if attempt_value is None:
                match = _RUN_RE.match(code)
                attempt_value = int(match.group(2)) if match and match.group(2) else 1
            return code, attempt_value
        match = re.search(r"pull run\s+([A-Za-z0-9_.-]+)", message)
        if match:
            found = match.group(1)
            suffix = _RUN_RE.match(found)
            if attempt_value is None:
                attempt_value = int(suffix.group(2)) if suffix and suffix.group(2) else 1
            return found, attempt_value
        issues.append({"scope": "pull_run", "code": None, "reason": "event missing run_code", "kind": "PULL_RUN"})
        return None, attempt_value or 1

    def ensure_track(code: str) -> dict[str, Any]:
        return tracks.setdefault(code, {"code": code, "placements": 0, "releases": 0, "turnovers": 0})

    for event in raw:
        kind = event["kind"]
        payload = event["payload"]
        event_at = _parse_time(event["at"])
        if event_at is None:
            issues.append(
                {
                    "scope": "event",
                    "code": event["sequence"],
                    "reason": "missing or unparseable event timestamp",
                    "kind": kind,
                }
            )

        if kind == "TRAIN_RECEIVED":
            code = intake_for(event, payload)
            if code is None:
                continue
            record = intakes.setdefault(code, {"code": code})
            record["received_at"] = event_at
            # The manifest carries the train's physical arrival time, which is
            # the reference for car waiting/departure dwell. The event time is
            # only a fallback when the manifest timestamp is unusable.
            arrival_at = _parse_time(payload.get("arrival_at")) if isinstance(payload.get("arrival_at"), str) else None
            if arrival_at is None and ("arrival_at" in payload):
                issues.append(
                    {
                        "scope": "intake",
                        "code": code,
                        "reason": "unparseable arrival_at timestamp; falling back to receive event time",
                    }
                )
            car_arrival_at = arrival_at or event_at
            record["arrival_at"] = arrival_at
            record["car_arrival_at"] = car_arrival_at
            record["car_count"] = _payload_int(payload, "car_count")
            car_items = payload.get("cars")
            car_codes: list[str] = []
            if isinstance(car_items, list) and car_items:
                for item in car_items:
                    if not isinstance(item, dict):
                        continue
                    car_code = item.get("code")
                    if not isinstance(car_code, str) or not car_code:
                        continue
                    car_codes.append(car_code)
                    cars[car_code] = {
                        "code": car_code,
                        "kind": str(item.get("kind") or "UNKNOWN") or "UNKNOWN",
                        "destination": str(item.get("destination") or "UNKNOWN") or "UNKNOWN",
                        "arrived_at": car_arrival_at,
                        "actual_arrival_at": arrival_at,
                        "received_event_at": event_at,
                    }
            else:
                car_codes = _payload_car_codes(payload)
                for car_code in car_codes:
                    cars.setdefault(
                        car_code,
                        {
                            "code": car_code,
                            "kind": "UNKNOWN",
                            "destination": "UNKNOWN",
                            "arrived_at": car_arrival_at,
                            "actual_arrival_at": arrival_at,
                            "received_event_at": event_at,
                        },
                    )
                if not car_codes:
                    issues.append(
                        {"scope": "intake", "code": code, "reason": "received event lists no cars; detail degraded"}
                    )
            record["car_codes"] = car_codes

        elif kind == "TRAIN_CLASSIFIED":
            code = intake_for(event, payload)
            if code is None:
                continue
            record = intakes.setdefault(code, {"code": code})
            spots = payload.get("spots")
            placed: list[dict[str, Any]] = []
            if isinstance(spots, list):
                for item in spots:
                    if isinstance(item, dict) and isinstance(item.get("car_code"), str):
                        placed.append(item)
            unplaced = payload.get("unplaced")
            unplaced_codes = [str(item) for item in unplaced] if isinstance(unplaced, list) else []
            record["classified_event"] = True
            record["classified_at"] = event_at
            record["placed_count"] = len(placed)
            record["unplaced_count"] = len(unplaced_codes)
            record["unplaced"] = unplaced_codes
            record["partial"] = bool(unplaced_codes)
            record["spots"] = [
                {"car_code": str(item["car_code"]), "track_code": str(item.get("track_code"))}
                for item in placed
            ]
            for item in placed:
                track_code = item.get("track_code")
                if isinstance(track_code, str) and track_code:
                    ensure_track(track_code)["placements"] += 1
                car_code = str(item["car_code"])
                car = cars.get(car_code)
                if car is not None:
                    # Structural classification counts even when the event
                    # timestamp is unusable; only timing degrades.
                    car["classified"] = True
                    car["classified_at"] = event_at
                    if isinstance(track_code, str):
                        car["track_code"] = track_code

        elif kind == "TRAIN_CREATED":
            code = outbound_for(payload)
            if code is None:
                issues.append({"scope": "outbound", "code": None, "reason": "event missing outbound_code"})
                continue
            destination = str(payload.get("destination") or "UNKNOWN")
            record = outbounds.setdefault(code, {"code": code})
            record["created_at"] = event_at
            record["destination"] = destination
            record["planned_car_codes"] = _payload_car_codes(payload)

        elif kind in {"PULL_PLANNED", "PULL_RUN_RETRIED"}:
            run_code, attempt = run_for(payload, event["message"])
            outbound_code = outbound_for(payload)
            if run_code is None or outbound_code is None:
                issues.append(
                    {"scope": "pull_run", "code": run_code, "reason": "planning event missing run or outbound code"}
                )
                continue
            record = runs.setdefault(run_code, {"code": run_code, "outbound_code": outbound_code})
            record["planned_at"] = event_at
            record["attempt"] = attempt
            record["steps_planned"] = _payload_int(payload, "steps")
            outbound = outbounds.setdefault(outbound_code, {"code": outbound_code})
            outbound.setdefault("attempts", []).append(run_code)
            if kind == "PULL_RUN_RETRIED":
                outbound["retries"] = outbound.get("retries", 0) + 1
                failed_run = payload.get("failed_run_code")
                if isinstance(failed_run, str):
                    record["replaces_run_code"] = failed_run

        elif kind == "PULL_RUN_STARTED":
            run_code, attempt = run_for(payload, event["message"])
            outbound_code = outbound_for(payload)
            if run_code is None:
                continue
            record = runs.setdefault(run_code, {"code": run_code, "outbound_code": outbound_code})
            if outbound_code is not None:
                record["outbound_code"] = outbound_code
            record["attempt"] = attempt
            record["started_at"] = event_at

        elif kind in {"PULL_RUN_ADVANCED", "PULL_RUN_COMPLETED", "PULL_RUN_FAILED"}:
            run_code, attempt = run_for(payload, event["message"])
            outbound_code = outbound_for(payload)
            if run_code is None:
                continue
            record = runs.setdefault(run_code, {"code": run_code, "outbound_code": outbound_code})
            if outbound_code is not None:
                record["outbound_code"] = outbound_code
            record["attempt"] = attempt
            if kind == "PULL_RUN_FAILED":
                # The failure payload lists every applied step that was rolled
                # back, including buffer moves committed by earlier advances.
                rolled = payload.get("rolled_back_steps")
                if isinstance(rolled, list):
                    rolled_items = [item for item in rolled if isinstance(item, dict)]
                else:
                    executed = payload.get("executed_steps")
                    rolled_items = [item for item in executed if isinstance(item, dict)] if isinstance(executed, list) else []
                record.setdefault("move_segments", []).append(
                    {"terminal": kind, "steps": rolled_items, "at": event_at}
                )
                record["state"] = "FAILED"
                failed_stamp = _parse_time(payload.get("failed_at"))
                record["failed_at"] = failed_stamp or event_at
                record["error"] = str(payload.get("error") or "execution failure")
            else:
                steps = payload.get("executed_steps")
                step_items = [item for item in steps if isinstance(item, dict)] if isinstance(steps, list) else []
                record.setdefault("move_segments", []).append(
                    {"terminal": kind, "steps": step_items, "at": event_at}
                )
            if kind == "PULL_RUN_COMPLETED":
                record["state"] = "COMPLETED"
                record["completed_at"] = event_at
                completed_stamp = _parse_time(payload.get("completed_at"))
                if completed_stamp is not None:
                    record["completed_at"] = completed_stamp
                started_stamp = _parse_time(payload.get("started_at"))
                if started_stamp is not None:
                    record["started_at"] = started_stamp
                assembled = _payload_car_codes(payload) or [
                    str(item["car_code"])
                    for item in step_items
                    if isinstance(item, dict) and str(item.get("verb", "")).upper() == "PULL"
                ]
                record["assembled_car_codes"] = assembled
                for item in step_items:
                    if str(item.get("verb", "")).upper() != "PULL":
                        continue
                    car_code = str(item.get("car_code", ""))
                    if not car_code:
                        continue
                    track_code = item.get("source_code")
                    if isinstance(track_code, str) and track_code:
                        ensure_track(track_code)["releases"] += 1
                    car = cars.get(car_code)
                    if car is not None:
                        car["assembled_at"] = event_at
                        car["pulled_from_track"] = str(track_code) if isinstance(track_code, str) else None
            elif kind == "PULL_RUN_ADVANCED":
                record["state"] = "RUNNING"

        elif kind == "TRAIN_DEPARTED":
            code = outbound_for(payload)
            if code is None:
                issues.append({"scope": "outbound", "code": None, "reason": "departure event missing outbound_code"})
                continue
            record = outbounds.setdefault(code, {"code": code})
            record["departed_at"] = event_at
            departed_stamp = _parse_time(payload.get("departed_at"))
            if departed_stamp is not None:
                record["departed_at"] = departed_stamp
            if "destination" not in record and isinstance(payload.get("destination"), str):
                record["destination"] = str(payload["destination"])
            for car_code in _payload_car_codes(payload):
                car = cars.get(car_code)
                if car is not None:
                    car["departed_at"] = record["departed_at"]

    closed = any(event["kind"] == "SHIFT_CLOSED" for event in raw)

    # ---- stage: intake handling (receive -> classify) -------------------
    intake_details: list[dict[str, Any]] = []
    intake_durations: list[int] = []
    intake_wait_durations: list[int] = []
    intake_in_progress = 0
    intake_completed = 0
    for code in sorted(intakes):
        record = intakes[code]
        dwell = _elapsed(record.get("received_at"), record.get("classified_at"))
        arrival_wait = _elapsed(record.get("car_arrival_at"), record.get("classified_at"))
        detail = {
            "intake_code": code,
            "arrival_at": record.get("arrival_at"),
            "received_at": record.get("received_at"),
            "classified_at": record.get("classified_at"),
            "duration_s": dwell,
            "arrival_to_classified_s": arrival_wait,
            "placed_count": record.get("placed_count"),
            "unplaced_count": record.get("unplaced_count"),
            "partial": record.get("partial", False),
            "car_codes": list(record.get("car_codes", [])),
        }
        if not record.get("classified_event"):
            intake_in_progress += 1
            detail["status"] = "OPEN_OR_PARTIAL"
        else:
            intake_completed += 1
            detail["status"] = "PARTIAL" if record.get("partial") else "CLASSIFIED"
            if dwell is None:
                issues.append(
                    {
                        "scope": "intake",
                        "code": code,
                        "reason": "classification duration uncomputable; timestamp missing or out of order",
                    }
                )
            else:
                intake_durations.append(dwell)
            if arrival_wait is None:
                issues.append(
                    {
                        "scope": "intake",
                        "code": code,
                        "reason": "arrival-to-classified duration uncomputable; arrival timestamp missing",
                    }
                )
            else:
                intake_wait_durations.append(arrival_wait)
        intake_details.append(detail)
    intake_stage = _stage_block()
    intake_stage.update(_avg_max(intake_durations))
    intake_stage["completed"] = intake_completed
    intake_stage["in_progress"] = intake_in_progress
    intake_stage["partial"] = sum(1 for item in intake_details if item["partial"])
    intake_stage["total"] = len(intake_details)

    intake_wait_stage = _stage_block()
    intake_wait_stage.update(_avg_max(intake_wait_durations))
    intake_wait_stage["completed"] = len(intake_wait_durations)
    intake_wait_stage["in_progress"] = intake_in_progress
    intake_wait_stage["total"] = len(intake_details)

    # ---- stage: pull run planning delay and execution -------------------
    run_details: list[dict[str, Any]] = []
    execution_durations: list[int] = []
    planning_durations: list[int] = []
    run_in_progress = 0
    failed_runs = 0
    completed_runs = 0
    for code in sorted(runs):
        record = runs[code]
        state = record.get("state", "QUEUED")
        started_at = record.get("started_at")
        completed_at = record.get("completed_at")
        planning_s = _elapsed(record.get("planned_at"), started_at)
        detail: dict[str, Any] = {
            "run_code": code,
            "outbound_code": record.get("outbound_code"),
            "attempt": record.get("attempt", 1),
            "state": state,
            "planned_at": record.get("planned_at"),
            "started_at": started_at,
            "completed_at": completed_at,
            "failed_at": record.get("failed_at"),
            "planning_delay_s": planning_s,
            "execution_s": None,
        }
        segments = record.get("move_segments", [])
        total = {"buffer": 0, "return": 0, "pull": 0}
        rolled_back = {"buffer": 0, "return": 0, "pull": 0}
        # The terminal event is authoritative: COMPLETED lists every run step
        # and FAILED lists every applied step that was rolled back (including
        # moves committed by earlier ADVANCED segments). Skip those subsumed
        # ADVANCED fragments to avoid double counting.
        has_terminal = any(
            segment["terminal"] in {"PULL_RUN_COMPLETED", "PULL_RUN_FAILED"} for segment in segments
        )
        for segment in segments:
            if has_terminal and segment["terminal"] == "PULL_RUN_ADVANCED":
                continue
            counts = _move_counts(segment["steps"])
            target = rolled_back if state == "FAILED" else total
            for key in target:
                target[key] += counts[key]
        detail["moves"] = total
        detail["rolled_back_moves"] = rolled_back
        if any(rolled_back.values()):
            detail["rolled_back_move_count"] = sum(rolled_back.values())
        if state == "COMPLETED":
            completed_runs += 1
            execution_s = _elapsed(started_at, completed_at)
            detail["execution_s"] = execution_s
            if execution_s is None:
                issues.append(
                    {"scope": "pull_run", "code": code, "reason": "completed run missing start or complete timestamp"}
                )
            else:
                execution_durations.append(execution_s)
            if planning_s is None:
                issues.append({"scope": "pull_run", "code": code, "reason": "missing planning or started timestamp"})
            else:
                planning_durations.append(planning_s)
        elif state == "FAILED":
            failed_runs += 1
            detail["error"] = record.get("error")
        else:
            run_in_progress += 1
        run_details.append(detail)

    execution_stage = _stage_block()
    execution_stage.update(_avg_max(execution_durations))
    execution_stage["completed"] = completed_runs
    execution_stage["failed"] = failed_runs
    execution_stage["in_progress"] = run_in_progress
    execution_stage["total"] = len(run_details)

    planning_stage = _stage_block()
    planning_stage.update(_avg_max(planning_durations))
    planning_stage["completed"] = completed_runs
    planning_stage["failed"] = failed_runs
    planning_stage["in_progress"] = run_in_progress
    planning_stage["total"] = len(run_details)

    # ---- buffering and repeated attempts -------------------------------
    # Moves of a failed attempt are all rolled back (FAILED lists every
    # applied step, including ADVANCED ones committed earlier); moves of a
    # completed run come from the terminal COMPLETED event. Both terminal
    # events subsume earlier ADVANCED fragments, which are skipped.
    buffer_total = 0
    return_total = 0
    pull_total = 0
    failed_move_totals = {"buffer": 0, "return": 0, "pull": 0}
    for record in runs.values():
        run_failed = record.get("state") == "FAILED"
        segments = record.get("move_segments", [])
        has_terminal = any(
            segment["terminal"] in {"PULL_RUN_COMPLETED", "PULL_RUN_FAILED"} for segment in segments
        )
        for segment in segments:
            if has_terminal and segment["terminal"] == "PULL_RUN_ADVANCED":
                continue
            counts = _move_counts(segment["steps"])
            if run_failed:
                for key in failed_move_totals:
                    failed_move_totals[key] += counts[key]
            else:
                buffer_total += counts["buffer"]
                return_total += counts["return"]
                pull_total += counts["pull"]

    outbound_details: list[dict[str, Any]] = []
    retry_outbounds = 0
    departed_outbounds = 0
    for code in sorted(outbounds):
        record = outbounds[code]
        attempts = record.get("attempts", [])
        attempt_records = [runs[run_code] for run_code in attempts if run_code in runs]
        terminal_states = [item.get("state") for item in attempt_records]
        retries = record.get("retries", sum(1 for state in terminal_states if state == "FAILED"))
        if attempts:
            retry_outbounds += 1 if retries else 0
        if record.get("departed_at"):
            departed_outbounds += 1
        outbound_details.append(
            {
                "outbound_code": code,
                "destination": record.get("destination", "UNKNOWN"),
                "created_at": record.get("created_at"),
                "departed_at": record.get("departed_at"),
                "attempt_run_codes": list(attempts),
                "attempts": len(attempts) if attempts else (1 if record.get("created_at") else 0),
                "retry_count": retries,
                "failed_attempts": sum(1 for state in terminal_states if state == "FAILED"),
                "departed": record.get("departed_at") is not None,
            }
        )

    # ---- per-car arrival -> assembly/departure dwell -------------------
    # Dwell starts at the manifest arrival time (actual physical arrival),
    # not at the receive-event time.
    car_details: list[dict[str, Any]] = []
    waiting_durations: list[int] = []
    assembly_durations: list[int] = []
    departure_durations: list[int] = []
    cars_waiting = 0
    cars_assembled = 0
    cars_departed = 0
    for code in sorted(cars):
        record = cars[code]
        arrived_at = record.get("arrived_at")
        actual_arrival_at = record.get("actual_arrival_at")
        classified_at = record.get("classified_at")
        assembled_at = record.get("assembled_at")
        departed_at = record.get("departed_at")
        waiting_s = _elapsed(arrived_at, classified_at)
        assembly_s = _elapsed(arrived_at, assembled_at)
        departure_s = _elapsed(arrived_at, departed_at)
        if arrived_at is None:
            issues.append({"scope": "car", "code": code, "reason": "car has no arrival timestamp"})
        if classified_at is not None:
            cars_waiting += 1
            if waiting_s is None:
                issues.append({"scope": "car", "code": code, "reason": "arrival-to-classified duration uncomputable"})
            else:
                waiting_durations.append(waiting_s)
        if assembled_at is not None:
            cars_assembled += 1
            if assembly_s is None:
                issues.append({"scope": "car", "code": code, "reason": "assembly duration uncomputable"})
            else:
                assembly_durations.append(assembly_s)
        if departed_at is not None:
            cars_departed += 1
            if departure_s is None:
                issues.append({"scope": "car", "code": code, "reason": "departure duration uncomputable"})
            else:
                departure_durations.append(departure_s)
        car_details.append(
            {
                "car_code": code,
                "kind": record.get("kind", "UNKNOWN"),
                "destination": record.get("destination", "UNKNOWN"),
                "arrival_at": actual_arrival_at,
                "arrival_time_source": "manifest" if actual_arrival_at is not None else "receive_event",
                "received_event_at": record.get("received_event_at"),
                "arrived_at": arrived_at,
                "classified_at": classified_at,
                "assembled_at": assembled_at,
                "departed_at": departed_at,
                "track_code": record.get("track_code"),
                "waiting_s": waiting_s,
                "assembly_s": assembly_s,
                "departure_s": departure_s,
            }
        )

    waiting_stage = _stage_block()
    waiting_stage.update(_avg_max(waiting_durations))
    waiting_stage["completed"] = cars_waiting
    waiting_stage["in_progress"] = len(cars) - cars_waiting
    waiting_stage["total"] = len(cars)

    assembly_stage = _stage_block()
    assembly_stage.update(_avg_max(assembly_durations))
    assembly_stage["completed"] = cars_assembled
    assembly_stage["in_progress"] = len(cars) - cars_assembled
    assembly_stage["total"] = len(cars)

    departure_stage = _stage_block()
    departure_stage.update(_avg_max(departure_durations))
    departure_stage["completed"] = cars_departed
    departure_stage["in_progress"] = len(cars) - cars_departed
    departure_stage["total"] = len(cars)

    # ---- track turnover ------------------------------------------------
    track_turnover: list[dict[str, Any]] = []
    for code in sorted(tracks):
        record = tracks[code]
        placements = record["placements"]
        releases = record["releases"]
        record["turnovers"] = min(placements, releases)
        track_turnover.append(
            {
                "track_code": code,
                "placements": placements,
                "releases": releases,
                "turnovers": min(placements, releases),
                "cars_remaining": placements - releases,
            }
        )

    # ---- destination distribution --------------------------------------
    destination_buckets: dict[str, dict[str, int]] = {}

    def bucket(name: str) -> dict[str, int]:
        return destination_buckets.setdefault(
            name, {"received": 0, "classified": 0, "assembled": 0, "departed": 0}
        )

    for record in cars.values():
        name = record.get("destination") or "UNKNOWN"
        entry = bucket(name)
        entry["received"] += 1
        if record.get("classified"):
            entry["classified"] += 1
        if record.get("assembled_at") is not None:
            entry["assembled"] += 1
        if record.get("departed_at") is not None:
            entry["departed"] += 1
    destination_distribution = [
        {"destination": name, **counts} for name, counts in sorted(destination_buckets.items())
    ]

    stages = {
        "intake_handling_s": intake_stage,
        "arrival_to_classified_s": intake_wait_stage,
        "car_waiting_s": waiting_stage,
        "pull_planning_delay_s": planning_stage,
        "pull_execution_s": execution_stage,
        "car_to_assembly_s": assembly_stage,
        "car_to_departure_s": departure_stage,
    }

    return {
        "shift_code": shift_code,
        "closed": closed,
        "generated_from": "events",
        "event_count": len(raw),
        "stages": stages,
        "buffering": {
            "buffer_moves": buffer_total,
            "return_moves": return_total,
            "pull_moves": pull_total,
            "moves_in_failed_attempts": dict(failed_move_totals),
            "buffer_moves_in_failed_attempts": failed_move_totals["buffer"],
        },
        "retries": {
            "failed_runs": failed_runs,
            "retry_runs_planned": sum(1 for run in runs.values() if run.get("attempt", 1) > 1),
            "outbounds_with_retries": retry_outbounds,
            "outbounds_departed": departed_outbounds,
        },
        "track_turnover": track_turnover,
        "destination_distribution": destination_distribution,
        "details": {
            "intakes": intake_details,
            "pull_runs": run_details,
            "outbounds": outbound_details,
            "cars": car_details,
        },
        "issues": issues,
    }


def shift_statistics(workspace: Any, shift_code: str) -> dict[str, Any]:
    """Rebuild statistics for one shift from the workspace event trail."""
    return shift_statistics_from_events(list(workspace.events), shift_code)


__all__ = ["shift_statistics", "shift_statistics_from_events"]
