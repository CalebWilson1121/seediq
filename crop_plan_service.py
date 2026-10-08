from __future__ import annotations

import json
import re
from typing import Any

from database import connect, row_to_dict, rows_to_dicts

ROTATION = {
    "CORN": "SOYBEANS",
    "SOYBEANS": "CORN",
    "SOYBEAN": "CORN",
}


def _norm_crop(crop: str | None) -> str | None:
    if crop is None:
        return None
    value = crop.strip().upper()
    return value or None


def list_field_plans(farm_id: int, crop_year: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.id AS field_id,f.name,f.acres,f.county,f.state,f.farm_number,f.tract_number,f.field_number,"
            "f.practice,f.irrigation,f.metadata_json,cp.id AS crop_plan_id,cp.crop_year,cp.crop,cp.source,cp.rotation_mode,"
            "cp.selected_seed_product_id,cp.yield_goal,cp.target_population,cp.seed_price_per_unit,cp.seeds_per_unit,"
            "cp.units_required,cp.seed_cost_per_acre,cp.total_seed_cost,cp.pricing_source,cp.notes,cp.status,"
            "sp.product_name AS selected_seed_name,sp.brand AS selected_seed_brand,sp.trait_package AS selected_trait_package "
            "FROM fields f LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? "
            "LEFT JOIN seed_products sp ON sp.id=cp.selected_seed_product_id "
            "WHERE f.farm_id=? ORDER BY f.name,f.id",
            (crop_year, farm_id),
        ).fetchall()
    return rows_to_dicts(rows)


def set_crop(field_id: int, crop_year: int, crop: str | None, source: str = "manual") -> dict[str, Any]:
    crop = _norm_crop(crop)
    with connect() as conn:
        field = conn.execute("SELECT id FROM fields WHERE id=?", (field_id,)).fetchone()
        if not field:
            raise KeyError(f"Field {field_id} not found")
        conn.execute(
            "INSERT INTO field_crop_plans(field_id,crop_year,crop,source,status) VALUES(?,?,?,?,?) "
            "ON CONFLICT(field_id,crop_year) DO UPDATE SET crop=excluded.crop,source=excluded.source,status='planning',updated_at=CURRENT_TIMESTAMP",
            (field_id, crop_year, crop, source, "planning"),
        )
        row = conn.execute("SELECT * FROM field_crop_plans WHERE field_id=? AND crop_year=?", (field_id, crop_year)).fetchone()
    return row_to_dict(row) or {}


def rotate_field(field_id: int, from_year: int, to_year: int) -> dict[str, Any]:
    with connect() as conn:
        prior = conn.execute("SELECT crop FROM field_crop_plans WHERE field_id=? AND crop_year=?", (field_id, from_year)).fetchone()
        if prior and prior["crop"]:
            prior_crop = _norm_crop(prior["crop"])
        else:
            f = conn.execute("SELECT crop FROM fields WHERE id=?", (field_id,)).fetchone()
            if not f:
                raise KeyError(f"Field {field_id} not found")
            prior_crop = _norm_crop(f["crop"])
    next_crop = ROTATION.get(prior_crop or "")
    if not next_crop:
        raise ValueError("Rotate currently supports Corn ↔ Soybeans. Assign the crop manually for other rotations.")
    result = set_crop(field_id, to_year, next_crop, source="rotation")
    with connect() as conn:
        conn.execute("UPDATE field_crop_plans SET rotation_mode='corn_soy' WHERE field_id=? AND crop_year=?", (field_id, to_year))
    result["previous_crop"] = prior_crop
    return result


def rotate_farm(farm_id: int, from_year: int, to_year: int, field_ids: list[int] | None = None) -> dict[str, Any]:
    with connect() as conn:
        if field_ids:
            placeholders = ",".join("?" for _ in field_ids)
            rows = conn.execute(f"SELECT id FROM fields WHERE farm_id=? AND id IN ({placeholders})", (farm_id, *field_ids)).fetchall()
        else:
            rows = conn.execute("SELECT id FROM fields WHERE farm_id=?", (farm_id,)).fetchall()
    rotated, skipped = [], []
    for row in rows:
        fid = int(row["id"])
        try:
            rotated.append(rotate_field(fid, from_year, to_year))
        except ValueError as exc:
            skipped.append({"field_id": fid, "reason": str(exc)})
    return {"farm_id": farm_id, "from_year": from_year, "to_year": to_year, "rotated": len(rotated), "skipped": skipped}


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def _soi_year(raw_preview: Any, identities: list[dict[str, Any]], fallback_year: int) -> int:
    for identity in identities:
        meta = _json_obj(identity.get("metadata_json"))
        value = meta.get("source_crop_year")
        if value:
            try:
                return int(value)
            except Exception:
                pass
    text = str(raw_preview or "")
    matches = re.findall(r"\b(20\d{2})\s+Total\s+Prod", text, re.I)
    if matches:
        return max(int(x) for x in matches)
    years = re.findall(r"\b(20\d{2})\b", text)
    plausible = [int(x) for x in years if 2020 <= int(x) <= fallback_year]
    return max(plausible) if plausible else fallback_year - 1


def _identity_crop(identity: dict[str, Any]) -> str | None:
    meta = _json_obj(identity.get("metadata_json"))
    crop = _norm_crop(meta.get("source_crop"))
    if crop in {"SOYBEAN", "SOYBEANS"}:
        return "SOYBEANS"
    if crop == "CORN":
        return "CORN"
    memberships = meta.get("insurance_unit_memberships") or []
    crops = {_norm_crop(x.get("crop")) for x in memberships if isinstance(x, dict) and x.get("crop")}
    crops.discard(None)
    normalized = {"SOYBEANS" if x == "SOYBEAN" else x for x in crops}
    return next(iter(normalized)) if len(normalized) == 1 else None


def apply_soi_crop_rotation(farm_id: int, crop_year: int) -> dict[str, Any]:
    """Seed a planning year from the latest mapped SOI without overwriting manual choices.

    The SOI crop is treated as the base-year crop. Future planning years alternate
    Corn <-> Soybeans. Any field whose crop plan source is manual remains untouched.
    """
    with connect() as conn:
        doc = conn.execute(
            "SELECT id,raw_preview FROM documents WHERE farm_id=? AND upper(document_type)='SOI' "
            "ORDER BY parsed_at DESC NULLS LAST,id DESC LIMIT 1",
            (farm_id,),
        ).fetchone()
        if not doc:
            return {"farm_id": farm_id, "crop_year": crop_year, "source_year": None, "applied": 0, "manual_preserved": 0, "unmatched": [], "message": "No mapped SOI found"}

        identities = [dict(r) for r in conn.execute(
            "SELECT * FROM soi_field_identities WHERE farm_id=? AND source_document_id=? ORDER BY id",
            (farm_id, int(doc["id"])),
        ).fetchall()]
        if not identities:
            # Farms initially built directly from the SOI keep source crop in field metadata.
            fields_for_identity = [dict(r) for r in conn.execute(
                "SELECT id,name,farm_number,tract_number,field_number,metadata_json FROM fields WHERE farm_id=? ORDER BY id",
                (farm_id,),
            ).fetchall()]
            identities = [{
                "name": x.get("name"),
                "farm_number": x.get("farm_number"),
                "tract_number": x.get("tract_number"),
                "field_number": x.get("field_number"),
                "metadata_json": x.get("metadata_json"),
                "_direct_field_id": x.get("id"),
            } for x in fields_for_identity if _identity_crop(x)]

        source_year = _soi_year(doc.get("raw_preview"), identities, crop_year)
        fields = [dict(r) for r in conn.execute(
            "SELECT f.id,f.name,f.farm_number,f.tract_number,f.field_number,f.metadata_json,"
            "fl.boundary_geojson FROM fields f LEFT JOIN LATERAL ("
            " SELECT boundary_geojson FROM field_locations x WHERE x.field_id=f.id AND x.boundary_geojson IS NOT NULL "
            " ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1"
            ") fl ON true WHERE f.farm_id=? ORDER BY f.id",
            (farm_id,),
        ).fetchall()]

        by_fsa: dict[tuple[str, str, str], list[int]] = {}
        for field in fields:
            key = tuple(str(field.get(k) or "").strip() for k in ("farm_number", "tract_number", "field_number"))
            if any(key):
                by_fsa.setdefault(key, []).append(int(field["id"]))

        assignments: dict[int, tuple[str, str]] = {}
        unmatched = []
        for identity in identities:
            source_crop = _identity_crop(identity)
            if source_crop not in ROTATION:
                unmatched.append({"name": identity.get("name"), "reason": "SOI crop unavailable"})
                continue
            key = tuple(str(identity.get(k) or "").strip() for k in ("farm_number", "tract_number", "field_number"))
            field_ids = list(by_fsa.get(key, []))
            if identity.get("_direct_field_id"):
                field_ids = [int(identity["_direct_field_id"])]
            if not field_ids:
                unmatched.append({"name": identity.get("name"), "reason": "Could not match SOI identity to AcreFit field"})
                continue
            years_forward = max(0, crop_year - source_year)
            planned_crop = source_crop
            for _ in range(years_forward):
                planned_crop = ROTATION.get(planned_crop, planned_crop)
            for field_id in field_ids:
                assignments[field_id] = (planned_crop, source_crop)

        applied = 0
        manual_preserved = 0
        for field_id, (planned_crop, source_crop) in assignments.items():
            existing = conn.execute(
                "SELECT id,crop,source FROM field_crop_plans WHERE field_id=? AND crop_year=?",
                (field_id, crop_year),
            ).fetchone()
            if existing and str(existing.get("source") or "").lower() in {"manual", "seed_plan", "manual_override"}:
                manual_preserved += 1
                continue
            conn.execute(
                "INSERT INTO field_crop_plans(field_id,crop_year,crop,source,rotation_mode,status) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(field_id,crop_year) DO UPDATE SET crop=excluded.crop,source=excluded.source,"
                "rotation_mode=excluded.rotation_mode,status='planning',updated_at=CURRENT_TIMESTAMP",
                (field_id, crop_year, planned_crop, "mapped_soi_rotation", f"soi_{source_year}_{source_crop.lower()}", "planning"),
            )
            applied += 1

    return {
        "farm_id": farm_id,
        "crop_year": crop_year,
        "source_year": source_year,
        "applied": applied,
        "manual_preserved": manual_preserved,
        "unmatched": unmatched,
        "message": f"Applied mapped SOI rotation to {applied} fields; preserved {manual_preserved} manual overrides.",
    }


def select_seed(field_id: int, crop_year: int, seed_product_id: int | None, target_population: int | None = None, notes: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        existing = conn.execute("SELECT id FROM field_crop_plans WHERE field_id=? AND crop_year=?", (field_id, crop_year)).fetchone()
        if not existing:
            conn.execute("INSERT INTO field_crop_plans(field_id,crop_year,source,status) VALUES(?,?,?,?)", (field_id, crop_year, "manual", "planning"))
        conn.execute(
            "UPDATE field_crop_plans SET selected_seed_product_id=?,target_population=?,notes=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE field_id=? AND crop_year=?",
            (seed_product_id, target_population, notes, "seed_selected" if seed_product_id else "planning", field_id, crop_year),
        )
        row = conn.execute("SELECT * FROM field_crop_plans WHERE field_id=? AND crop_year=?", (field_id, crop_year)).fetchone()
    return row_to_dict(row) or {}
