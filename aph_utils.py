from __future__ import annotations

import json
from typing import Any


def loads_json(value: Any, default=None):
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {} if default is None else default


def normalize_crop(value: Any) -> str:
    crop = str(value or "").upper().strip()
    if crop in {"SOYBEAN", "SOY"}:
        return "SOYBEANS"
    return crop


def practice_bucket(value: Any) -> str:
    raw = str(value or "").upper().replace("-", "").replace(" ", "")
    if "NIRR" in raw or "NONIRR" in raw:
        return "NIRR"
    if "IRR" in raw:
        return "IRR"
    return raw or "UNKNOWN"


def unit_number_from_metadata(value: Any) -> str:
    meta = loads_json(value, {})
    return str(meta.get("unit_number") or meta.get("unit") or "").strip()


def aph_identity(crop: Any, practice: Any, unit_number: Any) -> str:
    return f"{normalize_crop(crop)}|{practice_bucket(practice)}|{str(unit_number or '').strip()}"


def match_identity(match: dict[str, Any]) -> str:
    unit_key = str(match.get("unit_key") or "").strip()
    if unit_key.count("|") >= 2:
        return unit_key
    meta = loads_json(match.get("metadata_json") or match.get("metadata"), {})
    return aph_identity(meta.get("crop"), meta.get("practice"), meta.get("unit_number") or unit_key)


def record_identity(record: dict[str, Any]) -> str:
    return aph_identity(
        record.get("crop"),
        record.get("practice"),
        unit_number_from_metadata(record.get("metadata_json") or record.get("metadata")),
    )


def is_valid_production_record(record: dict[str, Any]) -> bool:
    """True only for real, usable APH production observations.

    SeedIQ retains excluded/placeholder rows for auditability, but they must not
    influence yield goals, stability, climate response, or seed placement.
    """
    try:
        crop_year = int(record.get("crop_year"))
    except (TypeError, ValueError):
        return False
    if crop_year < 1980 or crop_year > 2100:
        return False

    try:
        yield_value = float(record.get("yield_value"))
    except (TypeError, ValueError):
        return False
    if yield_value <= 0:
        return False

    meta = loads_json(record.get("metadata_json") or record.get("metadata"), {})
    excluded = meta.get("yield_excluded")
    if excluded is True or str(excluded).lower() == "true":
        return False

    descriptor = str(meta.get("yield_descriptor") or "").upper().strip()
    if descriptor == "Z":
        return False

    return True
