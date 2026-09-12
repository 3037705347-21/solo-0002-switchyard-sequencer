"""Workflow check: dispatch recomputation when the queue changes the stacks.

Reverse target order: the first ticket pulls the TOP car while a later ticket
targets the BOTTOM car of the same track. At registration the later plan
buffers cars that the first plan later pulls away, so its stored steps and
declared resources (including the X1 bay) go stale. The check verifies that
the later ticket is recomputed from the current track at claim (and at run
start), that the claim token survives this recomputation and a service
restart, and that cancellation still releases the refreshed dependencies.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from support import ApiClient, RunningServer, free_port

CARS = [
    # N4-A stack bottom->top after classify: 80, 81, 82, 83
    ("C-N4-80", "BOX"),
    ("C-N4-81", "BOX"),
    ("C-N4-82", "BOX"),
    ("C-N4-83", "BOX"),
    # S2-A top car used by a conflict-free side plan.
    ("C-S2-80", "HOPPER"),
]


def seed(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-05", "dispatcher": "QIN", "opened_at": "2026-09-12T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-51",
            "route": "RAIL-51",
            "arrival_at": "2026-09-12T08:30:00Z",
            "cars": [
                {
                    "code": code,
                    "kind": kind,
                    "destination": "N4" if code.startswith("C-N4") else "S2",
                    "loaded": True,
                    "length_m": 18,
                    "danger_class": "NONE",
                }
                for code, kind in CARS
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-51/classify", {})
    # REVERSE order relative to the stack: first plan takes the top car,
    # second plan takes the bottom car of the same N4-A track.
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-81", "destination": "N4", "car_codes": ["C-N4-83"]},
    )
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-82", "destination": "N4", "car_codes": ["C-N4-80"]},
    )
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": "OB-83", "destination": "S2", "car_codes": ["C-S2-80"]},
    )


def ticket_by_outbound(board: dict, outbound: str) -> dict:
    return next(item for item in board["tickets"] if item["outbound_code"] == outbound)


def run() -> None:
    workdir = tempfile.TemporaryDirectory(prefix="switchyard-reverse-")
    data_dir = Path(workdir.name) / "data"
    server = RunningServer(data_dir=data_dir, port=free_port())
    try:
        server.wait_ready()
        api = server.api
        seed(api)

        t1 = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-81/dispatch",
            {"transfer_code": "X1", "client_id": "crew-top"},
        )["ticket"]
        t2 = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-82/dispatch",
            {"transfer_code": "X1", "client_id": "crew-bottom"},
        )["ticket"]
        t3 = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-83/dispatch",
            {"transfer_code": "X1", "client_id": "crew-side"},
        )["ticket"]
        assert t1["queue_order"] + 2 == t3["queue_order"]

        # At registration the bottom plan buffers 81, 82 and the top car 83,
        # so it declares X1 and the blocker car resources.
        assert {"car:C-N4-81", "car:C-N4-82", "car:C-N4-83"}.issubset(set(t2["resources"]))
        assert "bay:X1" in t2["resources"]

        # The top plan and the independent S2 plan are eligible; T2 waits.
        board = api.expect_ok("GET", "/api/dispatch")
        assert set(board["eligible"]) == {t1["code"], t3["code"]}
        assert board["waiting"] == [t2["code"]]

        # First ticket (top car) executes immediately: one PULL, no buffering.
        token1 = api.expect_ok(
            "POST",
            f"/api/dispatch/{t1['code']}/claim",
            {"client_id": "crew-top"},
        )["claim_token"]
        done1 = api.expect_ok(
            "POST",
            f"/api/pull-runs/{t1['run_code']}/advance",
            {"steps": 5, "claim_token": token1},
        )
        assert done1["completed"] is True
        assert len(done1["pull_run"]["steps"]) == 1
        assert done1["outbound"]["assembled_car_codes"] == ["C-N4-83"]

        # The board now previews the refreshed plan for still-queued T2 and
        # flags the stored steps as stale; 83 must not appear any more.
        board = api.expect_ok("GET", "/api/dispatch")
        t2_preview = ticket_by_outbound(board, "OB-82")
        assert t2_preview["plan_stale"] is True
        preview_cars = {
            action["resource"]
            for action in t2_preview["release_actions"]
            if action["resource"].startswith("car:")
        }
        assert "car:C-N4-83" not in preview_cars

        # T3 claims while T2 is queued: independent track, no overlap.
        token3 = api.expect_ok(
            "POST",
            f"/api/dispatch/{t3['code']}/claim",
            {"client_id": "crew-side"},
        )["claim_token"]
        done3 = api.expect_ok(
            "POST",
            f"/api/pull-runs/{t3['run_code']}/advance",
            {"steps": 1, "claim_token": token3},
        )
        assert done3["completed"] is True

        # T2 claims: arbitration releases it and the run is recomputed against
        # the current stack [80, 81, 82]. The old blocker 83 is gone.
        claim2 = api.expect_ok(
            "POST",
            f"/api/dispatch/{t2['code']}/claim",
            {"client_id": "crew-bottom"},
        )
        assert claim2["replanned"] is True
        token2 = claim2["claim_token"]
        steps = claim2["pull_run"]["steps"]
        buffered = [s["car_code"] for s in steps if s["verb"] == "BUFFER"]
        pulled = [s["car_code"] for s in steps if s["verb"] == "PULL"]
        assert set(buffered) == {"C-N4-81", "C-N4-82"}, buffered
        assert "C-N4-83" not in buffered
        assert pulled == ["C-N4-80"]
        refreshed = claim2["ticket"]
        assert "car:C-N4-83" not in refreshed["resources"]
        assert {"car:C-N4-81", "car:C-N4-82", "track:N4-A", "bay:X1"}.issubset(
            set(refreshed["resources"])
        )
        # Ticket code, run code and owner stay stable across recomputation.
        assert refreshed["code"] == t2["code"]
        assert claim2["pull_run"]["code"] == t2["run_code"]
        assert refreshed["claimed_by"] == "crew-bottom"

        # Ownership is exclusive even with the refreshed plan.
        stolen = api.expect_error(
            "POST",
            f"/api/dispatch/{t2['code']}/claim",
            {"client_id": "crew-intruder"},
        )
        assert stolen["code"] == "RESOURCE_BUSY"
        # The same client replaying the claim with its token stays idempotent.
        replay = api.expect_ok(
            "POST",
            f"/api/dispatch/{t2['code']}/claim",
            {"client_id": "crew-bottom", "claim_token": token2},
        )
        assert replay["idempotent"] is True and replay["claim_token"] == token2

        # Restart the service: the claimed execution right and refreshed steps
        # persist; the original token still drives the run.
        server.restart()
        board = api.expect_ok("GET", "/api/dispatch")
        after = ticket_by_outbound(board, "OB-82")
        assert after["state"] == "CLAIMED"
        assert after["claimed_by"] == "crew-bottom"
        assert "car:C-N4-83" not in after["resources"]
        other = api.expect_error(
            "POST",
            f"/api/dispatch/{t2['code']}/claim",
            {"client_id": "crew-intruder"},
        )
        assert other["code"] == "RESOURCE_BUSY"

        run2 = api.expect_ok(
            "POST",
            f"/api/pull-runs/{t2['run_code']}/advance",
            {"steps": 10, "claim_token": token2},
        )
        assert run2["completed"] is True
        assert run2["outbound"]["assembled_car_codes"] == ["C-N4-80"]
        assert run2["ticket"]["state"] == "COMPLETED"

        # Blockers returned, X1 empty; the two pulled cars are assembled.
        yard = api.expect_ok("GET", "/api/yard")
        bays = {item["code"]: item for item in yard["metrics"]["transfer_bays"]}
        tracks = {item["code"]: item for item in yard["metrics"]["track_metrics"]}
        assert bays["X1"]["cars"] == 0
        assert tracks["N4-A"]["cars"] == 2
        assert tracks["N4-A"]["top_car"] == "C-N4-82"
        assert yard["metrics"]["car_state_counts"]["assembled"] == 3

        # A freshly queued reverse plan that is cancelled before start must
        # release its (refreshed) dependencies and be re-registerable.
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-84", "destination": "N4", "car_codes": ["C-N4-81"]},
        )
        t4 = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-84/dispatch",
            {"transfer_code": "X1", "client_id": "crew-84"},
        )["ticket"]
        # Stack is now [81, 82]: pulling bottom 81 buffers 82, so it claims X1.
        token4 = api.expect_ok(
            "POST",
            f"/api/dispatch/{t4['code']}/claim",
            {"client_id": "crew-84"},
        )["claim_token"]
        cancelled = api.expect_ok(
            "POST",
            f"/api/dispatch/{t4['code']}/cancel",
            {"client_id": "crew-84", "claim_token": token4},
        )
        assert cancelled["ticket"]["state"] == "CANCELLED"
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["reserved"] == 0
        requeue = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-84/dispatch",
            {"transfer_code": "X1", "client_id": "crew-84b"},
        )
        assert requeue["ticket"]["queue_order"] == 5

        # Long plan already holding X1: the re-registered OB-84 ticket targets
        # bottom C-N4-81 (buffers C-N4-82), so claim it and take one buffer
        # step, leaving it RUNNING and holding X1.
        board = api.expect_ok("GET", "/api/dispatch")
        t5 = next(item for item in board["tickets"] if item["outbound_code"] == "OB-84")
        assert "bay:X1" in t5["resources"]
        api.expect_ok(
            "POST",
            "/api/intake-trains",
            {
                "code": "INT-52",
                "route": "RAIL-52",
                "arrival_at": "2026-09-12T10:30:00Z",
                "cars": [
                    {
                        "code": "C-S2-81",
                        "kind": "HOPPER",
                        "destination": "S2",
                        "loaded": True,
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-S2-82",
                        "kind": "HOPPER",
                        "destination": "S2",
                        "loaded": True,
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                ],
            },
        )
        api.expect_ok("POST", "/api/intake-trains/INT-52/classify", {})
        # S2 bottom 81 (buffers top 82 via X1 at registration time).
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-86", "destination": "S2", "car_codes": ["C-S2-81"]},
        )
        # S2 top pull that later removes the blocker.
        api.expect_ok(
            "POST",
            "/api/outbound-trains",
            {"code": "OB-87", "destination": "S2", "car_codes": ["C-S2-82"]},
        )
        t6 = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-86/dispatch",
            {"transfer_code": "X1", "client_id": "crew-86"},
        )["ticket"]
        t7 = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-87/dispatch",
            {"transfer_code": "X1", "client_id": "crew-87"},
        )["ticket"]
        assert "bay:X1" in t6["resources"]
        token5 = api.expect_ok(
            "POST",
            f"/api/dispatch/{t5['code']}/claim",
            {"client_id": "crew-84b"},
        )["claim_token"]
        api.expect_ok(
            "POST",
            f"/api/pull-runs/{t5['run_code']}/advance",
            {"steps": 1, "claim_token": token5},
        )
        # T6 is blocked by the X1 holder and sits ahead of T7 on the S2 track.
        # Cancel T6, let T7 pull the S2 top blocker 82, then re-register T6: it
        # is now a straight top pull with no X1 even though T5 still holds X1.
        api.expect_ok(
            "POST",
            f"/api/dispatch/{t6['code']}/cancel",
            {"client_id": "dispatcher-qin"},
        )
        token7 = api.expect_ok(
            "POST",
            f"/api/dispatch/{t7['code']}/claim",
            {"client_id": "crew-87"},
        )["claim_token"]
        api.expect_ok(
            "POST",
            f"/api/pull-runs/{t7['run_code']}/advance",
            {"steps": 1, "claim_token": token7},
        )
        t6b = api.expect_ok(
            "POST",
            "/api/outbound-trains/OB-86/dispatch",
            {"transfer_code": "X1", "client_id": "crew-86"},
        )["ticket"]
        assert "bay:X1" not in t6b["resources"], t6b["resources"]
        board = api.expect_ok("GET", "/api/dispatch")
        v6 = next(item for item in board["tickets"] if item["code"] == t6b["code"])
        assert v6["eligible"] is True, v6["blocked_by"]
        claim6 = api.expect_ok(
            "POST",
            f"/api/dispatch/{t6b['code']}/claim",
            {"client_id": "crew-86"},
        )
        assert len(claim6["pull_run"]["steps"]) == 1
        done6 = api.expect_ok(
            "POST",
            f"/api/pull-runs/{claim6['pull_run']['code']}/advance",
            {"steps": 1, "claim_token": claim6["claim_token"]},
        )
        assert done6["completed"] is True
        # Let T5 finish too so the yard is left consistent.
        api.expect_ok(
            "POST",
            f"/api/pull-runs/{t5['run_code']}/advance",
            {"steps": 5, "claim_token": token5},
        )
    finally:
        server.stop()
        workdir.cleanup()


if __name__ == "__main__":
    run()
    print("OK wf_pull_reverse_order")
    raise SystemExit(0)
