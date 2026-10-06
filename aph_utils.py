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


def collapse_production_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return at most one valid production observation per field/crop/year.

    If multiple source units legitimately contribute to one mapped management
    field in the same crop year, combine them using planted-acre weighted yield
    when acreage is available. Invalid/excluded/placeholder rows are ignored.
    """
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for record in records:
        if not is_valid_production_record(record):
            continue
        year = int(record["crop_year"])
        crop = normalize_crop(record.get("crop"))
        groups.setdefault((year, crop), []).append(record)

    collapsed: list[dict[str, Any]] = []
    for (year, crop), rows in sorted(groups.items()):
        base = dict(rows[-1])
        weights = []
        for row in rows:
            try:
                acres = float(row.get("planted_acres") or 0)
            except (TypeError, ValueError):
                acres = 0.0
            try:
                yld = float(row.get("yield_value"))
            except (TypeError, ValueError):
                continue
            weights.append((max(acres, 0.0), yld))

        weighted_den = sum(a for a, _ in weights if a > 0)
        if weighted_den > 0:
            combined_yield = sum(a * y for a, y in weights if a > 0) / weighted_den
            combined_acres = weighted_den
        else:
            ys = [y for _, y in weights]
            combined_yield = sum(ys) / len(ys)
            combined_acres = None

        approved = []
        for row in rows:
            try:
                value = float(row.get("approved_yield"))
            except (TypeError, ValueError):
                continue
            try:
                acres = float(row.get("planted_acres") or 0)
            except (TypeError, ValueError):
                acres = 0.0
            approved.append((max(acres, 0.0), value))
        approved_yield = None
        if approved:
            aden = sum(a for a, _ in approved if a > 0)
            approved_yield = (
                sum(a * y for a, y in approved if a > 0) / aden
                if aden > 0 else sum(y for _, y in approved) / len(approved)
            )

        base["crop_year"] = year
        base["crop"] = crop
        base["yield_value"] = round(combined_yield, 4)
        if combined_acres is not None:
            base["planted_acres"] = round(combined_acres, 4)
        if approved_yield is not None:
            base["approved_yield"] = round(approved_yield, 4)
        base["_source_record_count"] = len(rows)
        collapsed.append(base)
    return collapsed
