from __future__ import annotations

import json
from statistics import mean, median
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


def _characteristic_map(meta: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for group in meta.get("characteristics") or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("characteristicId") or "").upper().strip()
            name = str(item.get("characteristicName") or "").upper().strip()
            value = item.get("value")
            if value in (None, ""):
                continue
            if cid:
                out[cid] = str(value).strip()
            if name:
                out[name] = str(value).strip()
    return out


def _numeric_rating(chars: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        value = chars.get(key.upper())
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _quality_points(rating: float | None, max_points: float) -> float:
    # Bayer numeric agronomic scales in the synced Channel feed use lower numbers
    # for stronger performance. Keep this conservative and capped.
    if rating is None:
        return 0.0
    rating = max(1.0, min(9.0, float(rating)))
    return max(0.0, max_points * ((9.0 - rating) / 8.0))


def _bayer_trait_fit(
    crop: str,
    meta: dict[str, Any],
    awc: float | None,
    drainage: dict[str, Any],
    slope: float | None,
    maturity: float | None,
    maturity_window: dict[str, float] | None,
) -> tuple[float, list[str]]:
    crop = _normalize_crop(crop)
    chars = _characteristic_map(meta)
    points = 0.0
    reasons: list[str] = []

    # Within the hard maturity gate, reward products near the field's local
    # maturity target without making maturity the entire recommendation.
    if maturity is not None and maturity_window is not None:
        target = maturity_window["target"]
        half_span = max(0.4, (maturity_window["max"] - maturity_window["min"]) / 2.0)
        distance = abs(float(maturity) - target)
        maturity_points = max(0.0, 10.0 * (1.0 - distance / half_span))
        points += maturity_points
        if maturity_points >= 6:
            reasons.append(f"RM {float(maturity):g} is close to the local {target:g} RM target")

    drainage_text = " ".join(str(k).lower() for k in (drainage or {}).keys())
    wet = any(x in drainage_text for x in ("poor", "somewhat poor", "very poor"))
    well = any(x in drainage_text for x in ("well drained", "moderately well drained", "excessively drained"))

    if crop == "SOYBEANS":
        stand = _numeric_rating(chars, "STANDABILITY_USCB", "STANDABILITY")
        emerge = _numeric_rating(chars, "EMERGENCE_USCB", "EMERGENCE")
        stand_pts = _quality_points(stand, 8.0)
        emerge_pts = _quality_points(emerge, 4.0)
        points += stand_pts + emerge_pts
        if stand is not None and stand <= 3:
            reasons.append(f"Bayer standability rating {stand:g} supports field fit")
        if emerge is not None and emerge <= 3:
            reasons.append(f"Bayer emergence rating {emerge:g} supports establishment")

        if wet:
            prr = _numeric_rating(chars, "PRR_TOLERANCE_USCB", "PRR FIELD TOLERANCE")
            prr_pts = _quality_points(prr, 12.0)
            gene = chars.get("PRR_GENE_USCB") or chars.get("PRR GENE")
            points += prr_pts
            if gene and gene.lower() not in {"susc", "susceptible", "-"}:
                points += 4.0
                reasons.append(f"PRR gene {gene} adds protection on wetter ground")
            if prr is not None and prr <= 4:
                reasons.append(f"Bayer PRR field tolerance rating {prr:g} fits drainage risk")
        elif well:
            # Avoid over-weighting wet-soil disease packages on well-drained fields.
            points += min(2.0, stand_pts * 0.25)

    elif crop == "CORN":
        drought = _numeric_rating(chars, "DROUGHT_TOLERANCE_USCB", "DROUGHT TOLERANCE")
        root = _numeric_rating(chars, "ROOT_STRENGTH_USCB", "ROOT STRENGTH")
        stalk = _numeric_rating(chars, "STALK_STRENGTH_USCB", "STALK STRENGTH")
        emerge = _numeric_rating(chars, "EMERGENCE_USCB", "EMERGENCE")

        if awc is not None and awc < 18:
            pts = _quality_points(drought, 14.0)
            points += pts
            if drought is not None and drought <= 4:
                reasons.append(f"Bayer drought rating {drought:g} fits lower-AWC soil")
        elif awc is not None and awc < 24:
            points += _quality_points(drought, 7.0)

        root_weight = 8.0 if (slope or 0) >= 4 or wet else 5.0
        root_pts = _quality_points(root, root_weight)
        stalk_pts = _quality_points(stalk, 5.0)
        emerge_pts = _quality_points(emerge, 3.0)
        points += root_pts + stalk_pts + emerge_pts
        if root is not None and root <= 3 and root_weight >= 8:
            reasons.append(f"Bayer root strength rating {root:g} fits slope/drainage pressure")
        if stalk is not None and stalk <= 3:
            reasons.append(f"Bayer stalk strength rating {stalk:g} supports harvestability")

    return round(points, 1), reasons[:4]


def _management_fit(
    crop: str,
    meta: dict[str, Any],
    tillage: str | None,
    row_spacing: str | None,
    planting_window: str | None,
) -> tuple[float, list[str]]:
    chars = _characteristic_map(meta)
    tillage = (tillage or "").upper()
    row_spacing = (row_spacing or "NORMAL").upper()
    planting_window = (planting_window or "NORMAL").upper()
    points = 0.0
    reasons: list[str] = []

    no_till = _numeric_rating(chars, "NO_TILL_ADAPTABILITY_USCB", "NO-TILL ADAPTABILITY")
    if tillage == "NO_TILL":
        pts = _quality_points(no_till, 12.0)
        points += pts
        if no_till is not None:
            reasons.append(f"Bayer no-till adaptability rating {no_till:g} matches farm tillage")
    elif tillage in {"STRIP_TILL", "MIN_TILL"}:
        points += _quality_points(no_till, 5.0)

    narrow = _numeric_rating(chars, "NARROW_ROW_USCB", "NARROW ROW")
    if row_spacing in {"15_IN", "20_IN", "TWIN_ROW"}:
        pts = _quality_points(narrow, 8.0)
        points += pts
        if narrow is not None:
            reasons.append(f"Bayer narrow-row rating {narrow:g} matches {row_spacing.replace('_IN',' in').replace('_',' ').title()}")

    if planting_window == "EARLY":
        emergence = _numeric_rating(chars, "EMERGENCE_USCB", "EMERGENCE")
        pts = _quality_points(emergence, 5.0)
        points += pts
        if emergence is not None and emergence <= 3:
            reasons.append(f"Bayer emergence rating {emergence:g} supports an early planting window")

    return round(points, 1), reasons[:3]



def _production_context(field_id: int, crop: str) -> dict[str, Any]:
    crop = _normalize_crop(crop)
    with connect() as conn:
        direct = rows_to_dicts(conn.execute(
            "SELECT cr.crop_year,cr.yield_value,cr.approved_yield,e.precipitation_in,e.heat_days_95,e.heat_days_90 "
            "FROM crop_records cr LEFT JOIN field_year_environment e ON e.field_id=? AND e.crop_year=cr.crop_year "
            "WHERE cr.field_id=? AND upper(cr.crop)=? AND cr.yield_value IS NOT NULL ORDER BY cr.crop_year",
            (field_id, field_id, crop),
        ).fetchall())
        linked_raw = rows_to_dicts(conn.execute(
            "SELECT cr.crop_year,cr.yield_value,cr.approved_yield,cr.metadata_json,m.unit_key,"
            "e.precipitation_in,e.heat_days_95,e.heat_days_90 "
            "FROM aph_unit_field_links l "
            "JOIN aph_unit_matches m ON m.id=l.match_id "
            "JOIN crop_records cr ON cr.source_document_id=m.source_document_id "
            "LEFT JOIN field_year_environment e ON e.field_id=l.field_id AND e.crop_year=cr.crop_year "
            "WHERE l.field_id=? AND m.match_status='confirmed' AND upper(cr.crop)=? "
            "AND cr.yield_value IS NOT NULL "
            "ORDER BY cr.crop_year",
            (field_id, crop),
        ).fetchall())
        linked=[]
        for rec in linked_raw:
            meta=_loads(rec.get("metadata_json"), {})
            if str(meta.get("unit_number") or "") == str(rec.get("unit_key") or ""):
                linked.append(rec)
        seen=set()
        rows=[]
        for r in direct+linked:
            k=(r.get("crop_year"),r.get("yield_value"))
            if k in seen: continue
            seen.add(k); rows.append(r)
    yields = [float(r["yield_value"]) for r in rows if r.get("yield_value") is not None]
    if not yields:
        return {"year_count": 0, "average_yield": None, "recent_5yr_average": None, "latest_approved_yield": None, "aph_yield_goal": None, "aph_yield_goal_source": None, "stability_score": None, "hot_dry_sensitivity": None}
    avg = mean(yields)
    stability = None
    if len(yields) >= 2 and avg:
        mad = mean(abs(x - avg) for x in yields)
        stability = round(max(0.0, min(100.0, 100.0 - (mad / avg * 180.0))), 0)

    weather_rows = [r for r in rows if r.get("precipitation_in") is not None and r.get("heat_days_95") is not None]
    sensitivity = None
    stress_years = 0
    if len(weather_rows) >= 4:
        rain_med = median(float(r["precipitation_in"]) for r in weather_rows)
        heat_med = median(float(r["heat_days_95"]) for r in weather_rows)
        stress = [float(r["yield_value"]) for r in weather_rows if float(r["precipitation_in"]) <= rain_med and float(r["heat_days_95"]) >= heat_med]
        normal = [float(r["yield_value"]) for r in weather_rows if not (float(r["precipitation_in"]) <= rain_med and float(r["heat_days_95"]) >= heat_med)]
        stress_years = len(stress)
        if stress and normal and mean(normal) > 0:
            sensitivity = round(max(-0.5, min(0.5, (mean(normal) - mean(stress)) / mean(normal))), 3)

    approved_rows = [
        r for r in rows
        if r.get("approved_yield") is not None
    ]
    approved_rows.sort(key=lambda r: int(r.get("crop_year") or 0))
    latest_approved = float(approved_rows[-1]["approved_yield"]) if approved_rows else None
    recent_avg = round(mean(yields[-5:]), 1) if yields else None
    aph_yield_goal = round(latest_approved, 1) if latest_approved is not None else recent_avg
    aph_yield_goal_source = "APH approved yield" if latest_approved is not None else ("APH recent 5-year average" if recent_avg is not None else None)

    return {
        "year_count": len(yields),
        "average_yield": round(avg, 1),
        "recent_5yr_average": recent_avg,
        "latest_approved_yield": round(latest_approved, 1) if latest_approved is not None else None,
        "aph_yield_goal": aph_yield_goal,
        "aph_yield_goal_source": aph_yield_goal_source,
        "stability_score": stability,
        "hot_dry_sensitivity": sensitivity,
        "weather_year_count": len(weather_rows),
        "stress_year_count": stress_years,
    }


def _aph_production_fit(crop: str, meta: dict[str, Any], tags: set[str], context: dict[str, Any]) -> tuple[float, list[str]]:
    years = int(context.get("year_count") or 0)
    if years < 3:
        return 0.0, []
    points = 0.0
    reasons: list[str] = []
    stability = context.get("stability_score")
    sensitivity = context.get("hot_dry_sensitivity")
    chars = _characteristic_map(meta)

    if stability is not None and stability >= 80:
        if "high_yield" in tags or "high_management" in tags or "broad_acre" in tags:
            points += 4.0
            reasons.append(f"APH history is stable ({int(stability)}/100), supporting more offensive yield positioning")
    elif stability is not None and stability < 65:
        if "stress" in tags or "drought" in tags or "broad_acre" in tags:
            points += 4.0
            reasons.append(f"APH history is variable ({int(stability)}/100), increasing value of defensive placement")

    if sensitivity is not None and sensitivity >= 0.08:
        if _normalize_crop(crop) == "CORN":
            drought = _numeric_rating(chars, "DROUGHT_TOLERANCE_USCB", "DROUGHT TOLERANCE")
            root = _numeric_rating(chars, "ROOT_STRENGTH_USCB", "ROOT STRENGTH")
            drought_pts = _quality_points(drought, 7.0)
            root_pts = _quality_points(root, 3.0)
            points += drought_pts + root_pts
            if drought_pts + root_pts >= 4:
                reasons.append(f"APH/weather history shows about {round(sensitivity*100)}% hot/dry downside; drought/root ratings gain weight")
        else:
            if "stress" in tags or "drought" in tags:
                points += 6.0
                reasons.append(f"APH/weather history shows about {round(sensitivity*100)}% hot/dry downside; stress tolerance gains weight")
    return round(min(points, 10.0), 1), reasons[:2]


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
    production_context = row.get("production_context") or {"year_count": 0}
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
        trait_points, trait_reasons = _bayer_trait_fit(
            crop,
            meta,
            row.get("weighted_aws150_cm"),
            drainage,
            row.get("weighted_slope_pct"),
            p.get("relative_maturity"),
            maturity_window,
        )
        management_points, management_reasons = _management_fit(
            crop,
            meta,
            row.get("default_tillage"),
            row.get("default_row_spacing"),
            row.get("default_planting_window"),
        )
        aph_points, aph_reasons = _aph_production_fit(crop, meta, tags, production_context)
        score = round(max(0, min(99, score + trait_points + management_points + aph_points)), 1)
        reasons = aph_reasons + management_reasons + trait_reasons + reasons
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
            "bayer_trait_points": trait_points,
            "management_points": management_points,
            "aph_points": aph_points,
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
            "SELECT f.id,f.farm_id,f.name,f.acres,f.crop AS field_crop,f.irrigation,fa.organization_id,fa.default_tillage,fa.default_row_spacing,fa.default_planting_window,cp.crop,cp.yield_goal,cp.target_population,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json,fl.centroid_lat,fl.centroid_lon FROM fields f JOIN farms fa ON fa.id=f.farm_id LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? LEFT JOIN field_soils fs ON fs.field_id=f.id LEFT JOIN LATERAL (SELECT centroid_lat,centroid_lon FROM field_locations x WHERE x.field_id=f.id ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) fl ON true WHERE f.id=?",
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
    row_dict = dict(row)
    production_context = _production_context(field_id, crop)
    row_dict["production_context"] = production_context
    ranked = _rank_products(row_dict, products)
    eligible_ranked = [x for x in ranked if x.get("location_eligible")]
    drainage = _loads(row.get("drainage_summary_json"), {})
    maturity_window = _maturity_window(crop, row.get("centroid_lat"))
    return {
        "field_id": field_id,
        "crop_year": crop_year,
        "field": {
            "name": row.get("name"), "acres": row.get("acres"), "crop": crop,
            "irrigation": row.get("irrigation"), "yield_goal": row.get("yield_goal"),
                "yield_goal_source": yield_goal_source,
            "awc_0_150cm": row.get("weighted_aws150_cm"), "drainage": drainage,
            "centroid_lat": row.get("centroid_lat"), "centroid_lon": row.get("centroid_lon"),
            "maturity_window": maturity_window,
            "farm_defaults": {
                "tillage": row.get("default_tillage"),
                "row_spacing": row.get("default_row_spacing"),
                "planting_window": row.get("default_planting_window"),
            },
            "aph_production_context": production_context,
        },
        "ranking_method": "SeedIQ hard location/maturity gate first, then SSURGO soil + IRR/NIRR + Bayer agronomic ratings + farm management + matched APH/weather production history; no AI/model cost",
        "population_note": "Population is a SeedIQ planning recommendation, not a Bayer/Channel prescription. Dealer/agronomist should confirm locally.",
        "catalog_product_count": len(ranked),
        "location_eligible_count": len(eligible_ranked),
        "recommendations": eligible_ranked[:max(1, min(limit, 5))],
        "all_options": ranked,
    }


@router.post("/api/farms/{farm_id}/channel-auto-plan")
def channel_auto_plan(farm_id: int, req: AutoPlanRequest):
    with connect() as conn:
        farm = conn.execute("SELECT id,organization_id,default_tillage,default_row_spacing,default_planting_window FROM farms WHERE id=?", (farm_id,)).fetchone()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        rows = rows_to_dicts(conn.execute(
            "SELECT f.id AS field_id,f.name,f.acres,f.crop AS field_crop,f.irrigation,cp.id AS crop_plan_id,cp.crop,cp.yield_goal,cp.target_population,cp.selected_seed_product_id,cp.seed_price_per_unit,cp.seeds_per_unit,cp.pricing_source,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json,fl.centroid_lat,fl.centroid_lon,fa.default_tillage,fa.default_row_spacing,fa.default_planting_window "
            "FROM fields f JOIN farms fa ON fa.id=f.farm_id LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? LEFT JOIN field_soils fs ON fs.field_id=f.id LEFT JOIN LATERAL (SELECT centroid_lat,centroid_lon FROM field_locations x WHERE x.field_id=f.id ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) fl ON true WHERE f.farm_id=? ORDER BY f.name",
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
            row["production_context"] = _production_context(int(row["field_id"]), crop)
            yield_goal_source = "manual"
            if row.get("yield_goal") is None:
                aph_goal = row["production_context"].get("aph_yield_goal")
                if aph_goal is not None:
                    row["yield_goal"] = float(aph_goal)
                    yield_goal_source = row["production_context"].get("aph_yield_goal_source") or "APH"
                else:
                    yield_goal_source = None
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
                "UPDATE field_crop_plans SET crop=?,yield_goal=COALESCE(yield_goal,?),selected_seed_product_id=?,target_population=?,seeds_per_unit=?,units_required=?,seed_cost_per_acre=?,total_seed_cost=?,status='seed_selected',updated_at=CURRENT_TIMESTAMP WHERE field_id=? AND crop_year=?",
                (crop, row.get("yield_goal"), top["seed_product_id"], population, unit_size, round(units_required, 3) if units_required is not None else None,
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
        "method": "Hard field-location maturity gate first; then Channel fit using SSURGO, IRR/NIRR, Bayer ratings, farm management and matched APH/weather production history. Products outside the local maturity window stay in the catalog but cannot be auto-selected. APH influence is conservative and cannot override the location gate.",
    }
