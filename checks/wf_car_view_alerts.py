"""Workflow check: inconsistent car references surface sourced alerts.

The normal API never writes inconsistent state, so this check drives a yard
to a healthy DEPARTED state, edits the persisted JSON workspace directly to
introduce conflicting claims, restarts a server against the same data
directory, and verifies that ``GET /api/cars/{code}`` names every conflicting
source instead of silently picking one field.  Queries must still be
read-only and a departed car must never count as in yard.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from support import ApiClient, RunningServer


def _car(code: str, destination: str) -> dict[str, object]:
    return {
        "code": code,
        "kind": "BOX",
        "destination": destination,
        "loaded": True,
        "length_m": 18,
        "danger_class": "NONE",
    }


def _build_departed_yard(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-06", "dispatcher": "LIN", "opened_at": "2026-09-14T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-60",
            "route": "RAIL-60",
            "arrival_at": "2026-09-14T09:00:00Z",
            "cars": [_car("C-N4-61", "N4"), _car("C-S2-60", "S2")],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-60/classify", {})
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-60", "destination": "N4", "car_codes": ["C-N4-61"]},
    )
    sequenced = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-60/sequencer",
        {"transfer_code": "X1"},
    )
    run_code = sequenced["pull_run"]["code"]
    api.expect_ok("POST", f"/api/pull-runs/{run_code}/advance", {"steps": 10})
    api.expect_ok("POST", "/api/outbound-trains/OB-60/depart", {})


def _tamper_state(data_dir: Path) -> None:
    state_path = data_dir / "yard-state.json"
    doc = json.loads(state_path.read_text(encoding="utf-8"))
    cars = {str(item["code"]): item for item in doc["cars"]}

    # Departed car claims to be standing again and is double-parked on N4-A
    # while still on the departed train consist.
    cars["C-N4-61"]["state"] = "STANDING"
    cars["C-N4-61"]["location"] = "N4-A"
    for track in doc["tracks"]:
        if track["code"] == "N4-A":
            track["stack"].append("C-N4-61")

    # Healthy car gets a location that references nothing.
    cars["C-S2-60"]["location"] = "GHOST-9"

    state_path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _alerts_by_source(view: dict[str, object]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for item in view.get("consistency", []):  # type: ignore[union-attr]
        grouped.setdefault(str(item["source"]), set()).add(str(item["code"]))
    return grouped


def run_check() -> None:
    persistent = tempfile.TemporaryDirectory(prefix="switchyard-alert-")
    data_dir = Path(persistent.name) / "data"
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        api = server.api
        _build_departed_yard(api)
        departed = api.expect_ok("GET", "/api/cars/C-N4-61")
        assert departed["phase"] == "DEPARTED" and departed["alert_count"] == 0
    finally:
        server.stop()

    _tamper_state(data_dir)

    restarted = RunningServer(data_dir=data_dir)
    try:
        restarted.wait_ready()
        api = restarted.api

        yard = api.expect_ok("GET", "/api/yard")
        version_before = int(yard["metrics"]["version"])

        view = api.expect_ok("GET", "/api/cars/C-N4-61")
        # Physical truth still says the car left on OB-60; it must not look in-yard.
        assert view["phase"] == "DEPARTED", view["phase"]
        assert view["in_yard"] is False
        assert view["claimed_phase"] == "IN_YARD"
        assert view["phase_conflict"] is True
        grouped = _alerts_by_source(view)
        assert "state" in grouped and "phase-conflict" in grouped["state"], grouped
        assert "ref" in grouped and "duplicate-physical-location" in grouped["ref"], grouped
        assert "yard" in grouped and "yard-overview-mismatch" in grouped["yard"], grouped
        duplicate = next(item for item in view["consistency"] if item["code"] == "duplicate-physical-location")
        containers = {item["code"] for item in duplicate["details"]["containers"]}
        assert containers == {"N4-A", "OB-60"}, containers

        ghost = api.expect_ok("GET", "/api/cars/C-S2-60")
        ghost_alerts = _alerts_by_source(ghost)
        assert "state" in ghost_alerts and "location-mismatch" in ghost_alerts["state"], ghost_alerts

        yard_after = api.expect_ok("GET", "/api/yard")
        assert int(yard_after["metrics"]["version"]) == version_before

        missing = api.expect_error("GET", "/api/cars/C-NOPE-9")
        assert missing["code"] == "NOT_FOUND"
    finally:
        restarted.stop()
        persistent.cleanup()


if __name__ == "__main__":
    run_check()
    print("OK wf_car_view_alerts")
    raise SystemExit(0)
