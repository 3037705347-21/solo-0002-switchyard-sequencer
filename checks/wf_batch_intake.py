"""Workflow check: batch intake from a local manifest file.

Exercises the morning-peak case where several inbound trains arrive together:
a valid multi-train file, a file with a car code duplicated across trains, a
file mixing dimension/hazard violations with an empty car list, and re-import
conflict behavior.  Every rejected file must leave zero partial state.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from support import ApiClient, run_check


def write_manifest(directory: Path, name: str, document: dict[str, Any]) -> Path:
    path = directory / name
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def valid_batch() -> dict[str, Any]:
    return {
        "code": "BATCH-20260912-01",
        "received_at": "2026-09-12T08:30:00Z",
        "note": "morning peak arrivals",
        "trains": [
            {
                "code": "INT-101",
                "route": "RAIL-11",
                "arrival_at": "2026-09-12T08:10:00Z",
                "cars": [
                    {
                        "code": "C-N4-101",
                        "kind": "BOX",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-N4-102",
                        "kind": "HOPPER",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 20,
                        "danger_class": "NONE",
                    },
                ],
            },
            {
                "code": "INT-102",
                "route": "RAIL-12",
                "arrival_at": "2026-09-12T08:12:00Z",
                "cars": [
                    {
                        "code": "C-E7-101",
                        "kind": "FLAT",
                        "destination": "E7",
                        "loaded": False,
                        "length_m": 16,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-HAZ-101",
                        "kind": "TANK",
                        "destination": "W9",
                        "loaded": True,
                        "length_m": 22,
                        "danger_class": "D1",
                    },
                ],
            },
        ],
    }


def cross_train_duplicate_batch() -> dict[str, Any]:
    document = valid_batch()
    document["code"] = "BATCH-20260912-02"
    document["note"] = "duplicate car across trains"
    # C-N4-101 belongs to INT-101; repeat it inside INT-102 with a fresh code.
    document["trains"][1]["cars"].append(
        {
            "code": "C-N4-101",
            "kind": "BOX",
            "destination": "N4",
            "loaded": True,
            "length_m": 18,
            "danger_class": "NONE",
        }
    )
    return document


def dimension_and_empty_batch() -> dict[str, Any]:
    return {
        "code": "BATCH-20260912-03",
        "received_at": "2026-09-12T08:45:00Z",
        "note": "oversize car and an empty train",
        "trains": [
            {
                "code": "INT-201",
                "route": "RAIL-21",
                "arrival_at": "2026-09-12T08:40:00Z",
                "cars": [
                    {
                        "code": "C-S2-201",
                        "kind": "BOX",
                        "destination": "S2",
                        "loaded": True,
                        "length_m": 42,  # above MAX_CAR_LENGTH_M (35)
                        "danger_class": "D9",  # unknown hazard class
                    },
                    {
                        "code": "C-W9-201",
                        "kind": "TANK",
                        "destination": "W9",
                        "loaded": False,
                        "length_m": 24,
                        "danger_class": "D2",
                    },
                ],
            },
            {
                "code": "INT-202",
                "route": "RAIL-22",
                "arrival_at": "2026-09-12T08:42:00Z",
                "cars": [],  # empty car list
            },
        ],
    }


def invalid_loaded_batch() -> dict[str, Any]:
    """Cars whose loaded flag is not a JSON boolean must locate, not 500."""
    return {
        "code": "BATCH-20260912-04",
        "received_at": "2026-09-12T08:50:00Z",
        "note": "non-boolean loaded flags",
        "trains": [
            {
                "code": "INT-301",
                "route": "RAIL-31",
                "arrival_at": "2026-09-12T08:48:00Z",
                "cars": [
                    {
                        "code": "C-N4-301",
                        "kind": "BOX",
                        "destination": "N4",
                        "loaded": "true",  # string, not boolean
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-N4-302",
                        "kind": "BOX",
                        "destination": "N4",
                        "loaded": 1,  # integer, not boolean
                        "length_m": 18,
                        "danger_class": "NONE",
                    },
                    {
                        "code": "C-N4-303",
                        "kind": "HOPPER",
                        "destination": "N4",
                        "loaded": True,
                        "length_m": 20,
                        "danger_class": "NONE",
                    },
                ],
            }
        ],
    }


def issue_map(error: dict[str, Any]) -> dict[str, str]:
    report = error.get("details", {}).get("report", {})
    issues = report.get("issues", [])
    return {item["locator"]: item["code"] for item in issues}


def run(api: ApiClient) -> None:
    opened = api.expect_ok(
        "POST",
        "/api/shifts",
        {"code": "SHIFT-01", "dispatcher": "LIN", "opened_at": "2026-09-12T08:00:00Z"},
    )
    assert opened["state"] == "OPEN"

    with tempfile.TemporaryDirectory(prefix="switchyard-batch-") as tmp:
        directory = Path(tmp)
        good_path = write_manifest(directory, "good.json", valid_batch())
        duplicate_path = write_manifest(directory, "duplicate.json", cross_train_duplicate_batch())
        bad_path = write_manifest(directory, "bad.json", dimension_and_empty_batch())
        bool_path = write_manifest(directory, "bad_loaded.json", invalid_loaded_batch())

        # --- 1. valid multi-train batch ----------------------------------
        yard_before = api.expect_ok("GET", "/api/yard")
        events_before = yard_before["metrics"]["event_count"]
        version_before = yard_before["metrics"]["version"]

        imported = api.expect_ok("POST", "/api/intake-batches", {"source_path": str(good_path)})
        batch = imported["batch"]
        assert batch["code"] == "BATCH-20260912-01"
        assert batch["source_kind"] == "file"
        assert batch["source_path"] == str(good_path.resolve())
        assert batch["source_sha256"] and len(batch["source_sha256"]) == 64
        assert batch["train_codes"] == ["INT-101", "INT-102"]
        assert batch["car_codes"] == ["C-N4-101", "C-N4-102", "C-E7-101", "C-HAZ-101"]

        report = imported["report"]
        assert report["accepted"] is True
        assert report["issue_count"] == 0
        assert [t["code"] for t in report["trains"]] == ["INT-101", "INT-102"]
        assert all(t["status"] == "accepted" for t in report["trains"])
        assert all(c["status"] == "accepted" for t in report["trains"] for c in t["cars"])

        # Same final state as single create: trains are OPEN and cars RECEIVED.
        assert {item["state"] for item in imported["intakes"]} == {"OPEN"}
        assert {item["state"] for item in imported["cars"]} == {"RECEIVED"}
        assert {item["location"] for item in imported["cars"]} == {"INTAKE"}
        assert all(item["batch_code"] == "BATCH-20260912-01" for item in imported["intakes"])

        # Three workflow events (two trains + one batch summary) recorded in a
        # single commit: version bumps once per recorded event plus once on save.
        yard_after = api.expect_ok("GET", "/api/yard")
        assert yard_after["metrics"]["event_count"] == events_before + 3
        assert yard_after["metrics"]["version"] == version_before + 4

        detail = api.expect_ok("GET", "/api/intake-batches/BATCH-20260912-01")
        assert detail["batch"]["code"] == "BATCH-20260912-01"
        assert {item["code"] for item in detail["intakes"]} == {"INT-101", "INT-102"}
        assert len(detail["cars"]) == 4
        kinds = {event["kind"] for event in detail["events"]}
        assert kinds == {"TRAIN_RECEIVED", "BATCH_IMPORTED"}
        assert len(detail["events"]) == 3
        assert detail["events"][-1]["kind"] == "BATCH_IMPORTED"
        assert detail["events"][-1]["payload"]["source_sha256"] == batch["source_sha256"]

        # Classification reaches the same result as a single intake.
        for train_code in ("INT-101", "INT-102"):
            classified = api.expect_ok(
                "POST", f"/api/intake-trains/{train_code}/classify", {}
            )
            assert classified["intake"]["state"] == "CLASSIFIED"
            assert classified["unplaced"] == []
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["car_state_counts"]["standing"] == 4

        # --- 2. cross-train duplicate car code ---------------------------
        dup_error = api.expect_error(
            "POST", "/api/intake-batches", {"source_path": str(duplicate_path)}
        )
        assert dup_error["code"] == "VALIDATION_ERROR"
        dup_issues = issue_map(dup_error)
        assert dup_issues["trains[1].cars[2].code"] == "DUPLICATE_CAR_IN_BATCH"
        dup_report = dup_error["details"]["report"]
        assert dup_report["accepted"] is False
        # located per train and per car
        train_102 = dup_report["trains"][1]
        assert train_102["code"] == "INT-102"
        assert train_102["status"] == "rejected"
        assert train_102["cars"][2]["issues"][0]["car_code"] == "C-N4-101"
        assert train_102["cars"][2]["issues"][0]["train_code"] == "INT-102"

        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["total_cars"] == 4  # nothing written
        assert yard["metrics"]["car_state_counts"]["standing"] == 4
        assert yard["metrics"]["active_intakes"] == []

        # --- 3. dimension/hazard violations plus an empty car list --------
        bad_error = api.expect_error(
            "POST", "/api/intake-batches", {"source_path": str(bad_path)}
        )
        assert bad_error["code"] == "VALIDATION_ERROR"
        bad_issues = issue_map(bad_error)
        assert bad_issues["trains[0].cars[0].length_m"] == "CAR_LENGTH_M_INVALID"
        assert bad_issues["trains[0].cars[0].danger_class"] == "CAR_DANGER_CLASS_INVALID"
        assert bad_issues["trains[1].cars"] == "EMPTY_CONSIST"
        # The valid car on INT-201 and the whole INT-202 train are still located.
        bad_report = bad_error["details"]["report"]
        statuses = {(t["code"], t["status"]) for t in bad_report["trains"]}
        assert statuses == {("INT-201", "rejected"), ("INT-202", "rejected")}
        assert bad_report["trains"][0]["cars"][1]["code"] == "C-W9-201"

        # All-or-nothing: no train, no car, no provenance record landed.
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["total_cars"] == 4
        missing = api.expect_error("GET", "/api/intake-batches/BATCH-20260912-03")
        assert missing["code"] == "NOT_FOUND"

        # Re-sending the good file after failures still works (no poisoning).
        still_good = api.expect_error(
            "POST", "/api/intake-batches", {"source_path": str(good_path)}
        )
        assert still_good["code"] == "CONFLICT"
        conflict_codes = {item["code"] for item in still_good["details"]["report"]["issues"]}
        assert "BATCH_ALREADY_IMPORTED" in conflict_codes
        assert "BATCH_SOURCE_ALREADY_IMPORTED" in conflict_codes
        assert "TRAIN_ALREADY_IN_YARD" in conflict_codes
        assert "CAR_ALREADY_IN_YARD" in conflict_codes
        # every train/car clash is located
        conflict_issues = issue_map(still_good)
        assert "trains[0].code" in conflict_issues
        assert "trains[0].cars[0].code" in conflict_issues
        assert conflict_issues["code"] == "BATCH_ALREADY_IMPORTED"
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["total_cars"] == 4

        # Same manifest content under a different batch code is also rejected
        # via the content hash, even before train/car uniqueness checks run.
        renamed = valid_batch()
        renamed["code"] = "BATCH-20260912-09"
        renamed_path = write_manifest(directory, "renamed.json", renamed)
        renamed_error = api.expect_error(
            "POST", "/api/intake-batches", {"source_path": str(renamed_path)}
        )
        assert renamed_error["code"] == "CONFLICT"
        renamed_codes = {item["code"] for item in renamed_error["details"]["report"]["issues"]}
        assert "BATCH_SOURCE_ALREADY_IMPORTED" in renamed_codes
        assert renamed_error["details"]["report"]["issue_count"] >= 1

        # --- 4. non-boolean loaded flags must locate per car, never 500 ----
        bool_error = api.request("POST", "/api/intake-batches", {"source_path": str(bool_path)})
        status, body = bool_error
        assert status == 422, f"expected 422 for non-boolean loaded, got {status} {body}"
        assert body["ok"] is False
        bool_issues = issue_map(body["error"])
        assert bool_issues["trains[0].cars[0].loaded"] == "CAR_LOADED_INVALID"
        assert bool_issues["trains[0].cars[1].loaded"] == "CAR_LOADED_INVALID"
        # the valid third car is still reported with its locator/status
        bool_report = body["error"]["details"]["report"]
        assert bool_report["trains"][0]["cars"][2]["code"] == "C-N4-303"
        assert bool_report["trains"][0]["cars"][2]["status"] == "accepted"
        assert bool_report["trains"][0]["status"] == "rejected"
        # whole-batch rejection: nothing from this file landed
        yard = api.expect_ok("GET", "/api/yard")
        assert yard["metrics"]["total_cars"] == 4
        missing = api.expect_error("GET", "/api/intake-batches/BATCH-20260912-04")
        assert missing["code"] == "NOT_FOUND"

        # Structural garbage also rejects cleanly with batch-level locators.
        broken_path = directory / "broken.json"
        broken_path.write_text('{"code": "BATCH-X", "trains": []}', encoding="utf-8")
        broken_error = api.expect_error(
            "POST", "/api/intake-batches", {"source_path": str(broken_path)}
        )
        assert broken_error["code"] == "VALIDATION_ERROR"
        assert issue_map(broken_error)["trains"] == "EMPTY_CONSIST"


if __name__ == "__main__":
    raise SystemExit(run_check("wf_batch_intake", run))
