"""Payload validation for public service boundaries."""

from __future__ import annotations

import re
from typing import Any

from .car import CarInput, FreightCar
from .enums import CarKind, TrackPurpose, TrackState
from .errors import ValidationError
from .forecast import ProspectiveTrain
from .intake import IntakeTrain
from .outbound import OutboundTrain
from .rules import (
    MAX_CAR_LENGTH_M,
    MAX_PLANNED_CARS,
    MAX_TRAIN_CONSIST,
    MIN_CAR_LENGTH_M,
    destination_known,
    hazard_known,
    is_car_code,
    is_entity_code,
    kind_known,
    normalize_destination,
)
from .timeutil import normalize_iso


def require_object(raw: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError(f"{field_name} must be an object", **{field_name: ["expected an object"]})
    return dict(raw)


def require_text(raw: Any, field_name: str, max_length: int = 40) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{field_name} is required", **{field_name: ["is required"]})
    value = raw.strip()
    if len(value) > max_length:
        raise ValidationError(
            f"{field_name} is too long",
            **{field_name: [f"must be at most {max_length} characters"]},
        )
    return value


def require_boolean(raw: Any, field_name: str, default: bool = False) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ValidationError(f"{field_name} must be a boolean", **{field_name: ["must be true or false"]})
    return raw


def require_integer(raw: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError(
            f"{field_name} must be an integer",
            **{field_name: [f"must be between {minimum} and {maximum}"]},
        )
    if raw < minimum or raw > maximum:
        raise ValidationError(
            f"{field_name} is out of range",
            **{field_name: [f"must be between {minimum} and {maximum}"]},
        )
    return raw


def parse_car_input(raw: dict[str, Any]) -> CarInput:
    code = require_text(raw.get("code"), "code").upper()
    if not is_car_code(code):
        raise ValidationError("invalid car code", **{"code": ["expected format C-PREFIX-NUMBER"]})
    kind_text = require_text(raw.get("kind"), "kind").upper()
    if not kind_known(kind_text):
        raise ValidationError("unknown car kind", **{"kind": ["BOX, HOPPER, FLAT, TANK, or REEFER"]})
    destination_text = require_text(raw.get("destination"), "destination").upper()
    if not destination_known(destination_text):
        raise ValidationError("unknown destination", **{"destination": ["N4, E7, S2, or W9"]})
    length = require_integer(raw.get("length_m"), "length_m", MIN_CAR_LENGTH_M, MAX_CAR_LENGTH_M)
    danger_raw = raw.get("danger_class")
    danger = "NONE" if danger_raw in (None, "") else require_text(danger_raw, "danger_class", 8).upper()
    if not hazard_known(danger):
        raise ValidationError("unknown hazard class", **{"danger_class": ["NONE, D1, or D2"]})
    loaded = require_boolean(raw.get("loaded"), "loaded", False)
    note = str(raw.get("note") or "").strip()[:200]
    return CarInput(
        code=code,
        kind=kind_text,
        destination=destination_text,
        loaded=loaded,
        length_m=length,
        danger_class=danger,
        note=note,
    )


def build_intake_payload(raw: Any) -> tuple[IntakeTrain, list[CarInput]]:
    body = require_object(raw, "payload")
    code = require_text(body.get("code"), "code").upper()
    if not is_entity_code(code, "INT"):
        raise ValidationError("invalid intake code", **{"code": ["expected prefix INT-"]})
    route = require_text(body.get("route"), "route", 30).upper()
    arrival = normalize_iso(require_text(body.get("arrival_at"), "arrival_at", 40))
    cars_raw = body.get("cars")
    if not isinstance(cars_raw, list) or not cars_raw:
        raise ValidationError("at least one car is required", **{"cars": ["must not be empty"]})
    if len(cars_raw) > MAX_TRAIN_CONSIST:
        raise ValidationError(
            "too many cars",
            **{"cars": [f"at most {MAX_TRAIN_CONSIST} cars per intake"]},
        )
    seen: set[str] = set()
    inputs: list[CarInput] = []
    for index, item in enumerate(cars_raw):
        try:
            car_input = parse_car_input(require_object(item, f"cars[{index}]"))
        except ValidationError as exc:
            prefixed = {f"cars[{index}].{key}": value for key, value in exc.fields.items()}
            raise ValidationError(exc.message, fields=prefixed) from exc
        if car_input.code in seen:
            raise ValidationError(
                "duplicate car code in consist",
                **{f"cars[{index}].code": ["appears more than once"]},
            )
        seen.add(car_input.code)
        inputs.append(car_input)
    train = IntakeTrain(
        code=code,
        route=route,
        arrival_at=arrival,
        consist=[item.code for item in inputs],
    )
    return train, inputs


def build_outbound_payload(raw: Any) -> tuple[str, str, list[str]]:
    body = require_object(raw, "payload")
    code = require_text(body.get("code"), "code").upper()
    if not is_entity_code(code, "OB"):
        raise ValidationError("invalid outbound code", **{"code": ["expected prefix OB-"]})
    destination = normalize_destination(require_text(body.get("destination"), "destination"))
    if not destination_known(destination):
        raise ValidationError("unknown destination", **{"destination": ["N4, E7, S2, or W9"]})
    car_codes_raw = body.get("car_codes")
    if not isinstance(car_codes_raw, list) or not car_codes_raw:
        raise ValidationError("at least one planned car is required", **{"car_codes": ["must not be empty"]})
    if len(car_codes_raw) > MAX_PLANNED_CARS:
        raise ValidationError(
            "too many planned cars",
            **{"car_codes": [f"at most {MAX_PLANNED_CARS} cars"]},
        )
    codes: list[str] = []
    for index, item in enumerate(car_codes_raw):
        if not isinstance(item, str) or not is_car_code(item):
            raise ValidationError(
                "invalid car code",
                **{f"car_codes[{index}]": ["expected format C-PREFIX-NUMBER"]},
            )
        value = item.strip().upper()
        if value in codes:
            raise ValidationError(
                "duplicate planned car",
                **{f"car_codes[{index}]": ["appears more than once"]},
            )
        codes.append(value)
    return code, destination, codes


def build_shift_payload(raw: Any) -> tuple[str, str, str]:
    body = require_object(raw, "payload")
    code = require_text(body.get("code"), "code").upper()
    if not is_entity_code(code, "SHIFT"):
        raise ValidationError("invalid shift code", **{"code": ["expected prefix SHIFT-"]})
    dispatcher = require_text(body.get("dispatcher"), "dispatcher", 30)
    opened = normalize_iso(require_text(body.get("opened_at"), "opened_at", 40))
    return code, dispatcher, opened


def parse_advance_steps(raw: Any) -> int:
    body = require_object(raw, "payload")
    steps = body.get("steps", 1)
    if steps is None:
        return 1
    return require_integer(steps, "steps", 1, 200)


def parse_transfer_code(raw: Any) -> str:
    body = require_object(raw, "payload")
    transfer = require_text(body.get("transfer_code"), "transfer_code").upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,8}", transfer):
        raise ValidationError("invalid transfer code", **{"transfer_code": ["expected short bay code"]})
    return transfer


def _parse_prospective_train(raw: Any, index: int, seen_codes: set[str]) -> ProspectiveTrain:
    body = require_object(raw, f"trains[{index}]")
    code = require_text(body.get("code"), f"trains[{index}].code").upper()
    if not (is_entity_code(code, "FCST") or is_entity_code(code, "INT")):
        raise ValidationError(
            "invalid prospective train code",
            **{f"trains[{index}].code": ["expected prefix FCST- or INT-"]},
        )
    route = require_text(body.get("route"), f"trains[{index}].route", 30).upper()
    arrival = normalize_iso(require_text(body.get("arrival_at"), f"trains[{index}].arrival_at", 40))
    cars_raw = body.get("cars")
    if not isinstance(cars_raw, list) or not cars_raw:
        raise ValidationError(
            "at least one car is required",
            **{f"trains[{index}].cars": ["must not be empty"]},
        )
    if len(cars_raw) > MAX_TRAIN_CONSIST:
        raise ValidationError(
            "too many cars",
            **{f"trains[{index}].cars": [f"at most {MAX_TRAIN_CONSIST} cars per intake"]},
        )
    car_inputs: list[CarInput] = []
    for car_index, item in enumerate(cars_raw):
        try:
            car_input = parse_car_input(require_object(item, f"trains[{index}].cars[{car_index}]"))
        except ValidationError as exc:
            prefixed = {
                f"trains[{index}].cars[{car_index}].{key}": value
                for key, value in exc.fields.items()
            }
            raise ValidationError(exc.message, fields=prefixed) from exc
        if car_input.code in seen_codes:
            raise ValidationError(
                "duplicate car code in forecast request",
                **{f"trains[{index}].cars[{car_index}].code": ["appears more than once"]},
            )
        seen_codes.add(car_input.code)
        car_inputs.append(car_input)
    return ProspectiveTrain(code=code, route=route, arrival_at=arrival, cars=car_inputs)


def build_forecast_payload(raw: Any) -> tuple[list[ProspectiveTrain], str | None]:
    body = require_object(raw, "payload")
    trains_raw = body.get("trains")
    if not isinstance(trains_raw, list) or not trains_raw:
        raise ValidationError("at least one prospective train is required", **{"trains": ["must not be empty"]})
    if len(trains_raw) > 10:
        raise ValidationError("too many prospective trains", **{"trains": ["at most 10 trains per forecast"]})
    seen_train_codes: set[str] = set()
    seen_car_codes: set[str] = set()
    trains: list[ProspectiveTrain] = []
    for index, item in enumerate(trains_raw):
        train = _parse_prospective_train(item, index, seen_car_codes)
        if train.code in seen_train_codes:
            raise ValidationError(
                "duplicate prospective train code",
                **{f"trains[{index}].code": ["appears more than once"]},
            )
        seen_train_codes.add(train.code)
        trains.append(train)
    horizon_raw = body.get("shift_horizon_at")
    horizon = None
    if horizon_raw not in (None, ""):
        horizon = normalize_iso(require_text(horizon_raw, "shift_horizon_at", 40))
    return trains, horizon


def build_track_arrangement_payload(raw: Any) -> tuple[str, str | None, str | None, str]:
    body = require_object(raw, "payload")
    code = require_text(body.get("code"), "code", 24).upper()
    if not is_entity_code(code, "TRK") and not re.fullmatch(r"[A-Z][A-Z0-9_-]{1,23}", code):
        raise ValidationError(
            "invalid track code",
            **{"code": ["expected an existing yard track code"]},
        )
    state_raw = body.get("state")
    state = None
    if state_raw not in (None, ""):
        state = require_text(state_raw, "state", 16).upper()
        if state not in {item.value for item in TrackState}:
            raise ValidationError(
                "invalid track state",
                **{"state": ["OPERATIONAL, RESTRICTED, or MAINTENANCE"]},
            )
    purpose_raw = body.get("purpose")
    purpose = None
    if purpose_raw not in (None, ""):
        purpose = require_text(purpose_raw, "purpose", 16).upper()
        if purpose not in {TrackPurpose.GENERAL.value, TrackPurpose.TRANSFER.value}:
            raise ValidationError(
                "invalid track purpose",
                **{"purpose": ["GENERAL or TRANSFER (destination tracks cannot be reassigned)"]},
            )
    if state is None and purpose is None:
        raise ValidationError(
            "nothing to change",
            **{"state": ["provide state and/or purpose"]},
        )
    note = str(body.get("note") or "").strip()[:200]
    return code, state, purpose, note  # type: ignore[return-value]


__all__ = [
    "build_forecast_payload",
    "build_intake_payload",
    "build_outbound_payload",
    "build_shift_payload",
    "build_track_arrangement_payload",
    "parse_advance_steps",
    "parse_car_input",
    "parse_transfer_code",
    "require_integer",
    "require_object",
    "require_text",
]
