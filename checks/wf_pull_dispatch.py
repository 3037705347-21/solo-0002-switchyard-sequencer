"""Workflow check: pull dispatch board arbitration, claims, restart, cancel.

Four plans are registered: two conflicting deep pulls on the same source track
and transfer bay X1, and two conflict-free top-of-stack pulls on independent
tracks. The check verifies queue order and blocking reasons, single-owner
claim tokens (including duplicate claims and restart durability), execution
under a token, and cancellation that releases declared dependencies.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from support import ApiClient, RunningServer, free_port


CARS = [
    # N4-A stack bottom->top after classify: 70, 71, 72, 73
    ("C-N4-70", "N4", "BOX"),
    ("C-N4-71", "N4", "BOX"),
    ("C-N4-72", "N4", "BOX"),
    ("C-N4-73", "N4", "BOX"),
    # S2-A top-pull
    ("C-S2-70", "S2", "HOPPER"),
    # W9-A top-pull
    ("C-W9-70", "W9", "FLAT"),
]


def seed(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-04", "dispatcher": "LIN", "opened_at": "2026-09-12T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-41",
            "route": "RAIL-41",
            "arrival_at": "2026-09-12T08:30:00Z",
            "cars": [
                {
                    "code": code,
                    "kind": kind,
                    "destination": dest,
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
                for code, dest, kind in CARS
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-41/classify", {})
    yard = api.expect_ok("GET", "/api/yard")
    tracks = {item["code"]: item for item in yard["metrics"]["track_metrics"]}
    assert tracks["N4-A"]["top_car"] == "C-N4-73"
    assert tracks["S2-A"]["top_car"] == "C-S2-70"
    assert tracks["W9-A"]["top_car"] == "C-W9-70"
    # Two conflicting deep pulls (same source track N4-A, both buffer via X1).
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-61", "destination": "N4", "car_codes": ["C-N4-70"]},
    )
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-62", "destination": "N4", "car_codes": ["C-N4-71"]},
    )
    # Two independent top-of-stack pulls with no buffer steps.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-63", "destination": "S2", "car_codes": ["C-S2-70"]},
    )
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-64", "destination": "W9", "car_codes": ["C-W9-70"]},
    )


def register(api: ApiClient) -> dict[str, str]:
    codes: dict[str, str] = {}
    for outbound, expected_order in [
        ("OB-61", 1),
        ("OB-62", 2),
        ("OB-63", 3),
        ("OB-64", 4),
    ]:
        data = api.expect_ok(
            "POST",
            f"/api/outbound-trains/{outbound}/dispatch",
            {"transfer_code": "X1", "client_id": f"client-{outbound}"},
        )
        ticket = data["ticket"]
        assert ticket["queue_order"] == expected_order
        assert ticket["state"] == "QUEUED"
        codes[outbound] = ticket["code"]
    return codes


def test_queue_order_and_blockers(api: ApiClient, codes: dict[str, str]) -> None:
    board = api.expect_ok("GET", "/api/dispatch")
    assert board["queue_order"] == [codes[o] for o in ("OB-61", "OB-62", "OB-63", "OB-64")]
    by_code = {item["code"]: item for item in board["tickets"]}
    t1, t2, t3, t4 = (by_code[codes[o]] for o in ("OB-61", "OB-62", "OB-63", "OB-64"))

    # First ticket and the two independent top-pulls are eligible immediately.
    assert board["eligible"] == [codes[o] for o in ("OB-61", "OB-63", "OB-64")]
    assert board["waiting"] == [codes["OB-62"]]

    # T1 declares the shared source track and X1 transfer capacity.
    assert "track:N4-A" in t1["resources"]
    assert "bay:X1" in t1["resources"]
    assert {"car:C-N4-71", "car:C-N4-72", "car:C-N4-73"}.issubset(set(t1["resources"]))
    # Top-pull tickets do not occupy X1 and only hold their own source track.
    assert "bay:X1" not in t3["resources"] and "bay:X1" not in t4["resources"]

    # T2 is blocked solely by the earlier ticket T1, with conflict details.
    assert t2["eligible"] is False
    blocked_by = t2["blocked_by"]
    assert len(blocked_by) == 1
    assert blocked_by[0]["ticket_code"] == codes["OB-61"]
    assert blocked_by[0]["reason"] == "ahead-in-queue"
    assert "track:N4-A" in blocked_by[0]["resources"]
    assert "bay:X1" in blocked_by[0]["resources"]

    # The board shows expected actions that release resources, with step index.
    release = {item["resource"]: item for item in t1["release_actions"]}
    assert "bay:X1" in release and "track:N4-A" in release and "car:C-N4-71" in release
    assert release["car:C-N4-71"]["step_index"] < release["bay:X1"]["step_index"]
    assert "source track N4-A" in release["track:N4-A"]["action"]
    assert release["track:N4-A"]["step_index"] == release["bay:X1"]["step_index"]

    # Resource board names one holder and a waiting ticket for X1.
    x1 = next(item for item in board["resources"] if item["resource"] == "bay:X1")
    assert x1["held_by"] == codes["OB-61"]
    assert codes["OB-62"] in x1["waiting_tickets"]


def test_claim_exclusion(api: ApiClient, codes: dict[str, str]) -> dict[str, str]:
    # T1 claimed by client A, tokens persisted and returned only to the owner.
    claim_a = api.expect_ok(
        "POST",
        f"/api/dispatch/{codes['OB-61']}/claim",
        {"client_id": "client-A"},
    )
    token_a = claim_a["claim_token"]
    assert len(token_a) >= 32 and claim_a["idempotent"] is False

    # Another client cannot claim the same ticket ("same ticket, two clients").
    other = api.expect_error(
        "POST",
        f"/api/dispatch/{codes['OB-61']}/claim",
        {"client_id": "client-B"},
    )
    assert other["code"] == "RESOURCE_BUSY"

    # Re-claim with the same client but no token is rejected.
    again = api.expect_error(
        "POST",
        f"/api/dispatch/{codes['OB-61']}/claim",
        {"client_id": "client-A"},
    )
    assert again["code"] == "RESOURCE_BUSY"

    # Re-claim with the same client and its token is idempotent (retry safe).
    retry = api.expect_ok(
        "POST",
        f"/api/dispatch/{codes['OB-61']}/claim",
        {"client_id": "client-A", "claim_token": token_a},
    )
    assert retry["idempotent"] is True and retry["claim_token"] == token_a

    # A wrong token from another client cannot steal the execution right.
    stolen = api.expect_error(
        "POST",
        f"/api/dispatch/{codes['OB-61']}/claim",
        {"client_id": "client-B", "claim_token": token_a},
    )
    assert stolen["code"] == "RESOURCE_BUSY"

    # The blocked ticket T2 still cannot be claimed by anyone.
    blocked = api.expect_error(
        "POST",
        f"/api/dispatch/{codes['OB-62']}/claim",
        {"client_id": "client-A"},
    )
    assert blocked["code"] == "RESOURCE_BUSY"
    assert blocked["details"]["blockers"][0]["ticket_code"] == codes["OB-61"]

    # Non-conflicting tickets can both be held concurrently.
    token_63 = api.expect_ok(
        "POST",
        f"/api/dispatch/{codes['OB-63']}/claim",
        {"client_id": "client-63"},
    )["claim_token"]
    token_64 = api.expect_ok(
        "POST",
        f"/api/dispatch/{codes['OB-64']}/claim",
        {"client_id": "client-64"},
    )["claim_token"]

    # Advance without the token is refused; with the token it starts.
    run_63 = f"RUN-OB-63-3"
    no_token = api.expect_error("POST", f"/api/pull-runs/{run_63}/advance", {"steps": 1})
    assert no_token["code"] == "RESOURCE_BUSY"
    bad_token = api.expect_error(
        "POST",
        f"/api/pull-runs/{run_63}/advance",
        {"steps": 1, "claim_token": "deadbeef" * 8},
    )
    assert bad_token["code"] == "RESOURCE_BUSY"
    advanced = api.expect_ok(
        "POST",
        f"/api/pull-runs/{run_63}/advance",
        {"steps": 1, "claim_token": token_63},
    )
    assert advanced["completed"] is True
    assert advanced["ticket"]["state"] == "COMPLETED"

    # T4 finishes too; neither touched X1.
    api.expect_ok(
        "POST",
        "/api/pull-runs/RUN-OB-64-4/advance",
        {"steps": 1, "claim_token": token_64},
    )
    return {"OB-61": token_a}


def test_restart_recovery(server: RunningServer, api: ApiClient, codes: dict[str, str], tokens: dict[str, str]) -> None:
    board_before = api.expect_ok("GET", "/api/dispatch")
    before = {item["code"]: item["queue_order"] for item in board_before["tickets"]}
    server.restart()

    board = api.expect_ok("GET", "/api/dispatch")
    after = {item["code"]: item["queue_order"] for item in board["tickets"]}
    assert after == before  # queue and order survived the restart

    by_code = {item["code"]: item for item in board["tickets"]}
    t1 = by_code[codes["OB-61"]]
    assert t1["state"] == "CLAIMED"
    assert t1["claimed_by"] == "client-A"

    # The execution right survived: another client still cannot claim or execute.
    other = api.expect_error(
        "POST",
        f"/api/dispatch/{codes['OB-61']}/claim",
        {"client_id": "client-B"},
    )
    assert other["code"] == "RESOURCE_BUSY"
    blocked_advance = api.expect_error(
        "POST",
        "/api/pull-runs/RUN-OB-61-1/advance",
        {"steps": 1, "claim_token": "0" * 32},
    )
    assert blocked_advance["code"] == "RESOURCE_BUSY"

    # The original token still works after restart and completes the pull.
    total = 7  # BUFFER 71,72,73 -> PULL 70 -> RETURN 73,72,71
    advanced = api.expect_ok(
        "POST",
        "/api/pull-runs/RUN-OB-61-1/advance",
        {"steps": total, "claim_token": tokens["OB-61"]},
    )
    assert advanced["completed"] is True
    assert advanced["outbound"]["state"] == "READY"
    assert advanced["outbound"]["assembled_car_codes"] == ["C-N4-70"]

    # X1 is empty again and the blocker cars returned to N4-A.
    yard = api.expect_ok("GET", "/api/yard")
    bays = {item["code"]: item for item in yard["metrics"]["transfer_bays"]}
    tracks = {item["code"]: item for item in yard["metrics"]["track_metrics"]}
    assert bays["X1"]["cars"] == 0
    # All three blocker cars (73 top, then 72, 71) were returned; car 70 left.
    assert tracks["N4-A"]["cars"] == 3 and tracks["N4-A"]["top_car"] == "C-N4-73"

    # T2 becomes eligible once T1 released its resources.
    board_after = api.expect_ok("GET", "/api/dispatch")
    t2 = next(item for item in board_after["tickets"] if item["code"] == codes["OB-62"])
    assert t2["eligible"] is True and t2["blocked_by"] == []


def test_cancel_releases(api: ApiClient, codes: dict[str, str]) -> None:
    # A fresh plan targeting the new top car 73: it queues behind still-queued
    # T2 in FIFO order even though T2 itself is now technically derivable.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-65", "destination": "N4", "car_codes": ["C-N4-73"]},
    )
    t65 = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-65/dispatch",
        {"transfer_code": "X1", "client_id": "client-65"},
    )["ticket"]
    assert t65["queue_order"] == 5
    blocked = api.expect_error(
        "POST",
        f"/api/dispatch/{t65['code']}/claim",
        {"client_id": "client-65"},
    )
    assert blocked["details"]["blockers"][0]["ticket_code"] == codes["OB-62"]

    # An unclaimed queued ticket can be cancelled by a dispatcher action.
    cancelled = api.expect_ok(
        "POST",
        f"/api/dispatch/{codes['OB-62']}/cancel",
        {"client_id": "dispatcher-lin"},
    )
    assert cancelled["ticket"]["state"] == "CANCELLED"

    # Released dependencies: outbound back to DRAFT and target car 71 standing.
    board = api.expect_ok("GET", "/api/dispatch")
    assert codes["OB-62"] not in board["queue_order"]

    # T65 is now first in line for N4-A / X1 and can claim and execute.
    t65_view = next(item for item in board["tickets"] if item["code"] == t65["code"])
    assert t65_view["eligible"] is True
    token65 = api.expect_ok(
        "POST",
        f"/api/dispatch/{t65['code']}/claim",
        {"client_id": "client-65"},
    )["claim_token"]
    done = api.expect_ok(
        "POST",
        "/api/pull-runs/RUN-OB-65-5/advance",
        {"steps": 1, "claim_token": token65},
    )
    assert done["completed"] is True

    # After execution nothing stays reserved: 71/72 standing, targets assembled.
    yard = api.expect_ok("GET", "/api/yard")
    counts = yard["metrics"]["car_state_counts"]
    assert counts["reserved"] == 0
    assert counts["standing"] == 2 and counts["assembled"] == 4

    # A cancelled ticket can never be advanced.
    dead = api.expect_error(
        "POST",
        "/api/pull-runs/RUN-OB-62-2/advance",
        {"steps": 1, "claim_token": "0" * 32},
    )
    assert dead["code"] == "CONFLICT"

    # A cancelled outbound can be re-planned through the board.
    requeue = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-62/dispatch",
        {"transfer_code": "X1", "client_id": "client-62b"},
    )
    assert requeue["ticket"]["queue_order"] == 6
    # The requeued ticket queues ahead of later N4-A plans; cancel it again so
    # the next test can claim its own ticket.
    api.expect_ok(
        "POST",
        f"/api/dispatch/{requeue['ticket']['code']}/cancel",
        {"client_id": "dispatcher-lin"},
    )


def test_running_ticket_cannot_cancel(api: ApiClient) -> None:
    # OB-62 is back at DRAFT targeting bottom car 71 (stack is [71, 72] after
    # OB-65 pulled top car 73); its plan buffers 72, giving a mid-run window.
    api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-62/dispatch",
        {"transfer_code": "X1", "client_id": "client-66"},
    )
    board = api.expect_ok("GET", "/api/dispatch")
    own = next(item for item in board["tickets"] if item["outbound_code"] == "OB-62")

    # While the ticket is still queued, any client can cancel it (unstarted).
    api.expect_ok(
        "POST",
        f"/api/dispatch/{own['code']}/cancel",
        {"client_id": "dispatcher-lin"},
    )
    # Re-register and claim so the running-state rule can be exercised.
    api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-62/dispatch",
        {"transfer_code": "X1", "client_id": "client-66"},
    )
    board = api.expect_ok("GET", "/api/dispatch")
    own = next(item for item in board["tickets"] if item["outbound_code"] == "OB-62")
    token = api.expect_ok(
        "POST",
        f"/api/dispatch/{own['code']}/claim",
        {"client_id": "client-66"},
    )["claim_token"]

    # A claimed (but unstarted) ticket cannot be cancelled by another client.
    intruder = api.expect_error(
        "POST",
        f"/api/dispatch/{own['code']}/cancel",
        {"client_id": "client-99", "claim_token": "0" * 32},
    )
    assert intruder["code"] == "RESOURCE_BUSY"

    # Buffer one car: work has begun, so even the owner cannot cancel.
    api.expect_ok(
        "POST",
        f"/api/pull-runs/{own['run_code']}/advance",
        {"steps": 1, "claim_token": token},
    )
    refused = api.expect_error(
        "POST",
        f"/api/dispatch/{own['code']}/cancel",
        {"client_id": "client-66", "claim_token": token},
    )
    assert refused["code"] == "STATE_TRANSITION"

    # The owner finishes the run instead, proving the buffer can be recovered.
    api.expect_ok(
        "POST",
        f"/api/pull-runs/{own['run_code']}/advance",
        {"steps": 10, "claim_token": token},
    )
    yard = api.expect_ok("GET", "/api/yard")
    bays = {item["code"]: item for item in yard["metrics"]["transfer_bays"]}
    assert bays["X1"]["cars"] == 0


def run_with_restart() -> None:
    workdir = tempfile.TemporaryDirectory(prefix="switchyard-dispatch-")
    data_dir = Path(workdir.name) / "data"
    server = RunningServer(data_dir=data_dir, port=free_port())
    try:
        server.wait_ready()
        api = server.api
        seed(api)
        codes = register(api)
        test_queue_order_and_blockers(api, codes)
        tokens = test_claim_exclusion(api, codes)
        test_restart_recovery(server, api, codes, tokens)
        test_cancel_releases(api, codes)
        test_running_ticket_cannot_cancel(api)
    finally:
        server.stop()
        workdir.cleanup()


if __name__ == "__main__":
    run_with_restart()
    print("OK wf_pull_dispatch")
    raise SystemExit(0)
