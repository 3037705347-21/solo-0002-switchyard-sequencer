"""Workflow check: frozen departure manifests across the planning lifecycle.

Verifies that a manifest version is published with a stable version number and
content digest, that older versions remain byte-stable as the yard changes,
that preparation for departure separates PENDING differences (plan not yet
executed) from CONFLICT differences (plan can no longer be achieved), and that
a replanned consist produces a corrected manifest version. Every frozen export
is checked against the live yard.
"""

from __future__ import annotations

from support import ApiClient, run_check


def _car(code: str, kind: str) -> dict[str, object]:
    return {
        "code": code,
        "kind": kind,
        "destination": "N4",
        "loaded": False,
        "length_m": 18,
        "danger_class": "NONE",
    }


def setup_yard(api: ApiClient) -> None:
    api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-04", "dispatcher": "LIN", "opened_at": "2026-09-10T08:00:00Z"},
    )
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-41",
            "route": "RAIL-41",
            "arrival_at": "2026-09-10T09:00:00Z",
            "cars": [
                _car("C-N4-41", "BOX"),
                _car("C-BLK-41", "BOX"),
                _car("C-N4-42", "FLAT"),
                _car("C-BLK-42", "TANK"),
                _car("C-BLK-43", "REEFER"),
                _car("C-BLK-44", "BOX"),
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-41/classify", {})
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-42",
            "route": "RAIL-42",
            "arrival_at": "2026-09-10T09:30:00Z",
            "cars": [_car("C-N4-43", "TANK"), _car("C-N4-44", "REEFER")],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-42/classify", {})
    api.expect_ok(
        "POST",
        "/api/intake-trains",
        {
            "code": "INT-43",
            "route": "RAIL-43",
            "arrival_at": "2026-09-10T10:00:00Z",
            "cars": [
                {"code": "C-E7-41", "kind": "BOX", "destination": "E7", "loaded": False, "length_m": 18, "danger_class": "NONE"},
                {"code": "C-E7-42", "kind": "TANK", "destination": "E7", "loaded": False, "length_m": 18, "danger_class": "NONE"},
                {"code": "C-E7-BLK", "kind": "BOX", "destination": "E7", "loaded": False, "length_m": 18, "danger_class": "NONE"},
            ],
        },
    )
    api.expect_ok("POST", "/api/intake-trains/INT-43/classify", {})


def _plan(api: ApiClient, code: str, cars: list[str], destination: str = "N4") -> str:
    api.expect_ok(
        "POST",
        "/api/outbound-trains",
        {"code": code, "destination": destination, "car_codes": cars},
    )
    sequenced = api.expect_ok(
        "POST",
        f"/api/outbound-trains/{code}/sequencer",
        {"transfer_code": "X1"},
    )
    return sequenced["pull_run"]["code"]


def _publish(api: ApiClient, code: str, expected_version: int) -> dict[str, object]:
    published = api.expect_ok("POST", f"/api/outbound-trains/{code}/manifests", {})
    manifest = published["manifest"]
    assert manifest["version"] == expected_version
    assert manifest["code"] == f"MAN-{code}-V{expected_version}"
    assert isinstance(manifest["content_digest"], str) and len(manifest["content_digest"]) == 64
    return dict(manifest)


def _get_version(api: ApiClient, code: str, version: int) -> dict[str, object]:
    return api.expect_ok("GET", f"/api/outbound-trains/{code}/manifests/{version}")["manifest"]


def check_unexecuted_plan(api: ApiClient) -> dict[str, object]:
    # N4-A stacks the eight cars in intake order. C-N4-42 is buried under the
    # two INT-42 cars and three blocker cars; pulling it (and later C-N4-41)
    # therefore produces explicit BUFFER/RETURN moves.
    run_code = _plan(api, "OB-41", ["C-N4-42", "C-N4-41"])
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["reserved"] == 2
    manifest = _publish(api, "OB-41", 1)

    assert manifest["planned_sequence"] == ["C-N4-42", "C-N4-41"]
    assert manifest["assembled_sequence"] == []
    assert manifest["references"]["shift_code"] == "SHIFT-04"
    assert manifest["references"]["pull_run_code"] == run_code
    assert manifest["references"]["pull_run_state"] == "QUEUED"
    assert manifest["references"]["transfer_bay_code"] == "X1"

    summary = manifest["discrepancy_summary"]
    assert summary["pending_count"] == 2
    assert summary["conflict_count"] == 0
    assert summary["ready_to_depart"] is False
    statuses = {entry["position"]: entry["plan_status"] for entry in manifest["entries"]}
    assert statuses == {1: "PENDING", 2: "PENDING"}
    for entry in manifest["entries"]:
        assert entry["car"]["destination"] == "N4"
        origin = entry["origin"]
        assert origin["intake_code"] == "INT-41"
        assert origin["intake_route"] == "RAIL-41"
        assert origin["spotted_track"] is not None
        assert "arrived on INT-41" in origin["source_note"]
    readiness = api.expect_ok("GET", "/api/outbound-trains/OB-41/readiness")
    assert [item["status"] for item in readiness["pending"]] == ["PENDING", "PENDING"]
    assert readiness["conflicts"] == []
    assert readiness["ready_to_depart"] is False
    return manifest


def check_buffered_execution(api: ApiClient, v1: dict[str, object]) -> dict[str, object]:
    # Mid-run snapshot: C-N4-42 is assembled, the next planned car C-N4-41 is
    # still RESERVED on N4-A, and three blockers are parked in the buffer
    # while the rest of the stack is cleared above it.
    api.expect_ok("POST", "/api/pull-runs/RUN-OB-41/advance", {"steps": 14})
    manifest = _publish(api, "OB-41", 2)

    assert manifest["planned_sequence"] == ["C-N4-42", "C-N4-41"]
    assert manifest["assembled_sequence"] == ["C-N4-42"]
    assert manifest["references"]["pull_run_state"] == "RUNNING"
    buffered = {
        (item["car_code"], item["bay_code"]) for item in manifest["yard_context"]["buffered_cars"]
    }
    assert buffered == {
        ("C-N4-44", "X1"),
        ("C-N4-43", "X1"),
        ("C-BLK-44", "X1"),
    }
    assert manifest["yard_context"]["run_current_step"] == 14
    assert manifest["discrepancy_summary"] == {
        "total_slots": 2,
        "pending_count": 1,
        "conflict_count": 0,
        "ready_to_depart": False,
    }
    statuses = {entry["position"]: entry["plan_status"] for entry in manifest["entries"]}
    assert statuses == {1: "MATCH", 2: "PENDING"}
    pending = manifest["discrepancies"][0]
    assert pending["status"] == "PENDING"
    assert pending["planned_car_code"] == "C-N4-41"
    assert pending["reason"] == "planned car not assembled yet"

    # V1 is frozen: its digest and content must survive unchanged.
    stored_v1 = _get_version(api, "OB-41", 1)
    assert stored_v1["content_digest"] == v1["content_digest"]
    assert stored_v1["assembled_sequence"] == []
    verification = api.expect_ok("GET", "/api/outbound-trains/OB-41/manifests/1/verify")["verification"]
    assert verification["digest_valid"] is True
    assert verification["matches_current_yard"] is False
    assert "assembled sequence changed" in verification["divergences"]
    return manifest


def check_assembly_complete(api: ApiClient, v2: dict[str, object]) -> dict[str, object]:
    api.expect_ok("POST", "/api/pull-runs/RUN-OB-41/advance", {"steps": 10})
    yard_before = api.expect_ok("GET", "/api/yard")
    assert yard_before["metrics"]["car_state_counts"]["standing"] == 9

    manifest = _publish(api, "OB-41", 3)
    assert manifest["planned_sequence"] == manifest["assembled_sequence"] == ["C-N4-42", "C-N4-41"]
    assert manifest["discrepancy_summary"] == {
        "total_slots": 2,
        "pending_count": 0,
        "conflict_count": 0,
        "ready_to_depart": True,
    }
    assert all(entry["plan_status"] == "MATCH" for entry in manifest["entries"])
    readiness = api.expect_ok("GET", "/api/outbound-trains/OB-41/readiness")
    assert readiness["ready_to_depart"] is True

    exported = api.expect_ok("GET", "/api/outbound-trains/OB-41/manifests/3/export")
    assert exported["verification"]["digest_valid"] is True
    assert exported["verification"]["matches_current_yard"] is True
    assert exported["departure_readiness"]["ready_to_depart"] is True
    # Export document matches the frozen version exactly.
    assert exported["export"]["document"]["content_digest"] == manifest["content_digest"]

    # The yard-state-only manifest publish must not have moved any car.
    yard = api.expect_ok("GET", "/api/yard")
    assert yard["metrics"]["car_state_counts"]["standing"] == 9
    assert yard["metrics"]["car_state_counts"]["assembled"] == 2

    # Previous versions remain retrievable and byte-stable.
    stored_v2 = _get_version(api, "OB-41", 2)
    assert stored_v2["content_digest"] == v2["content_digest"]
    listing = api.expect_ok("GET", "/api/outbound-trains/OB-41/manifests")
    assert [item["version"] for item in listing["versions"]] == [1, 2, 3]
    assert listing["latest_version"] == 3
    return manifest


def check_conflict_and_correction(api: ApiClient) -> None:
    # C-N4-44 is the top car on N4-A; C-N4-43 sits beneath it, so this order
    # pulls directly with no buffer moves.
    _plan(api, "OB-42", ["C-N4-44", "C-N4-43"])
    v1 = _publish(api, "OB-42", 1)
    assert v1["discrepancy_summary"]["pending_count"] == 2
    assert v1["discrepancy_summary"]["conflict_count"] == 0

    # The first planned car is pulled out of service: the still-unexecuted
    # plan now has a hard conflict, not merely a pending move.
    removed = api.expect_ok(
        "POST",
        "/api/cars/remove",
        {"car_code": "C-N4-44", "reason": "defect hold"},
    )
    assert removed["car"]["state"] == "REMOVED"
    assert removed["affected_outbounds"] == ["OB-42"]

    readiness = api.expect_ok("GET", "/api/outbound-trains/OB-42/readiness")
    assert readiness["ready_to_depart"] is False
    assert len(readiness["pending"]) == 1
    conflicts = readiness["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["position"] == 1
    assert conflicts[0]["planned_car_code"] == "C-N4-44"
    assert conflicts[0]["live_car_state"] == "REMOVED"

    v2 = _publish(api, "OB-42", 2)
    assert v2["discrepancy_summary"]["pending_count"] == 1
    assert v2["discrepancy_summary"]["conflict_count"] == 1
    conflict_entry = next(entry for entry in v2["entries"] if entry["position"] == 1)
    assert conflict_entry["plan_status"] == "CONFLICT"
    assert conflict_entry["car"]["state"] == "REMOVED"
    pending_entry = next(entry for entry in v2["entries"] if entry["position"] == 2)
    assert pending_entry["plan_status"] == "PENDING"

    # Correct the plan by dropping the removed car and deriving a new run.
    replanned = api.expect_ok(
        "POST",
        "/api/outbound-trains/OB-42/replan",
        {"transfer_code": "X1", "car_codes": ["C-N4-43"]},
    )
    assert replanned["superseded_run"] == "RUN-OB-42"
    new_run = replanned["pull_run"]
    assert new_run["code"] == "RUN-OB-42-2"
    assert new_run["state"] == "QUEUED"
    assert replanned["outbound"]["planned_car_codes"] == ["C-N4-43"]

    v3 = _publish(api, "OB-42", 3)
    assert v3["planned_sequence"] == ["C-N4-43"]
    assert v3["references"]["pull_run_code"] == "RUN-OB-42-2"
    assert v3["references"]["run_codes"] == ["RUN-OB-42", "RUN-OB-42-2"]
    assert v3["discrepancy_summary"]["conflict_count"] == 0
    assert v3["discrepancy_summary"]["pending_count"] == 1

    # Finish assembly on the corrected run and confirm the export matches yard.
    api.expect_ok("POST", "/api/pull-runs/RUN-OB-42-2/advance", {"steps": 10})
    v4 = _publish(api, "OB-42", 4)
    assert v4["discrepancy_summary"]["ready_to_depart"] is True
    exported = api.expect_ok("GET", "/api/outbound-trains/OB-42/manifests/4/export")
    assert exported["verification"]["digest_valid"] is True
    assert exported["verification"]["matches_current_yard"] is True

    # The conflict version remains frozen, intact, and retrievable as recorded.
    stored_conflict = _get_version(api, "OB-42", 2)
    assert stored_conflict["content_digest"] == v2["content_digest"]
    assert stored_conflict["planned_sequence"] == ["C-N4-44", "C-N4-43"]
    verify_conflict = api.expect_ok("GET", "/api/outbound-trains/OB-42/manifests/2/verify")["verification"]
    assert verify_conflict["digest_valid"] is True
    assert verify_conflict["matches_current_yard"] is False
    assert "planned sequence changed" in verify_conflict["divergences"]
    assert any("C-N4-43" in item for item in verify_conflict["divergences"])

    # Cross references: outbound points to all versions; journal events exist.
    listing = api.expect_ok("GET", "/api/outbound-trains/OB-42/manifests")
    assert [item["version"] for item in listing["versions"]] == [1, 2, 3, 4]
    shift = api.expect_ok("GET", "/api/shifts/SHIFT-04")
    kinds = [event["kind"] for event in shift["events"]]
    assert kinds.count("MANIFEST_PUBLISHED") == 7
    assert "CAR_REMOVED" in kinds
    assert "PULL_REPLANNED" in kinds
    manifest_events = [
        event for event in shift["events"] if event["kind"] == "MANIFEST_PUBLISHED"
    ]
    assert manifest_events[-1]["payload"]["manifest_code"] == "MAN-OB-42-V4"


def check_blocker_only_move(api: ApiClient) -> None:
    """Regression: a BUFFER step moves only a non-planned blocker car.

    E7-A stacks C-E7-41, C-E7-BLK, C-E7-42. The plan pulls C-E7-42 first, so
    the very first step buffers C-E7-BLK without touching any planned car or
    the assembled sequence. A frozen V1 must therefore no longer verify as
    matching the yard even though every planned entry looks unchanged.
    """

    run_code = _plan(api, "OB-43", ["C-E7-42", "C-E7-41"], destination="E7")
    v1 = _publish(api, "OB-43", 1)
    assert v1["yard_context"]["buffered_cars"] == []
    assert v1["references"]["pull_run_state"] == "QUEUED"
    assert v1["references"]["pull_run_code"] == run_code

    readiness0 = api.expect_ok("GET", "/api/outbound-trains/OB-43/readiness")
    assert len(readiness0["pending"]) == 2
    assert readiness0["conflicts"] == []
    assert readiness0["buffered_cars"] == []
    assert readiness0["pull_run"]["state"] == "QUEUED"
    assert readiness0["pull_run"]["current_step"] == 0

    verify0 = api.expect_ok("GET", "/api/outbound-trains/OB-43/manifests/1/verify")["verification"]
    assert verify0["digest_valid"] is True
    assert verify0["matches_current_yard"] is True

    # Advance a single step: only the blocker car is buffered; planned cars
    # stay RESERVED on E7-A and the assembled consist is still empty.
    api.expect_ok("POST", "/api/pull-runs/RUN-OB-43/advance", {"steps": 1})

    readiness1 = api.expect_ok("GET", "/api/outbound-trains/OB-43/readiness")
    assert len(readiness1["pending"]) == 2
    assert readiness1["conflicts"] == []
    assert readiness1["ready_to_depart"] is False
    buffered = {(item["car_code"], item["bay_code"], item["planned_for_train"]) for item in readiness1["buffered_cars"]}
    assert buffered == {("C-E7-BLK", "X1", False)}
    assert readiness1["pull_run"]["state"] == "RUNNING"
    assert readiness1["pull_run"]["current_step"] == 1
    assert readiness1["pull_run"]["remaining_steps"] == readiness1["pull_run"]["total_steps"] - 1

    # Core regression: the export verification must detect the buffer/run
    # change instead of reporting the frozen plan as still yard-consistent.
    export1 = api.expect_ok("GET", "/api/outbound-trains/OB-43/manifests/1/export")
    verification = export1["verification"]
    assert verification["digest_valid"] is True
    assert verification["matches_current_yard"] is False
    joined = " | ".join(verification["divergences"])
    assert "C-E7-BLK newly buffered in X1" in joined
    assert "RUNNING" in joined
    assert "advanced from step 0 to 1" in joined
    # Planned cars themselves have not moved yet.
    assert not any("C-E7-42" in item or "C-E7-41" in item for item in verification["divergences"])
    # The frozen document content is still byte-intact.
    stored = _get_version(api, "OB-43", 1)
    assert stored["content_digest"] == v1["content_digest"]
    assert stored["yard_context"]["buffered_cars"] == []

    # Publishing V2 at the blocker-only point freezes the live buffer; it must
    # verify against the current yard right now.
    v2 = _publish(api, "OB-43", 2)
    buffered_v2 = {(item["car_code"], item["bay_code"]) for item in v2["yard_context"]["buffered_cars"]}
    assert buffered_v2 == {("C-E7-BLK", "X1")}
    verify2 = api.expect_ok("GET", "/api/outbound-trains/OB-43/manifests/2/verify")["verification"]
    assert verify2["digest_valid"] is True
    assert verify2["matches_current_yard"] is True

    # Finish assembly: blockers return to the track and both planned cars come
    # out in order. V1/V2 diverge; the completed V3 matches the yard, and the
    # driver sees no pending or conflicting difference.
    api.expect_ok("POST", "/api/pull-runs/RUN-OB-43/advance", {"steps": 10})
    v3 = _publish(api, "OB-43", 3)
    assert v3["planned_sequence"] == v3["assembled_sequence"] == ["C-E7-42", "C-E7-41"]
    assert v3["discrepancy_summary"]["ready_to_depart"] is True
    assert v3["yard_context"]["buffered_cars"] == []

    export3 = api.expect_ok("GET", "/api/outbound-trains/OB-43/manifests/3/export")
    assert export3["verification"]["digest_valid"] is True
    assert export3["verification"]["matches_current_yard"] is True
    assert export3["departure_readiness"]["ready_to_depart"] is True
    assert export3["departure_readiness"]["buffered_cars"] == []

    verify1_after = api.expect_ok("GET", "/api/outbound-trains/OB-43/manifests/1/verify")["verification"]
    assert verify1_after["matches_current_yard"] is False
    assert verify1_after["digest_valid"] is True
    joined1 = " | ".join(verify1_after["divergences"])
    assert "assembled sequence changed" in joined1
    assert "QUEUED" in joined1 and "COMPLETED" in joined1

    # V2 froze the blocker in the buffer, so its return is reported explicitly.
    verify2_after = api.expect_ok("GET", "/api/outbound-trains/OB-43/manifests/2/verify")["verification"]
    assert verify2_after["matches_current_yard"] is False
    assert "C-E7-BLK left buffer X1" in " | ".join(verify2_after["divergences"])

    listing = api.expect_ok("GET", "/api/outbound-trains/OB-43/manifests")
    assert [item["version"] for item in listing["versions"]] == [1, 2, 3]


def run(api: ApiClient) -> None:
    setup_yard(api)
    v1 = check_unexecuted_plan(api)
    v2 = check_buffered_execution(api, v1)
    check_assembly_complete(api, v2)
    check_conflict_and_correction(api)
    check_blocker_only_move(api)


if __name__ == "__main__":
    raise SystemExit(run_check("wf_departure_manifest", run))
