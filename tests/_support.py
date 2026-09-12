"""Shared helpers for the switchyard key-path test suite.

The suite uses only the Python standard library (``unittest``).  Every test
runs against a :class:`tempfile.TemporaryDirectory`, so the repository's own
``data/`` tree is never touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SRC_DIR))

from switchyard.domain.enums import CarKind, TrackPurpose, TrackState  # noqa: E402
from switchyard.domain.car import FreightCar  # noqa: E402
from switchyard.domain.errors import DomainError  # noqa: E402
from switchyard.domain.track import BufferBay, StandingTrack  # noqa: E402
from switchyard.service.context import YardApplication  # noqa: E402

VALID_TIME = "2026-09-10T09:00:00Z"


def car_payload(code: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "kind": "BOX",
        "destination": "N4",
        "loaded": False,
        "length_m": 18,
        "danger_class": "NONE",
    }
    payload.update(overrides)
    return payload


def intake_payload(code: str, cars: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "route": "RAIL-1",
        "arrival_at": VALID_TIME,
        "cars": cars,
    }
    payload.update(overrides)
    return payload


def shift_payload(code: str = "SHIFT-1", dispatcher: str = "DISP") -> dict[str, Any]:
    return {"code": code, "dispatcher": dispatcher, "opened_at": VALID_TIME}


def outbound_payload(code: str, destination: str, car_codes: list[str]) -> dict[str, Any]:
    return {"code": code, "destination": destination, "car_codes": car_codes}


def make_car(code: str, **overrides: Any) -> FreightCar:
    payload: dict[str, Any] = {
        "kind": CarKind.BOX,
        "destination": "N4",
        "loaded": False,
        "length_m": 18,
        "danger_class": "NONE",
    }
    payload.update(overrides)
    if isinstance(payload["kind"], str):
        payload["kind"] = CarKind.parse(payload["kind"])
    return FreightCar(code=code, **payload)


def make_track(code: str, **overrides: Any) -> StandingTrack:
    payload: dict[str, Any] = {
        "purpose": TrackPurpose.GENERAL,
        "capacity_cars": 10,
        "capacity_length_m": 300,
    }
    payload.update(overrides)
    if isinstance(payload["purpose"], str):
        payload["purpose"] = TrackPurpose.parse(payload["purpose"])
    if "state" in payload and isinstance(payload["state"], str):
        payload["state"] = TrackState.parse(payload["state"])
    return StandingTrack(code=code, **payload)


def make_bay(code: str = "X1", capacity_cars: int = 10) -> BufferBay:
    return BufferBay(code=code, capacity_cars=capacity_cars)


class TempDirCase(unittest.TestCase):
    """TestCase that owns a fresh temporary data directory."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="switchyard-test-")
        self.data_dir = Path(self._tempdir.name) / "data"
        self.app = YardApplication(self.data_dir)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    # -- service-level convenience wrappers ---------------------------------

    def call_service(self, fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Invoke a service command and return its data payload."""
        result = fn(self.app, *args, **kwargs)
        self.assertIsInstance(result, dict)
        return result

    def assert_service_error(
        self,
        fn: Any,
        *args: Any,
        status: int | None = None,
        code: str | None = None,
        field: str | None = None,
        **kwargs: Any,
    ) -> DomainError:
        """Assert the service command raises a DomainError and return it."""
        try:
            fn(self.app, *args, **kwargs)
        except DomainError as exc:
            if status is not None:
                self.assertEqual(
                    exc.status,
                    status,
                    msg=f"expected HTTP status {status}, got {exc.status} ({exc.code}: {exc.message})",
                )
            if code is not None:
                self.assertEqual(exc.code, code, msg=f"error code mismatch: {exc.as_dict()}")
            if field is not None:
                self.assertIn(
                    field,
                    exc.fields,
                    msg=f"expected field {field!r} in error fields, got {exc.fields}",
                )
            return exc
        self.fail(f"expected {fn.__name__} to raise a DomainError")

    # -- full workflow builders ---------------------------------------------

    def open_shift(self, code: str = "SHIFT-1", dispatcher: str = "DISP") -> dict[str, Any]:
        from switchyard.service.shift_service import open_shift

        return open_shift(self.app, shift_payload(code, dispatcher))

    def create_intake(self, code: str, cars: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
        from switchyard.service.intake_service import create_intake

        return create_intake(self.app, intake_payload(code, cars, **overrides))

    def classify(self, code: str) -> dict[str, Any]:
        from switchyard.service.intake_service import classify_intake_command

        return classify_intake_command(self.app, code)

    def create_outbound(self, code: str, destination: str, car_codes: list[str]) -> dict[str, Any]:
        from switchyard.service.outbound_service import create_outbound

        return create_outbound(self.app, outbound_payload(code, destination, car_codes))

    def sequence(self, outbound_code: str, transfer_code: str = "X1") -> dict[str, Any]:
        from switchyard.service.outbound_service import sequence_outbound

        return sequence_outbound(self.app, outbound_code, {"transfer_code": transfer_code})

    def advance(self, run_code: str, steps: int | None = None) -> dict[str, Any]:
        from switchyard.service.run_service import advance_run

        payload = {} if steps is None else {"steps": steps}
        return advance_run(self.app, run_code, payload)

    def depart(self, outbound_code: str) -> dict[str, Any]:
        from switchyard.service.run_service import depart_outbound

        return depart_outbound(self.app, outbound_code)

    def close_shift(self, shift_code: str) -> dict[str, Any]:
        from switchyard.service.closure_service import close_shift

        return close_shift(self.app, shift_code)

    def load(self) -> Any:
        return self.app.load()
