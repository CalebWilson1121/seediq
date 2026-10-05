from __future__ import annotations

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
