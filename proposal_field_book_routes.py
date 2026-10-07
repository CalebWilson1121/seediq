from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from database import connect, row_to_dict, rows_to_dicts
from channel_fit_routes import _loads, _rank_products, _production_context, _whole_farm_reason_summary
from climate_service import current_enso_outlook

router = APIRouter()
logger = logging.getLogger(__name__)


def _boundary(value: Any):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _build_field_book_data(token: str):
    with connect() as conn:
        proposal_row = conn.execute(
            "SELECT sp.*,p.prospect_name,f.farm_name,f.producer_name "
            "FROM seed_proposals sp JOIN prospects p ON p.id=sp.prospect_id "
            "JOIN farms f ON f.id=sp.farm_id WHERE sp.public_token=?",
            (token,),
        ).fetchone()
        if not proposal_row:
            raise HTTPException(status_code=404, detail="Proposal not found")
        proposal = row_to_dict(proposal_row) or {}
        rows = conn.execute(
            "SELECT f.id AS field_id,f.name AS field_name,f.acres,f.crop AS field_crop,f.irrigation,"
            "cp.crop,cp.yield_goal,cp.target_population,cp.seed_price_per_unit,cp.seeds_per_unit,"
            "cp.units_required,cp.seed_cost_per_acre,cp.total_seed_cost,cp.pricing_source,cp.selected_seed_product_id,"
            "sp.id AS seed_product_id,sp.product_name,sp.brand,sp.trait_package,sp.relative_maturity,sp.placement_text,sp.metadata_json,sp.unit_size_seeds,sp.source_url,"
            "fl.boundary_geojson,fl.centroid_lat,fl.centroid_lon,fl.township_range,fl.section,"
            "fs.dominant_muname,fs.dominant_musym,fs.weighted_aws150_cm,fs.weighted_slope_pct,"
            "fs.drainage_summary_json,fs.hydrologic_group_summary_json,fa.default_tillage,fa.default_row_spacing,fa.default_planting_window "
            "FROM fields f JOIN farms fa ON fa.id=f.farm_id "
            "JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? "
            "JOIN seed_products sp ON sp.id=cp.selected_seed_product_id "
            "LEFT JOIN field_locations fl ON fl.id=(SELECT x.id FROM field_locations x WHERE x.field_id=f.id ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) "
            "LEFT JOIN field_soils fs ON fs.field_id=f.id "
            "WHERE f.farm_id=? AND cp.selected_seed_product_id IS NOT NULL ORDER BY f.name",
            (proposal["crop_year"], proposal["farm_id"]),
        ).fetchall()

    outlook = current_enso_outlook(int(proposal.get("crop_year") or 0))
    fields = []
    for row in rows_to_dicts(rows):
        crop = (row.get("crop") or "").upper()
        drainage = _loads(row.get("drainage_summary_json"), {})
        if not isinstance(drainage, dict):
            drainage = {}
        meta = _loads(row.get("metadata_json"), {})
        if not isinstance(meta, dict):
            meta = {}
        product = {
            "id": row.get("seed_product_id"),
            "product_name": row.get("product_name"),
            "brand": row.get("brand"),
            "crop": crop,
            "relative_maturity": row.get("relative_maturity"),
            "trait_package": row.get("trait_package"),
            "placement_text": row.get("placement_text"),
            "metadata_json": row.get("metadata_json"),
            "unit_size_seeds": row.get("unit_size_seeds"),
            "source_url": row.get("source_url"),
        }
        context = _production_context(int(row.get("field_id")), crop)
        row["production_context"] = context
        row["climate_outlook"] = outlook
        ranked = _rank_products(row, [product])
        selected_fit = ranked[0] if ranked else {}
        fit_score = selected_fit.get("fit_score", 0)
        reasons = selected_fit.get("reasons", [])
        why = _whole_farm_reason_summary(row, selected_fit, context)
        planting = outlook.get("planting") or {}
        summer = outlook.get("early_summer") or {}
        hist = why.get("climate_history") or {}
        el_pct = planting.get("el_nino_pct")
        delta = hist.get("el_nino_vs_overall_pct")
        climate_decision = None
        if el_pct is not None:
            if delta is not None and delta <= -5:
                climate_decision = (
                    f"NOAA shows {el_pct}% El Nino odds for {planting.get('season') or 'spring'}; "
                    f"this field has averaged {abs(delta):.0f}% below its overall yield in El Nino years. "
                    "That supports keeping stress stability, roots and moisture-use efficiency in the seed decision."
                )
            elif delta is not None and delta >= 5:
                climate_decision = (
                    f"NOAA shows {el_pct}% El Nino odds for {planting.get('season') or 'spring'}; "
                    f"this field has averaged {delta:.0f}% above its overall yield in El Nino years. "
                    "That supports protecting top-end yield potential rather than over-defending."
                )
            else:
                climate_decision = (
                    f"NOAA shows {el_pct}% El Nino odds for {planting.get('season') or 'spring'}, "
                    "but this field does not show a strong historical El Nino yield bias. ENSO remains a secondary factor."
                )
            hotdry = hist.get("hot_dry_sensitivity")
            wxyears = int(hist.get("weather_year_count") or 0)
            if hotdry is not None and wxyears >= 4:
                if float(hotdry) >= 0.05:
                    climate_decision += (
                        f" Across {wxyears} weather-linked APH years, hotter/drier seasons averaged about "
                        f"{round(float(hotdry)*100)}% below normal yield, increasing the value of stress, roots and moisture-use traits."
                    )
                elif float(hotdry) <= -0.05:
                    climate_decision += (
                        f" Across {wxyears} weather-linked APH years, hotter/drier seasons have not reduced yield, "
                        "so climate risk does not justify giving up top-end yield potential."
                    )
                else:
                    climate_decision += (
                        f" Across {wxyears} weather-linked APH years, hot/dry conditions have not created a strong repeatable yield penalty."
                    )
            if summer.get("neutral_pct") is not None and summer.get("el_nino_pct") is not None:
                climate_decision += (
                    f" NOAA shifts to {summer.get('neutral_pct')}% Neutral / {summer.get('el_nino_pct')}% El Nino "
                    f"for {summer.get('season') or 'early summer'}, so the winter signal is not treated as a summer guarantee."
                )
        hydro = _loads(row.get("hydrologic_group_summary_json"), {})
        if not isinstance(hydro, dict):
            hydro = {}
        fields.append({
            "field_id": row.get("field_id"),
            "field_name": row.get("field_name"),
            "acres": row.get("acres"),
            "irrigation": row.get("irrigation"),
            "crop": crop,
            "yield_goal": row.get("yield_goal"),
            "target_population": row.get("target_population"),
            "seed_price_per_unit": row.get("seed_price_per_unit"),
            "seeds_per_unit": row.get("seeds_per_unit"),
            "units_required": row.get("units_required"),
            "seed_cost_per_acre": row.get("seed_cost_per_acre"),
            "total_seed_cost": row.get("total_seed_cost"),
            "pricing_source": row.get("pricing_source"),
            "product_name": row.get("product_name"),
            "brand": row.get("brand"),
            "trait_package": row.get("trait_package"),
            "relative_maturity": row.get("relative_maturity"),
            "placement": row.get("placement_text"),
            "fit_score": fit_score,
            "reasons": reasons,
            "agronomy_score": selected_fit.get("agronomy_score"),
            "agronomy_engine_version": selected_fit.get("agronomy_engine_version"),
            "agronomy_rules_fired": selected_fit.get("agronomy_rules_fired", []),
            "agronomy_source_ids": selected_fit.get("agronomy_source_ids", []),
            "location_eligible": selected_fit.get("location_eligible"),
            "location_reason": selected_fit.get("location_reason"),
            "bayer_trait_points": selected_fit.get("bayer_trait_points"),
            "management_points": selected_fit.get("management_points"),
            "climate_decision": climate_decision,
            "climate_history": why.get("climate_history"),
            "field_signals": why.get("field_signals"),
            "boundary_geojson": _boundary(row.get("boundary_geojson")),
            "centroid_lat": row.get("centroid_lat"),
            "centroid_lon": row.get("centroid_lon"),
            "township_range": row.get("township_range"),
            "section": row.get("section"),
            "dominant_soil": row.get("dominant_muname"),
            "dominant_soil_symbol": row.get("dominant_musym"),
            "awc_0_150cm": row.get("weighted_aws150_cm"),
            "slope_pct": row.get("weighted_slope_pct"),
            "drainage": drainage,
            "hydrologic_group": hydro,
        })

    return {
        "proposal_id": proposal.get("id"),
        "prospect_name": proposal.get("prospect_name"),
        "farm_name": proposal.get("farm_name"),
        "producer_name": proposal.get("producer_name"),
        "crop_year": proposal.get("crop_year"),
        "salesperson_name": proposal.get("salesperson_name"),
        "salesperson_email": proposal.get("salesperson_email"),
        "salesperson_phone": proposal.get("salesperson_phone"),
        "fields": fields,
        "field_count": len(fields),
        "planned_acres": round(sum(float(x.get("acres") or 0) for x in fields), 2),
        "estimated_seed_value": round(sum(float(x.get("total_seed_cost") or 0) for x in fields), 2),
        "climate_outlook": outlook,
        "method": "SeedIQ full field-fit engine using location/maturity eligibility, neutral land-grant Extension agronomy rules, SSURGO soil, IRR/NIRR, product agronomic ratings, farm management defaults and yield environment.",
        "population_note": "Planting populations are SeedIQ planning recommendations and should be confirmed by the dealer/agronomist for local conditions.",
    }


@router.get("/api/public/proposals/{token}/field-book-data")
def public_field_book_data(token: str):
    try:
        return _build_field_book_data(token)
    except HTTPException:
        raise
    except Exception:
        logger.exception("field-book-data failed")
        raise HTTPException(status_code=500, detail="Unable to build field book")
