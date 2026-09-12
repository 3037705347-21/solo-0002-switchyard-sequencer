"""Input boundary tests for intake train and freight car payloads.

Covers ``domain.validators`` and the predicate helpers in ``domain.rules``:
car/entity code shapes, kinds, destinations, hazard classes, integer ranges,
booleans, empty/overlong consists, duplicate codes, timestamp normalization,
and per-car error attribution.

Contract note: validators raise ``ValidationError(message, **{field: ...})``
and the constructor puts those kwargs into ``payload["details"]`` rather than
into ``fields``.  Tests assert the actual placement, and two tests marked
``expectedFailure`` pin the documented interface intent (field attribution in
``fields``, domain error for bad timestamps).
"""

from __future__ import annotations

import unittest

from _support import car_payload, intake_payload
from switchyard.domain.errors import ValidationError
from switchyard.domain.rules import (
    is_car_code,
    is_entity_code,
    kind_known,
    hazard_known,
    destination_known,
)
from switchyard.domain.validators import (
    build_intake_payload,
    build_outbound_payload,
    build_shift_payload,
    parse_advance_steps,
    parse_car_input,
    parse_transfer_code,
    require_boolean,
    require_integer,
    require_object,
    require_text,
)


def assert_detail(testcase: unittest.TestCase, exc: ValidationError, key: str) -> None:
    """Field problems currently land in payload/details; accept both shapes."""
    if key in exc.fields:
        return
    testcase.assertIn(
        key,
        exc.payload,
        msg=f"{key!r} not in fields={exc.fields} or details={exc.payload}",
    )


class RulePredicateTest(unittest.TestCase):
    def test_car_code_shapes(self) -> None:
        # Length rule applies to the whole string: 6..24 characters.
        for valid in ("C-N4-1", "C-ABCD", "C-A_B-99", " c-n4-1 ", "C-" + "X" * 22):
            with self.subTest(value=valid):
                self.assertTrue(is_car_code(valid))
        # C-1 (3 chars) is too short; C- + 23 X's (25 chars) is too long.
        for invalid in ("", "X-N4-1", "C-1", "C-", "C--", "C N4 1", "C-N4.1", "C-" + "X" * 23):
            with self.subTest(value=invalid):
                self.assertFalse(is_car_code(invalid))

    def test_entity_code_prefix(self) -> None:
        self.assertTrue(is_entity_code("INT-01", "INT"))
        self.assertTrue(is_entity_code("OB-2", "OB"))
        # The prefix is matched literally: "OB_" is not the "OB-" prefix.
        self.assertFalse(is_entity_code("OB_2", "OB"))
        self.assertFalse(is_entity_code("XX-01", "INT"))
        self.assertFalse(is_entity_code("", "INT"))
        self.assertTrue(is_entity_code("ANYTHING"))

    def test_known_enum_predicates(self) -> None:
        for kind in ("BOX", "HOPPER", "FLAT", "TANK", "REEFER", "box"):
            with self.subTest(kind=kind):
                self.assertTrue(kind_known(kind))
        self.assertFalse(kind_known("BOXCAR"))
        self.assertFalse(kind_known(" tanker "))
        for destination in ("N4", "E7", "S2", "W9", " n4"):
            self.assertTrue(destination_known(destination))
        self.assertFalse(destination_known("N5"))
        for hazard in ("NONE", "D1", "D2"):
            self.assertTrue(hazard_known(hazard))
        self.assertFalse(hazard_known("D9"))


class RequirePrimitiveTest(unittest.TestCase):
    def test_require_object(self) -> None:
        self.assertEqual(require_object({"a": 1}, "payload"), {"a": 1})
        for bad in (None, [], "x", 1, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError) as caught:
                    require_object(bad, "payload")
                self.assertIn("payload", caught.exception.message)

    def test_require_text_trims_and_bounds(self) -> None:
        self.assertEqual(require_text("  AB ", "f"), "AB")
        for bad in (None, "", "   ", 1, ["x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    require_text(bad, "f")
        with self.assertRaises(ValidationError) as caught:
            require_text("x" * 41, "f", max_length=40)
        self.assertIn("too long", caught.exception.message)
        self.assertEqual(require_text("x" * 40, "f", max_length=40), "x" * 40)

    def test_require_boolean_rejects_int(self) -> None:
        self.assertTrue(require_boolean(True, "f"))
        self.assertFalse(require_boolean(False, "f"))
        self.assertTrue(require_boolean(None, "f", default=True))
        for bad in (0, 1, "true"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    require_boolean(bad, "f")

    def test_require_integer_bounds(self) -> None:
        self.assertEqual(require_integer(8, "f", 8, 35), 8)
        self.assertEqual(require_integer(35, "f", 8, 35), 35)
        for bad in (True, False, 8.0, "8", None, 7, 36):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    require_integer(bad, "f", 8, 35)


class ParseCarInputTest(unittest.TestCase):
    def test_valid_car_normalized(self) -> None:
        parsed = parse_car_input(
            car_payload(
                " c-n4-1 ",
                kind="box",
                destination=" n4 ",
                danger_class="d1",
                length_m=20,
                loaded=True,
                note="  keep  ",
            )
        )
        self.assertEqual(parsed.code, "C-N4-1")
        self.assertEqual(parsed.kind, "BOX")
        self.assertEqual(parsed.destination, "N4")
        self.assertEqual(parsed.danger_class, "D1")
        self.assertEqual(parsed.length_m, 20)
        self.assertTrue(parsed.loaded)
        self.assertEqual(parsed.note, "keep")

    def test_note_defaults_and_truncation(self) -> None:
        parsed = parse_car_input(car_payload("C-N4-9", note=None))
        self.assertEqual(parsed.note, "")
        parsed_long = parse_car_input(car_payload("C-N4-9", note="z" * 500))
        self.assertEqual(len(parsed_long.note), 200)

    def test_invalid_fields_are_reported(self) -> None:
        cases = [
            ({"code": ""}, "code", "required"),
            ({"code": "BAD-1"}, "code", "invalid car code"),
            ({"kind": "BOXCAR"}, "kind", "unknown car kind"),
            ({"kind": ""}, "kind", "required"),
            ({"destination": "N5"}, "destination", "unknown destination"),
            ({"length_m": 7}, "length_m", "out of range"),
            ({"length_m": 36}, "length_m", "out of range"),
            ({"length_m": "18"}, "length_m", "integer"),
            ({"length_m": 18.0}, "length_m", "integer"),
            ({"danger_class": "D9"}, "danger_class", "unknown hazard"),
            ({"danger_class": ""}, "danger_class", "required"),
            ({"loaded": "yes"}, "loaded", "boolean"),
        ]
        for overrides, field, fragment in cases:
            with self.subTest(field=field):
                payload = car_payload("C-N4-1")
                payload.update(overrides)
                with self.assertRaises(ValidationError) as caught:
                    parse_car_input(payload)
                self.assertIn(fragment, caught.exception.message)
                assert_detail(self, caught.exception, field)

    @unittest.expectedFailure
    def test_contract_field_problems_should_populate_fields_mapping(self) -> None:
        # CONTRACT GAP: every parse_car_input error is constructed as
        # ValidationError(message, **{field: [...]}) which the dataclass
        # routes into payload/details instead of fields.  HTTP clients
        # expecting error.fields per the error envelope find it empty.
        with self.assertRaises(ValidationError) as caught:
            parse_car_input(car_payload("C-N4-1", kind="BOXCAR"))
        self.assertIn("kind", caught.exception.fields)


class BuildIntakePayloadTest(unittest.TestCase):
    def _payload(self, **overrides: object) -> dict[str, object]:
        return intake_payload("INT-1", [car_payload("C-N4-1")], **overrides)

    def test_valid_train_normalizes_timestamp(self) -> None:
        train, cars = build_intake_payload(self._payload(arrival_at="2026-09-10 09:00:00"))
        self.assertEqual(train.code, "INT-1")
        self.assertEqual(train.arrival_at, "2026-09-10T09:00:00Z")
        self.assertEqual(train.consist, ["C-N4-1"])
        self.assertEqual(cars[0].code, "C-N4-1")

    def test_train_code_must_use_int_prefix(self) -> None:
        with self.assertRaises(ValidationError):
            build_intake_payload(intake_payload("XX-1", [car_payload("C-N4-1")]))

    def test_route_required(self) -> None:
        with self.assertRaises(ValidationError):
            build_intake_payload(self._payload(route=""))

    def test_consist_empty_or_missing(self) -> None:
        payload = self._payload()
        payload["cars"] = []
        with self.assertRaises(ValidationError) as caught:
            build_intake_payload(payload)
        assert_detail(self, caught.exception, "cars")
        payload["cars"] = None
        with self.assertRaises(ValidationError):
            build_intake_payload(payload)

    def test_consist_too_large(self) -> None:
        payload = self._payload()
        payload["cars"] = [car_payload(f"C-N4-{index}") for index in range(1, 22)]
        with self.assertRaises(ValidationError) as caught:
            build_intake_payload(payload)
        assert_detail(self, caught.exception, "cars")

    def test_boundary_consist_size_20_accepted(self) -> None:
        payload = self._payload()
        payload["cars"] = [car_payload(f"C-N4-{index}") for index in range(1, 21)]
        _train, cars = build_intake_payload(payload)
        self.assertEqual(len(cars), 20)

    def test_duplicate_car_code_reported_at_index(self) -> None:
        payload = self._payload()
        payload["cars"] = [car_payload("C-N4-1"), car_payload("C-N4-2"), car_payload("C-N4-1")]
        with self.assertRaises(ValidationError) as caught:
            build_intake_payload(payload)
        assert_detail(self, caught.exception, "cars[2].code")

    def test_nested_car_error_is_attributed_to_index(self) -> None:
        payload = self._payload()
        payload["cars"] = [car_payload("C-N4-1"), car_payload("C-N4-2", length_m=1)]
        # Actual behavior: the error message keeps only the innermost text;
        # neither fields nor details carry the cars[1].length_m location.
        with self.assertRaises(ValidationError) as caught:
            build_intake_payload(payload)
        self.assertIn("length_m", caught.exception.message)
        self.assertEqual(caught.exception.fields, {})
        self.assertEqual(caught.exception.payload, {})

    @unittest.expectedFailure
    def test_contract_nested_error_should_carry_prefixed_field_key(self) -> None:
        # CONTRACT GAP: build_intake_payload re-raises as
        # ValidationError(exc.message, fields=prefixed) but the dataclass puts
        # positional message before fields, and the nested validator already
        # discarded its field mapping; clients cannot locate the bad car.
        payload = self._payload()
        payload["cars"] = [car_payload("C-N4-1"), car_payload("C-N4-2", length_m=1)]
        with self.assertRaises(ValidationError) as caught:
            build_intake_payload(payload)
        self.assertTrue(
            "cars[1].length_m" in caught.exception.fields
            or "cars[1].length_m" in caught.exception.payload
        )

    def test_non_object_car_attributed(self) -> None:
        payload = self._payload()
        payload["cars"] = ["not-an-object"]
        # Actual behavior: location appears only in the human-readable message.
        with self.assertRaises(ValidationError) as caught:
            build_intake_payload(payload)
        self.assertIn("cars[0]", caught.exception.message)
        self.assertEqual(caught.exception.fields, {})

    @unittest.expectedFailure
    def test_contract_non_object_car_should_appear_in_fields(self) -> None:
        # CONTRACT GAP: require_object raises ValidationError(message) with no
        # field mapping, and the wrapper does not synthesize one.
        payload = self._payload()
        payload["cars"] = ["not-an-object"]
        with self.assertRaises(ValidationError) as caught:
            build_intake_payload(payload)
        self.assertIn("cars[0]", caught.exception.fields)

    def test_bad_timestamp_currently_raises_plain_value_error(self) -> None:
        # Documents actual behavior: a malformed arrival_at escapes as a
        # ValueError (mapped to HTTP 500 by the server), not ValidationError.
        with self.assertRaises(ValueError):
            build_intake_payload(self._payload(arrival_at="not-a-time"))

    @unittest.expectedFailure
    def test_contract_bad_timestamp_should_be_validation_error(self) -> None:
        # CONTRACT GAP: boundary input should produce ValidationError (HTTP
        # 422), not a raw ValueError (HTTP 500).
        with self.assertRaises(ValidationError):
            build_intake_payload(self._payload(arrival_at="not-a-time"))


class BuildOutboundPayloadTest(unittest.TestCase):
    def test_valid_outbound(self) -> None:
        code, destination, codes = build_outbound_payload(
            outbound_payload_raw("OB-1", "n4", [" c-n4-1 ", "C-N4-2"])
        )
        self.assertEqual(code, "OB-1")
        self.assertEqual(destination, "N4")
        self.assertEqual(codes, ["C-N4-1", "C-N4-2"])

    def test_code_prefix_and_destination(self) -> None:
        with self.assertRaises(ValidationError):
            build_outbound_payload(outbound_payload_raw("XX-1", "N4", ["C-N4-1"]))
        with self.assertRaises(ValidationError) as caught:
            build_outbound_payload(outbound_payload_raw("OB-1", "N5", ["C-N4-1"]))
        assert_detail(self, caught.exception, "destination")

    def test_empty_missing_non_string_codes(self) -> None:
        with self.assertRaises(ValidationError):
            build_outbound_payload(outbound_payload_raw("OB-1", "N4", []))
        with self.assertRaises(ValidationError):
            build_outbound_payload(outbound_payload_raw("OB-1", "N4", [1]))  # type: ignore[list-item]
        with self.assertRaises(ValidationError):
            build_outbound_payload(outbound_payload_raw("OB-1", "N4", ["BAD"]))

    def test_duplicate_and_limit(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            build_outbound_payload(outbound_payload_raw("OB-1", "N4", ["C-N4-1", "C-N4-1"]))
        assert_detail(self, caught.exception, "car_codes[1]")
        with self.assertRaises(ValidationError):
            build_outbound_payload(outbound_payload_raw("OB-1", "N4", [f"C-N4-{i}" for i in range(17)]))
        _code, _destination, codes = build_outbound_payload(
            outbound_payload_raw("OB-1", "N4", [f"C-N4-{i}" for i in range(16)])
        )
        self.assertEqual(len(codes), 16)


def outbound_payload_raw(code: str, destination: str, car_codes: list[object]) -> dict[str, object]:
    return {"code": code, "destination": destination, "car_codes": car_codes}


class BuildShiftAndMiscTest(unittest.TestCase):
    def test_shift_valid_and_invalid(self) -> None:
        code, dispatcher, opened = build_shift_payload(
            {"code": " shift-1 ", "dispatcher": "LIN", "opened_at": "2026-09-10T08:00:00Z"}
        )
        self.assertEqual(code, "SHIFT-1")
        self.assertEqual(dispatcher, "LIN")
        self.assertEqual(opened, "2026-09-10T08:00:00Z")
        with self.assertRaises(ValidationError):
            build_shift_payload({"code": "XX-1", "dispatcher": "LIN", "opened_at": "2026-09-10T08:00:00Z"})
        with self.assertRaises(ValidationError):
            build_shift_payload({"code": "SHIFT-1", "dispatcher": "", "opened_at": "2026-09-10T08:00:00Z"})

    def test_bad_shift_timestamp_currently_plain_value_error(self) -> None:
        with self.assertRaises(ValueError):
            build_shift_payload({"code": "SHIFT-1", "dispatcher": "LIN", "opened_at": "yesterday"})

    def test_advance_steps_defaults_and_bounds(self) -> None:
        self.assertEqual(parse_advance_steps({}), 1)
        self.assertEqual(parse_advance_steps({"steps": None}), 1)
        self.assertEqual(parse_advance_steps({"steps": 200}), 200)
        for bad in (0, 201, "2", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    parse_advance_steps({"steps": bad})

    def test_transfer_code(self) -> None:
        self.assertEqual(parse_transfer_code({"transfer_code": "x1"}), "X1")
        for bad in ("", "1X", "TOOLONGBAY", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    parse_transfer_code({"transfer_code": bad})


if __name__ == "__main__":
    unittest.main(verbosity=2)
