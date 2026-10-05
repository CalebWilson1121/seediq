from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from database import connect, row_to_dict, rows_to_dicts
from channel_fit_routes import _fit_score, _loads

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
            "SELECT f.id AS field_id,f.name AS field_name,f.acres,f.irrigation,"
            "cp.crop,cp.yield_goal,cp.target_population,cp.seed_price_per_unit,cp.seeds_per_unit,"
            "cp.units_required,cp.seed_cost_per_acre,cp.total_seed_cost,cp.selected_seed_product_id,"
            "sp.product_name,sp.brand,sp.trait_package,sp.relative_maturity,sp.placement_text,sp.metadata_json,"
            "fl.boundary_geojson,fl.centroid_lat,fl.centroid_lon,fl.township_range,fl.section,"
            "fs.dominant_muname,fs.dominant_musym,fs.weighted_aws150_cm,fs.weighted_slope_pct,"
            "fs.drainage_summary_json,fs.hydrologic_group_summary_json "
            "FROM fields f "
            "JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? "
            "JOIN seed_products sp ON sp.id=cp.selected_seed_product_id "
            "LEFT JOIN field_locations fl ON fl.id=(SELECT x.id FROM field_locations x WHERE x.field_id=f.id ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) "
            "LEFT JOIN field_soils fs ON fs.field_id=f.id "
            "WHERE f.farm_id=? AND cp.selected_seed_product_id IS NOT NULL ORDER BY f.name",
            (proposal["crop_year"], proposal["farm_id"]),
        ).fetchall()

    fields = []
    for row in rows_to_dicts(rows):
        crop = (row.get("crop") or "").upper()
        drainage = _loads(row.get("drainage_summary_json"), {})
        if not isinstance(drainage, dict):
            drainage = {}
        meta = _loads(row.get("metadata_json"), {})
        if not isinstance(meta, dict):
            meta = {}
        raw_tags = meta.get("tags", [])
        if not isinstance(raw_tags, (list, tuple, set)):
            raw_tags = []
        tags = {str(x).lower() for x in raw_tags}
        fit_score, reasons = _fit_score(
            crop,
            row.get("irrigation"),
            row.get("weighted_aws150_cm"),
            drainage,
            row.get("yield_goal"),
            tags,
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
            "product_name": row.get("product_name"),
            "brand": row.get("brand"),
            "trait_package": row.get("trait_package"),
            "relative_maturity": row.get("relative_maturity"),
            "placement": row.get("placement_text"),
            "fit_score": fit_score,
            "reasons": reasons,
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
        "method": "SeedIQ deterministic field fit using crop, IRR/NIRR, SSURGO soil attributes, yield goal and published product positioning.",
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
