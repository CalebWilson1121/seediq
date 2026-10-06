from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import connect, rows_to_dicts

router = APIRouter()


class AutoPlanRequest(BaseModel):
    crop_year: int = 2027
    overwrite_existing: bool = False


def _loads(value: Any, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def _normalize_crop(value: str | None) -> str:
    crop = (value or "").upper()
    return "SOYBEANS" if crop == "SOYBEAN" else crop


def _population(crop: str, irrigation: str | None, awc: float | None, yield_goal: float | None, tags: set[str]) -> int:
    crop = (crop or "").upper()
    irr = (irrigation or "").upper()
    if crop == "CORN":
        pop = 34000 if irr == "IRR" else 29000
        if awc is not None:
            if awc < 18: pop -= 2000
            elif awc >= 25: pop += 1000
        if yield_goal is not None:
            if yield_goal >= 220: pop += 1000
            elif yield_goal <= 160: pop -= 1000
        if "higher_population" in tags: pop += 1500
        if "medium_population" in tags: pop = min(pop, 32000)
        if "lower_population" in tags: pop -= 1500
        return int(max(24000, min(36000, round(pop / 500) * 500)))
    pop = 140000
    if irr == "NIRR": pop -= 5000
    if "no_till" in tags: pop += 10000
    if awc is not None and awc < 18: pop += 5000
    return int(max(120000, min(165000, round(pop / 5000) * 5000)))


def _maturity_window(crop: str, latitude: float | None) -> dict[str, float] | None:
    """Conservative geographic maturity gate derived from field latitude.

    This intentionally uses a broad planning window. It is a hard eligibility
    gate for Top 5 ranking, not a substitute for local dealer/agronomist review.
    """
    if latitude is None:
        return None
    lat = float(latitude)
    crop = _normalize_crop(crop)
    if crop == "SOYBEANS":
        # Approximate U.S. maturity-zone progression: later groups moving south.
        target = 9.8 - (0.16 * lat)
        low = max(0.0, target - 0.8)
        high = min(8.0, target + 0.8)
        return {"target": round(target, 1), "min": round(low, 1), "max": round(high, 1)}
    if crop == "CORN":
        # Broad relative-maturity planning band by latitude.
        target = 190.0 - (2.0 * lat)
        low = max(65.0, target - 10.0)
        high = min(125.0, target + 8.0)
        return {"target": round(target, 0), "min": round(low, 0), "max": round(high, 0)}
    return None


def _location_eligibility(crop: str, maturity: float | None, latitude: float | None) -> tuple[bool, str | None, dict[str, float] | None]:
    window = _maturity_window(crop, latitude)
    if window is None:
        return True, "Field location unavailable; maturity-zone gate not applied", None
    if maturity is None:
        return False, "Relative maturity unavailable", window
    rm = float(maturity)
    if rm < window["min"] or rm > window["max"]:
        return False, f"Outside local maturity window ({window['min']}–{window['max']} RM)", window
    return True, f"Inside local maturity window ({window['min']}–{window['max']} RM)", window


def _fit_score(crop: str, irrigation: str | None, awc: float | None, drainage: dict[str, Any], yield_goal: float | None, tags: set[str]) -> tuple[float, list[str]]:
    score = 55.0
    reasons: list[str] = []
    irr = (irrigation or "").upper()
    crop = (crop or "").upper()
    if irr == "IRR":
        if "irrigated" in tags: score += 14; reasons.append("public Channel positioning calls out irrigation response")
        if "high_management" in tags: score += 9; reasons.append("fits an irrigated/high-management environment")
        if "high_yield" in tags: score += 7; reasons.append("strong top-end yield positioning")
    elif irr == "NIRR":
        if "drought" in tags or "stress" in tags: score += 13; reasons.append("stress/drought positioning fits non-irrigated acres")
        if "flex" in tags or "semi_flex" in tags or "lower_population" in tags: score += 7; reasons.append("ear-flex/population positioning adds dryland flexibility")
    if awc is not None:
        if awc < 18 and ("drought" in tags or "stress" in tags): score += 10; reasons.append("low AWC increases value of drought/stress tolerance")
        if awc >= 25 and ("high_yield" in tags or "high_management" in tags): score += 8; reasons.append("higher water-holding capacity supports yield/management response")
        if 18 <= awc < 25 and "broad_acre" in tags: score += 5; reasons.append("broad-acre positioning fits moderate water-holding capacity")
    drainage_text = " ".join(str(k).lower() for k in (drainage or {}).keys())
    if any(x in drainage_text for x in ("poor", "somewhat poor", "very poor")):
        if crop.startswith("SOY") and "phytophthora" in tags: score += 12; reasons.append("Phytophthora protection is useful on wetter/poorly drained soil")
        if crop == "CORN" and "root_strength" in tags: score += 6; reasons.append("root strength helps on challenging drainage")
    if yield_goal is not None:
        high_goal = yield_goal >= (200 if crop == "CORN" else 60)
        if high_goal and "high_yield" in tags: score += 8; reasons.append("product yield positioning matches the field yield goal")
        if high_goal and "high_management" in tags: score += 5
    if crop.startswith("SOY") and "standability" in tags: score += 5; reasons.append("strong standability supports harvestability")
    return round(max(0, min(99, score)), 1), reasons[:5]


def _rank_products(row: dict[str, Any], products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    crop = _normalize_crop(row.get("crop") or row.get("field_crop"))
    drainage = _loads(row.get("drainage_summary_json"), {})
    latitude = row.get("centroid_lat")
    ranked: list[dict[str, Any]] = []
    for p in products:
        meta = _loads(p.get("metadata_json"), {})
        tags = {str(x).lower() for x in meta.get("tags", [])}
        eligible, location_reason, maturity_window = _location_eligibility(
            crop, p.get("relative_maturity"), latitude
        )
        score, reasons = _fit_score(
            crop,
            row.get("irrigation"),
            row.get("weighted_aws150_cm"),
            drainage,
            row.get("yield_goal"),
            tags,
        )
        if location_reason:
            reasons = [location_reason] + reasons
        pop = _population(
            crop,
            row.get("irrigation"),
            row.get("weighted_aws150_cm"),
            row.get("yield_goal"),
            tags,
        )
        ranked.append({
            "seed_product_id": p["id"],
            "product_name": p["product_name"],
            "brand": p.get("brand"),
            "crop": crop,
            "relative_maturity": p.get("relative_maturity"),
            "trait_package": p.get("trait_package"),
            "fit_score": score,
            "location_eligible": eligible,
            "location_reason": location_reason,
            "maturity_window": maturity_window,
            "recommended_population": pop,
            "unit_size_seeds": p.get("unit_size_seeds"),
            "placement": p.get("placement_text"),
            "reasons": reasons[:6],
            "source_url": p.get("source_url"),
        })
    ranked.sort(
        key=lambda x: (
            0 if x["location_eligible"] else 1,
            -x["fit_score"],
            abs((x.get("relative_maturity") or 999) - ((x.get("maturity_window") or {}).get("target") or 999)),
            x.get("product_name") or "",
        )
    )
    return ranked


@router.get("/api/fields/{field_id}/channel-fit")
def channel_fit(field_id: int, crop_year: int = 2027, limit: int = 5):
    with connect() as conn:
        row = conn.execute(
            "SELECT f.id,f.farm_id,f.name,f.acres,f.crop AS field_crop,f.irrigation,fa.organization_id,cp.crop,cp.yield_goal,cp.target_population,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json,fl.centroid_lat,fl.centroid_lon FROM fields f JOIN farms fa ON fa.id=f.farm_id LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? LEFT JOIN field_soils fs ON fs.field_id=f.id LEFT JOIN LATERAL (SELECT centroid_lat,centroid_lon FROM field_locations x WHERE x.field_id=f.id ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) fl ON true WHERE f.id=?",
            (crop_year, field_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Field not found")
        crop = _normalize_crop(row.get("crop") or row.get("field_crop"))
        if crop not in {"CORN", "SOYBEANS"}:
            raise HTTPException(status_code=400, detail="Assign Corn or Soybeans to this field first")
        org_id = row.get("organization_id")
        products = rows_to_dicts(conn.execute(
            "SELECT * FROM seed_products WHERE brand='Channel' AND active=true AND crop_year=? AND upper(crop)=? AND (organization_id=? OR ? IS NULL) ORDER BY relative_maturity,product_name",
            (crop_year, crop, org_id, org_id),
        ).fetchall())
    ranked = _rank_products(dict(row), products)
    eligible_ranked = [x for x in ranked if x.get("location_eligible")]
    drainage = _loads(row.get("drainage_summary_json"), {})
    maturity_window = _maturity_window(crop, row.get("centroid_lat"))
    return {
        "field_id": field_id,
        "crop_year": crop_year,
        "field": {
            "name": row.get("name"), "acres": row.get("acres"), "crop": crop,
            "irrigation": row.get("irrigation"), "yield_goal": row.get("yield_goal"),
            "awc_0_150cm": row.get("weighted_aws150_cm"), "drainage": drainage,
            "centroid_lat": row.get("centroid_lat"), "centroid_lon": row.get("centroid_lon"),
            "maturity_window": maturity_window,
        },
        "ranking_method": "SeedIQ hard location/maturity gate first, then deterministic soil + irrigation + management fit; no AI/model cost",
        "population_note": "Population is a SeedIQ planning recommendation, not a Bayer/Channel prescription. Dealer/agronomist should confirm locally.",
        "catalog_product_count": len(ranked),
        "location_eligible_count": len(eligible_ranked),
        "recommendations": eligible_ranked[:max(1, min(limit, 5))],
        "all_options": ranked,
    }


@router.post("/api/farms/{farm_id}/channel-auto-plan")
def channel_auto_plan(farm_id: int, req: AutoPlanRequest):
    with connect() as conn:
        farm = conn.execute("SELECT id,organization_id FROM farms WHERE id=?", (farm_id,)).fetchone()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        rows = rows_to_dicts(conn.execute(
            "SELECT f.id AS field_id,f.name,f.acres,f.crop AS field_crop,f.irrigation,cp.id AS crop_plan_id,cp.crop,cp.yield_goal,cp.target_population,cp.selected_seed_product_id,cp.seed_price_per_unit,cp.seeds_per_unit,cp.pricing_source,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json,fl.centroid_lat,fl.centroid_lon "
            "FROM fields f LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? LEFT JOIN field_soils fs ON fs.field_id=f.id LEFT JOIN LATERAL (SELECT centroid_lat,centroid_lon FROM field_locations x WHERE x.field_id=f.id ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) fl ON true WHERE f.farm_id=? ORDER BY f.name",
            (req.crop_year, farm_id),
        ).fetchall())
        all_products = rows_to_dicts(conn.execute(
            "SELECT * FROM seed_products WHERE brand='Channel' AND active=true AND crop_year=? AND (organization_id=? OR ? IS NULL) ORDER BY crop,relative_maturity,product_name",
            (req.crop_year, farm.get("organization_id"), farm.get("organization_id")),
        ).fetchall())
        products_by_crop: dict[str, list[dict[str, Any]]] = {"CORN": [], "SOYBEANS": []}
        for p in all_products:
            c = _normalize_crop(p.get("crop"))
            if c in products_by_crop:
                products_by_crop[c].append(p)

        assignments: list[dict[str, Any]] = []
        skipped_existing = 0
        skipped_unassigned = 0
        skipped_no_products = 0
        for row in rows:
            crop = _normalize_crop(row.get("crop") or row.get("field_crop"))
            if crop not in {"CORN", "SOYBEANS"}:
                skipped_unassigned += 1
                continue
            if row.get("selected_seed_product_id") and not req.overwrite_existing:
                skipped_existing += 1
                continue
            ranked = _rank_products(row, products_by_crop.get(crop, []))
            eligible_ranked = [x for x in ranked if x.get("location_eligible")]
            if not eligible_ranked:
                skipped_no_products += 1
                continue
            top = eligible_ranked[0]
            population = int(top["recommended_population"])
            unit_size = int(top.get("unit_size_seeds") or (80000 if crop == "CORN" else 140000))
            acres = float(row.get("acres") or 0)
            units_required = acres * population / unit_size if unit_size else None
            price = row.get("seed_price_per_unit")
            cost_per_acre = (population / unit_size) * float(price) if price is not None and unit_size else None
            total_cost = units_required * float(price) if price is not None and units_required is not None else None
            if not row.get("crop_plan_id"):
                conn.execute(
                    "INSERT INTO field_crop_plans(field_id,crop_year,crop,source,status) VALUES(?,?,?,?,?)",
                    (row["field_id"], req.crop_year, crop, "channel_auto_plan", "planning"),
                )
            conn.execute(
                "UPDATE field_crop_plans SET crop=?,selected_seed_product_id=?,target_population=?,seeds_per_unit=?,units_required=?,seed_cost_per_acre=?,total_seed_cost=?,status='seed_selected',updated_at=CURRENT_TIMESTAMP WHERE field_id=? AND crop_year=?",
                (crop, top["seed_product_id"], population, unit_size, round(units_required, 3) if units_required is not None else None,
                 round(cost_per_acre, 2) if cost_per_acre is not None else None,
                 round(total_cost, 2) if total_cost is not None else None,
                 row["field_id"], req.crop_year),
            )
            assignments.append({
                "field_id": row["field_id"],
                "field_name": row.get("name"),
                "crop": crop,
                "product_id": top["seed_product_id"],
                "product_name": top["product_name"],
                "fit_score": top["fit_score"],
                "population": population,
                "yield_goal": row.get("yield_goal"),
            })
    return {
        "farm_id": farm_id,
        "crop_year": req.crop_year,
        "overwrite_existing": req.overwrite_existing,
        "planned_fields": len(assignments),
        "skipped_existing": skipped_existing,
        "skipped_unassigned": skipped_unassigned,
        "skipped_no_products": skipped_no_products,
        "assignments": assignments,
        "method": "Hard field-location maturity gate first; then Channel fit using SSURGO AWC/drainage + IRR/NIRR + existing yield goal. Products outside the local maturity window stay in the catalog but cannot be auto-selected. Existing dealer price is preserved. Yield goals are never invented by auto-plan.",
    }
