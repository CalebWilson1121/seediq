from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import connect, row_to_dict, rows_to_dicts
from pricing_service import calculated_farmer_price, latest_field_price_request

router = APIRouter()


class FieldSeedPlanRequest(BaseModel):
    crop_year: int
    yield_goal: float | None = None
    target_population: int | None = None
    seed_product_id: int | None = None
    seed_price_per_unit: float | None = None
    seeds_per_unit: int | None = None
    notes: str | None = None
    pricing_source: str | None = "dealer_input"


def _default_unit_size(crop: str | None) -> int | None:
    c = (crop or "").upper()
    if c == "CORN":
        return 80000
    if c in {"SOYBEAN", "SOYBEANS"}:
        return 140000
    return None


def _economics(acres: float, population: int | None, price: float | None, seeds_per_unit: int | None) -> dict[str, float | int | None]:
    if not population or population <= 0 or not seeds_per_unit or seeds_per_unit <= 0:
        return {"units_required": None, "sold_units": None, "seed_cost_per_acre": None, "total_seed_cost": None}
    units_required = acres * population / seeds_per_unit
    sold_units = math.ceil(units_required - 1e-9)
    cost_per_acre = (population / seeds_per_unit) * price if price is not None else None
    total_cost = units_required * price if price is not None else None
    return {
        "units_required": round(units_required, 3),
        "sold_units": sold_units,
        "seed_cost_per_acre": round(cost_per_acre, 2) if cost_per_acre is not None else None,
        "total_seed_cost": round(total_cost, 2) if total_cost is not None else None,
    }


@router.put("/api/fields/{field_id}/seed-plan")
def save_field_seed_plan(field_id: int, req: FieldSeedPlanRequest):
    with connect() as conn:
        field = conn.execute("SELECT id,acres,crop,irrigation FROM fields WHERE id=?", (field_id,)).fetchone()
        if not field:
            raise HTTPException(status_code=404, detail="Field not found")
        existing = conn.execute("SELECT * FROM field_crop_plans WHERE field_id=? AND crop_year=?", (field_id, req.crop_year)).fetchone()
        crop = (existing["crop"] if existing and existing.get("crop") else field.get("crop")) if field else None
        product = None
        if req.seed_product_id is not None:
            product = conn.execute("SELECT id,product_name,crop,unit_size_seeds,population_target FROM seed_products WHERE id=?", (req.seed_product_id,)).fetchone()
            if not product:
                raise HTTPException(status_code=400, detail="Seed product not found")
        population = req.target_population or (int(product["population_target"]) if product and product.get("population_target") else None)
        seeds_per_unit = req.seeds_per_unit or (int(product["unit_size_seeds"]) if product and product.get("unit_size_seeds") else None) or _default_unit_size(crop or (product.get("crop") if product else None))

        effective_price = req.seed_price_per_unit
        effective_source = req.pricing_source
        if req.seed_product_id is not None:
            farm_row = conn.execute("SELECT farm_id FROM fields WHERE id=?", (field_id,)).fetchone()
            standard = calculated_farmer_price(int(farm_row["farm_id"]), req.crop_year, int(req.seed_product_id))
            approved_existing = bool(existing and existing.get("selected_seed_product_id") == req.seed_product_id and str(existing.get("pricing_source") or "") in {"dealer_approved_override","dealer_counter_price"})
            if approved_existing:
                effective_price = existing.get("seed_price_per_unit")
                effective_source = existing.get("pricing_source")
            elif standard.get("available"):
                standard_price = float(standard["calculated_price"])
                if req.seed_price_per_unit is not None and abs(float(req.seed_price_per_unit)-standard_price) > 0.01:
                    raise HTTPException(status_code=400, detail="Use Request Price Override for a price different from the calculated farmer price.")
                effective_price = standard_price
                effective_source = "farmer_pricing_profile"

        econ = _economics(float(field.get("acres") or 0), population, effective_price, seeds_per_unit)
        if not existing:
            conn.execute(
                "INSERT INTO field_crop_plans(field_id,crop_year,crop,source,status) VALUES(?,?,?,?,?)",
                (field_id, req.crop_year, crop, "seed_plan", "planning"),
            )
        conn.execute(
            "UPDATE field_crop_plans SET yield_goal=?,target_population=?,selected_seed_product_id=?,seed_price_per_unit=?,seeds_per_unit=?,units_required=?,seed_cost_per_acre=?,total_seed_cost=?,pricing_source=?,notes=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE field_id=? AND crop_year=?",
            (
                req.yield_goal, population, req.seed_product_id, effective_price, seeds_per_unit,
                econ["units_required"], econ["seed_cost_per_acre"], econ["total_seed_cost"], effective_source,
                req.notes, "seed_selected" if req.seed_product_id else "planning", field_id, req.crop_year,
            ),
        )
        row = conn.execute(
            "SELECT cp.*,f.name AS field_name,f.acres,f.irrigation,sp.product_name,sp.brand FROM field_crop_plans cp JOIN fields f ON f.id=cp.field_id LEFT JOIN seed_products sp ON sp.id=cp.selected_seed_product_id WHERE cp.field_id=? AND cp.crop_year=?",
            (field_id, req.crop_year),
        ).fetchone()
    result = row_to_dict(row) or {}
    result["sold_units"] = econ["sold_units"]
    if result.get("selected_seed_product_id"):
        try:
            result["standard_pricing"] = calculated_farmer_price(int(field["id"] and conn.execute("SELECT farm_id FROM fields WHERE id=?", (field_id,)).fetchone()["farm_id"]), req.crop_year, int(result["selected_seed_product_id"]))
        except Exception:
            result["standard_pricing"] = None
        result["price_request"] = latest_field_price_request(field_id, req.crop_year)
    return result


@router.get("/api/farms/{farm_id}/seed-logistics")
def farm_seed_logistics(farm_id: int, crop_year: int):
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.id AS field_id,f.name AS field_name,f.acres,f.irrigation,cp.crop,cp.yield_goal,cp.target_population,cp.selected_seed_product_id,cp.seed_price_per_unit,cp.seeds_per_unit,cp.units_required,cp.seed_cost_per_acre,cp.total_seed_cost,sp.product_name,sp.brand,sp.trait_package FROM fields f LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? LEFT JOIN seed_products sp ON sp.id=cp.selected_seed_product_id WHERE f.farm_id=? ORDER BY f.name",
            (crop_year, farm_id),
        ).fetchall()
    fields = rows_to_dicts(rows)
    grouped: dict[str, dict[str, Any]] = {}
    for row in fields:
        product_id = row.get("selected_seed_product_id")
        if not product_id or not row.get("target_population"):
            continue
        key = str(product_id)
        g = grouped.setdefault(key, {
            "seed_product_id": product_id,
            "product_name": row.get("product_name"),
            "brand": row.get("brand"),
            "trait_package": row.get("trait_package"),
            "acres": 0.0,
            "seed_count": 0.0,
            "unit_size": int(row.get("seeds_per_unit") or _default_unit_size(row.get("crop")) or 0),
            "estimated_value": 0.0,
            "field_count": 0,
        })
        acres = float(row.get("acres") or 0)
        pop = int(row.get("target_population") or 0)
        g["acres"] += acres
        g["seed_count"] += acres * pop
        g["estimated_value"] += float(row.get("total_seed_cost") or 0)
        g["field_count"] += 1
    order_lines = []
    for g in grouped.values():
        units = g["seed_count"] / g["unit_size"] if g["unit_size"] else 0
        order_lines.append({
            **g,
            "acres": round(g["acres"], 2),
            "units_required": round(units, 3),
            "sold_units": math.ceil(units - 1e-9),
            "estimated_value": round(g["estimated_value"], 2),
        })
    return {
        "farm_id": farm_id,
        "crop_year": crop_year,
        "fields": fields,
        "order_lines": sorted(order_lines, key=lambda x: (x.get("brand") or "", x.get("product_name") or "")),
        "totals": {
            "planned_acres": round(sum(float(x.get("acres") or 0) for x in fields if x.get("selected_seed_product_id")), 2),
            "sold_units": sum(int(x["sold_units"]) for x in order_lines),
            "estimated_seed_value": round(sum(float(x["estimated_value"]) for x in order_lines), 2),
        },
    }
