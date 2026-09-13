"""Persistence consistency checks for the state/journal commit protocol.

Exercises real server restarts against one on-disk data directory:

1. crash after state, before journal  -> roll-forward on next startup
2. crash with torn journal batch       -> batch completed from state
3. duplicate journal event/marker      -> duplicates stripped, data intact
4. several commands with restarts      -> continuous history fully recovers
5. unrecoverable divergence            -> service refuses to serve history
6. legacy (pre-framing) data files     -> migrated and still readable

Run: PYTHONPATH=src python3 checks/wf_persistence_recovery.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
from pathlib import Path

from support import SRC_DIR, RunningServer

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from switchyard.storage.digest import event_content_hash  # noqa: E402

SHIFT = {"code": "SHIFT-RC", "dispatcher": "KE", "opened_at": "2026-09-13T08:00:00Z"}
INTAKE_A = {
    "code": "INT-A",
    "route": "RAIL-A",
    "arrival_at": "2026-09-13T09:00:00Z",
    "cars": [
        {"code": "C-A1-01", "kind": "BOX", "destination": "N4", "loaded": True,
         "length_m": 18, "danger_class": "NONE"},
        {"code": "C-A2-01", "kind": "HOPPER", "destination": "S2", "loaded": True,
         "length_m": 20, "danger_class": "NONE"},
    ],
}
INTAKE_B = {
    "code": "INT-B",
    "route": "RAIL-B",
    "arrival_at": "2026-09-13T10:00:00Z",
    "cars": [
        {"code": "C-B1-01", "kind": "BOX", "destination": "N4", "loaded": True,
         "length_m": 18, "danger_class": "NONE"},
    ],
}


def _journal_lines(data_dir: Path) -> list[dict]:
    path = data_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _state(data_dir: Path) -> dict:
    return json.loads((data_dir / "yard-state.json").read_text(encoding="utf-8"))


def _wait_dead(server: RunningServer, timeout: float = 8.0) -> int:
    assert server.process is not None
    code = server.process.wait(timeout=timeout)
    if server.process.stdout:
        server._tail = server.process.stdout.read()
    return code


def _crash_on_command(data_dir: Path, hook: str, path: str, payload: dict) -> int:
    """Start a fault-injected server, make one command, expect a hard exit."""
    server = RunningServer(data_dir=data_dir, extra_env={"SWITCHYARD_CRASH_AFTER": hook})
    try:
        server.wait_ready()
        try:
            server.api.post(path, payload)
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        return _wait_dead(server)
    finally:
        server.stop()


# --------------------------------------------------------------------------
def scenario_crash_after_state(root: Path) -> None:
    data_dir = root / "crash-after-state"
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        server.api.expect_ok("POST", "/api/shifts", SHIFT)
        server.api.expect_ok("POST", "/api/intake-trains", INTAKE_A)
    finally:
        server.stop()

    code = _crash_on_command(data_dir, "after_state", "/api/intake-trains/INT-A/classify", {})
    assert code == 91, f"expected fault exit 91, got {code}"

    markers = [line for line in _journal_lines(data_dir) if line.get("record") == "commit"]
    state = _state(data_dir)
    assert len(state["commits"]) == len(markers) + 1, "state must be exactly one commit ahead"

    restarted = RunningServer(data_dir=data_dir)
    try:
        restarted.wait_ready()
        recovery = restarted.api.get("/api/recovery")[1]["data"]["recovery"]
        assert recovery["action"] == "rollforward", recovery
        assert len(recovery["rolled_forward_commits"]) == 1, recovery
        yard = restarted.api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["standing"] == 2, "classified result survived"
        shift = restarted.api.expect_ok("GET", "/api/shifts/SHIFT-RC")
        kinds = [event["kind"] for event in shift["events"]]
        assert kinds == ["SHIFT_OPENED", "TRAIN_RECEIVED", "TRAIN_CLASSIFIED"], kinds
        markers = [line for line in _journal_lines(data_dir) if line.get("record") == "commit"]
        assert len(markers) == len(state["commits"])
        # Replaying the command after recovery is a clean domain conflict,
        # proving the commit was not re-applied.
        assert restarted.api.expect_error("POST", "/api/intake-trains", INTAKE_A)["code"] == "CONFLICT"
    finally:
        restarted.stop()
    print("  OK crash after state -> rolled forward, no re-application")


def scenario_torn_batch(root: Path) -> None:
    data_dir = root / "torn-batch"
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        server.api.expect_ok("POST", "/api/shifts", SHIFT)
    finally:
        server.stop()

    code = _crash_on_command(data_dir, "after_event_lines", "/api/intake-trains", INTAKE_A)
    assert code == 91, code
    lines = _journal_lines(data_dir)
    assert lines[-1].get("record") == "event", "event lines landed without marker"
    assert [line.get("record") for line in lines].count("commit") == 1, "only the old marker exists"

    restarted = RunningServer(data_dir=data_dir)
    try:
        restarted.wait_ready()
        recovery = restarted.api.get("/api/recovery")[1]["data"]["recovery"]
        assert recovery["action"] == "repaired", recovery
        assert recovery["completed_partial_commits"] == 1, recovery
        lines = _journal_lines(data_dir)
        assert lines[-1]["record"] == "commit", "torn batch completed with a marker"
        err = restarted.api.expect_error("POST", "/api/intake-trains", INTAKE_A)
        assert err["code"] == "CONFLICT", err
    finally:
        restarted.stop()
    print("  OK torn journal batch -> completed, command not duplicated")


def scenario_duplicate_writes(root: Path) -> None:
    data_dir = root / "duplicates"
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        server.api.expect_ok("POST", "/api/shifts", SHIFT)
        server.api.expect_ok("POST", "/api/intake-trains", INTAKE_A)
        server.api.expect_ok("POST", "/api/intake-trains/INT-A/classify", {})
    finally:
        server.stop()

    journal = data_dir / "events.jsonl"
    text = journal.read_text(encoding="utf-8")
    journal.write_text(text + text, encoding="utf-8")  # every line doubled

    restarted = RunningServer(data_dir=data_dir)
    try:
        restarted.wait_ready()
        recovery = restarted.api.get("/api/recovery")[1]["data"]["recovery"]
        assert recovery["action"] == "repaired", recovery
        assert recovery["removed_duplicate_events"] == 3, recovery
        assert recovery["removed_duplicate_commits"] == 3, recovery
        lines = _journal_lines(data_dir)
        commit_ids = [line["commit_id"] for line in lines if line.get("record") == "commit"]
        assert len(commit_ids) == len(set(commit_ids)) == 3, commit_ids
        yard = restarted.api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["standing"] == 2
        # A subsequent commit appends after the repaired boundary cleanly.
        restarted.api.expect_ok("POST", "/api/intake-trains", INTAKE_B)
        assert len([line for line in _journal_lines(data_dir) if line.get("record") == "commit"]) == 4
    finally:
        restarted.stop()
    print("  OK duplicate event/marker writes -> deduped, history intact")


def scenario_continuous_commands(root: Path) -> None:
    data_dir = root / "continuous"
    steps = [
        ("POST", "/api/shifts", SHIFT),
        ("POST", "/api/intake-trains", INTAKE_A),
        ("POST", "/api/intake-trains/INT-A/classify", {}),
        ("POST", "/api/intake-trains", INTAKE_B),
        ("POST", "/api/intake-trains/INT-B/classify", {}),
    ]
    server: RunningServer | None = None
    try:
        for index, (method, path, payload) in enumerate(steps):
            server = RunningServer(data_dir=data_dir)
            server.wait_ready()
            server.api.expect_ok(method, path, payload)
            server.stop()
            server = None
        server = RunningServer(data_dir=data_dir)
        server.wait_ready()
        yard = server.api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["standing"] == 3, yard
        state = _state(data_dir)
        markers = [line for line in _journal_lines(data_dir) if line.get("record") == "commit"]
        assert len(markers) == len(state["commits"]) == 5, (len(markers), len(state["commits"]))
        sequences = [event["sequence"] for event in state["events"]]
        assert sequences == list(range(1, 6)), sequences
        journaled = sorted(line["event"]["sequence"]
                           for line in _journal_lines(data_dir) if line.get("record") == "event")
        assert journaled == sequences
        assert state["next_event_sequence"] == 6
        closed = server.api.expect_ok("POST", "/api/shifts/SHIFT-RC/close", {})
        assert closed["shift"]["state"] == "CLOSED"
        state = _state(data_dir)
        assert [c["commit_number"] for c in state["commits"]] == list(range(1, 7))
    finally:
        if server is not None:
            server.stop()
    print("  OK continuous commands across restarts -> contiguous, complete history")


def scenario_unrecoverable(root: Path) -> None:
    # Shape 1: journaled content tampered after commit -> hash mismatch.
    data_dir = root / "divergence-hash"
    server = RunningServer(data_dir=data_dir)
    try:
        server.wait_ready()
        server.api.expect_ok("POST", "/api/shifts", SHIFT)
        server.api.expect_ok("POST", "/api/intake-trains", INTAKE_A)
    finally:
        server.stop()

    journal = data_dir / "events.jsonl"
    lines = journal.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])  # first framed event line
    assert record.get("record") == "event"
    record["event"]["message"] = "TAMPERED"
    lines[0] = json.dumps(record, sort_keys=True)
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")

    broken = RunningServer(data_dir=data_dir)
    try:
        code = _wait_dead(broken)
        assert code == 2, f"divergent store must refuse startup, got {code}"
        assert "inconsistency" in broken._tail and "hash mismatch" in broken._tail, broken._tail
    finally:
        broken.stop()

    # Shape 2: state ahead of the journal by two commits -> cannot guess.
    data_dir2 = root / "divergence-gap"
    good = RunningServer(data_dir=data_dir2)
    good.wait_ready()
    good.api.expect_ok("POST", "/api/shifts", SHIFT)
    good.stop()

    from switchyard.storage.repository import YardRepository

    repo = YardRepository(data_dir2)
    workspace = repo.load()
    base = dict(workspace.commits[0])
    event_dict = workspace.events[0].to_dict()
    for number, commit_id in ((2, "C-FAKE-2"), (3, "C-FAKE-3")):
        workspace.commits.append({
            "commit_id": commit_id,
            "commit_number": number,
            "state_version": int(base["state_version"]) + number,
            "event_sequences": [1],
            "event_hashes": [event_content_hash(event_dict)],
        })
    repo.rewrite_state(workspace)  # journal untouched: one marker vs three commits

    broken2 = RunningServer(data_dir=data_dir2)
    try:
        code = _wait_dead(broken2)
        assert code == 2, f"gap must refuse startup, got {code}"
        assert "more than one commit" in broken2._tail, broken2._tail
    finally:
        broken2.stop()

    # The healthy directory still starts; bad state is never treated as history.
    healthy = RunningServer(data_dir=root / "continuous")
    healthy.wait_ready()
    healthy.stop()
    print("  OK unrecoverable divergence -> service refuses to serve it")


def scenario_legacy_files(root: Path) -> None:
    seed_dir = root / "legacy-seed"
    seeder = RunningServer(data_dir=seed_dir)
    seeder.wait_ready()
    seeder.api.expect_ok("POST", "/api/shifts", {"code": "SHIFT-OLD", "dispatcher": "KE",
                                                 "opened_at": "2026-09-12T08:00:00Z"})
    seeder.stop()

    data_dir = root / "legacy"
    data_dir.mkdir(parents=True, exist_ok=True)
    state = _state(seed_dir)
    state.pop("commits", None)  # pre-framing state: no commit ledger
    (data_dir / "yard-state.json").write_text(json.dumps(state, indent=2, sort_keys=True))
    (data_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in state["events"]) + "\n",
        encoding="utf-8",
    )

    restarted = RunningServer(data_dir=data_dir)
    try:
        restarted.wait_ready()
        recovery = restarted.api.get("/api/recovery")[1]["data"]["recovery"]
        assert recovery["action"] == "migrated", recovery
        shift = restarted.api.expect_ok("GET", "/api/shifts/SHIFT-OLD")
        assert shift["shift"]["state"] == "OPEN"
        assert [event["kind"] for event in shift["events"]] == ["SHIFT_OPENED"]
    finally:
        restarted.stop()

    restarted = RunningServer(data_dir=data_dir)  # migration is one-time
    try:
        restarted.wait_ready()
        assert restarted.api.get("/api/recovery")[1]["data"]["recovery"]["action"] == "none"
        restarted.api.expect_ok("POST", "/api/intake-trains", {
            "code": "INT-L", "route": "RAIL-L", "arrival_at": "2026-09-12T09:00:00Z",
            "cars": [{"code": "C-L1-01", "kind": "BOX", "destination": "N4", "loaded": True,
                      "length_m": 18, "danger_class": "NONE"}],
        })
        state = _state(data_dir)
        assert [c["commit_number"] for c in state["commits"]] == [1, 2]
    finally:
        restarted.stop()
    print("  OK legacy state/journal -> auto-migrated, readable, new commits append cleanly")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="switchyard-persistence-") as tmp:
        root = Path(tmp)
        scenario_crash_after_state(root)
        scenario_torn_batch(root)
        scenario_duplicate_writes(root)
        scenario_continuous_commands(root)
        scenario_unrecoverable(root)
        scenario_legacy_files(root)
    print("OK wf_persistence_recovery")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
