from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from database import connect, rows_to_dicts

router = APIRouter()


def _loads(value: Any, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


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


@router.get("/api/fields/{field_id}/channel-fit")
def channel_fit(field_id: int, crop_year: int = 2027, limit: int = 5):
    with connect() as conn:
        row = conn.execute(
            "SELECT f.id,f.farm_id,f.name,f.acres,f.crop AS field_crop,f.irrigation,fa.organization_id,cp.crop,cp.yield_goal,cp.target_population,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json FROM fields f JOIN farms fa ON fa.id=f.farm_id LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? LEFT JOIN field_soils fs ON fs.field_id=f.id WHERE f.id=?",
            (crop_year, field_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Field not found")
        crop = (row.get("crop") or row.get("field_crop") or "").upper()
        if crop == "SOYBEAN": crop = "SOYBEANS"
        if crop not in {"CORN", "SOYBEANS"}:
            raise HTTPException(status_code=400, detail="Assign Corn or Soybeans to this field first")
        org_id = row.get("organization_id")
        products = conn.execute(
            "SELECT * FROM seed_products WHERE brand='Channel' AND active=true AND crop_year=? AND upper(crop)=? AND (organization_id=? OR ? IS NULL) ORDER BY relative_maturity,product_name",
            (crop_year, crop, org_id, org_id),
        ).fetchall()
    drainage = _loads(row.get("drainage_summary_json"), {})
    ranked = []
    for p in rows_to_dicts(products):
        meta = _loads(p.get("metadata_json"), {})
        tags = {str(x).lower() for x in meta.get("tags", [])}
        score, reasons = _fit_score(crop, row.get("irrigation"), row.get("weighted_aws150_cm"), drainage, row.get("yield_goal"), tags)
        pop = _population(crop, row.get("irrigation"), row.get("weighted_aws150_cm"), row.get("yield_goal"), tags)
        ranked.append({
            "seed_product_id": p["id"],
            "product_name": p["product_name"],
            "brand": p.get("brand"),
            "crop": crop,
            "relative_maturity": p.get("relative_maturity"),
            "trait_package": p.get("trait_package"),
            "fit_score": score,
            "recommended_population": pop,
            "placement": p.get("placement_text"),
            "reasons": reasons,
            "source_url": p.get("source_url"),
        })
    ranked.sort(key=lambda x: (-x["fit_score"], x.get("relative_maturity") or 999))
    return {
        "field_id": field_id,
        "crop_year": crop_year,
        "field": {
            "name": row.get("name"), "acres": row.get("acres"), "crop": crop,
            "irrigation": row.get("irrigation"), "yield_goal": row.get("yield_goal"),
            "awc_0_150cm": row.get("weighted_aws150_cm"), "drainage": drainage,
        },
        "ranking_method": "SeedIQ deterministic soil + irrigation + management fit; no AI/model cost",
        "population_note": "Population is a SeedIQ planning recommendation, not a Bayer/Channel prescription. Dealer/agronomist should confirm locally.",
        "recommendations": ranked[:max(1, min(limit, 20))],
    }
