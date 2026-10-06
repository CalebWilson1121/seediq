from __future__ import annotations

import json
from datetime import date
from statistics import mean
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import connect, json_dumps, rows_to_dicts

router = APIRouter()


class ConfirmAPHMatchRequest(BaseModel):
    field_id: int


def _loads(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _unit_from_record(record: dict[str, Any]) -> str | None:
    meta = _loads(record.get("metadata_json"), {})
    unit = meta.get("unit_number")
    return str(unit) if unit not in (None, "") else None


def _weather_for_field_year(field_id: int, crop_year: int) -> dict[str, Any]:
    with connect() as conn:
        loc = conn.execute(
            "SELECT centroid_lat,centroid_lon FROM field_locations WHERE field_id=? "
            "ORDER BY updated_at DESC NULLS LAST,id DESC LIMIT 1",
            (field_id,),
        ).fetchone()
    if not loc or loc.get("centroid_lat") is None or loc.get("centroid_lon") is None:
        return {"status": "skipped", "reason": "Field centroid unavailable"}

    lat = float(loc["centroid_lat"])
    lon = float(loc["centroid_lon"])
    start = date(crop_year, 4, 1)
    end = date(crop_year, 10, 15)
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "models": "era5_land",
    }
    try:
        r = httpx.get("https://archive-api.open-meteo.com/v1/archive", params=params, timeout=35.0)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        return {"status": "error", "reason": f"Historical weather unavailable: {str(exc)[:180]}"}

    daily = payload.get("daily") or {}
    highs = [float(x) for x in (daily.get("temperature_2m_max") or []) if x is not None]
    lows = [float(x) for x in (daily.get("temperature_2m_min") or []) if x is not None]
    rain = [float(x) for x in (daily.get("precipitation_sum") or []) if x is not None]
    if not highs or not lows or not rain:
        return {"status": "error", "reason": "Historical weather response was incomplete"}

    n = min(len(highs), len(lows), len(rain))
    highs, lows, rain = highs[:n], lows[:n], rain[:n]
    gdd = 0.0
    for hi, lo in zip(highs, lows):
        bounded_hi = min(86.0, hi)
        bounded_lo = max(50.0, lo)
        gdd += max(0.0, ((bounded_hi + bounded_lo) / 2.0) - 50.0)

    metrics = {
        "precipitation_in": round(sum(rain), 2),
        "avg_max_temp_f": round(mean(highs), 1),
        "avg_min_temp_f": round(mean(lows), 1),
        "heat_days_90": sum(1 for x in highs if x >= 90),
        "heat_days_95": sum(1 for x in highs if x >= 95),
        "dry_days": sum(1 for x in rain if x < 0.01),
        "gdd_base50": round(gdd, 0),
    }
    with connect() as conn:
        conn.execute(
            "INSERT INTO field_year_environment(field_id,crop_year,season_start,season_end,precipitation_in,"
            "avg_max_temp_f,avg_min_temp_f,heat_days_90,heat_days_95,dry_days,gdd_base50,enso_phase,source,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb) "
            "ON CONFLICT(field_id,crop_year) DO UPDATE SET "
            "season_start=excluded.season_start,season_end=excluded.season_end,precipitation_in=excluded.precipitation_in,"
            "avg_max_temp_f=excluded.avg_max_temp_f,avg_min_temp_f=excluded.avg_min_temp_f,"
            "heat_days_90=excluded.heat_days_90,heat_days_95=excluded.heat_days_95,dry_days=excluded.dry_days,"
            "gdd_base50=excluded.gdd_base50,source=excluded.source,metadata_json=excluded.metadata_json,"
            "updated_at=CURRENT_TIMESTAMP",
            (
                field_id, crop_year, start.isoformat(), end.isoformat(),
                metrics["precipitation_in"], metrics["avg_max_temp_f"], metrics["avg_min_temp_f"],
                metrics["heat_days_90"], metrics["heat_days_95"], metrics["dry_days"], metrics["gdd_base50"],
                None, "Open-Meteo ERA5-Land",
                json_dumps({"latitude": lat, "longitude": lon, "weather_model": "era5_land", "enso_status": "pending"}),
            ),
        )
    return {"status": "ready", **metrics}


def _enrich_weather_for_field(field_id: int) -> dict[str, Any]:
    with connect() as conn:
        years = [
            int(r["crop_year"])
            for r in conn.execute(
                "SELECT DISTINCT crop_year FROM crop_records WHERE field_id=? AND crop_year IS NOT NULL ORDER BY crop_year",
                (field_id,),
            ).fetchall()
        ]
    results = []
    for year in years:
        results.append({"crop_year": year, **_weather_for_field_year(field_id, year)})
    return {"field_id": field_id, "years": results}


@router.get("/api/farms/{farm_id}/aph-matches")
def aph_matches(farm_id: int):
    with connect() as conn:
        matches = rows_to_dicts(conn.execute(
            "SELECT m.*,d.original_name FROM aph_unit_matches m JOIN documents d ON d.id=m.source_document_id "
            "WHERE m.farm_id=? ORDER BY d.uploaded_at DESC,m.unit_key",
            (farm_id,),
        ).fetchall())
        fields = rows_to_dicts(conn.execute(
            "SELECT id,name,acres,crop,practice,irrigation,farm_number,tract_number,field_number FROM fields "
            "WHERE farm_id=? ORDER BY name",
            (farm_id,),
        ).fetchall())
    for m in matches:
        m["metadata"] = _loads(m.pop("metadata_json", None), {})
    return {"farm_id": farm_id, "matches": matches, "fields": fields}


@router.put("/api/aph-matches/{match_id}/confirm")
def confirm_aph_match(match_id: int, req: ConfirmAPHMatchRequest):
    with connect() as conn:
        match = conn.execute("SELECT * FROM aph_unit_matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(status_code=404, detail="APH unit match not found")
        field = conn.execute("SELECT id,farm_id,name FROM fields WHERE id=?", (req.field_id,)).fetchone()
        if not field or int(field["farm_id"]) != int(match["farm_id"]):
            raise HTTPException(status_code=400, detail="Field does not belong to this farm")
        conn.execute(
            "UPDATE aph_unit_matches SET field_id=?,match_status='confirmed',confidence=1.0,method='manual_confirm',"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (req.field_id, match_id),
        )
        records = rows_to_dicts(conn.execute(
            "SELECT id,metadata_json FROM crop_records WHERE source_document_id=?",
            (match["source_document_id"],),
        ).fetchall())
        updated = 0
        for rec in records:
            if _unit_from_record(rec) == str(match["unit_key"]):
                conn.execute("UPDATE crop_records SET field_id=? WHERE id=?", (req.field_id, rec["id"]))
                updated += 1
    weather = _enrich_weather_for_field(req.field_id)
    return {
        "match_id": match_id,
        "field_id": req.field_id,
        "field_name": field["name"],
        "crop_records_attached": updated,
        "weather": weather,
    }


@router.post("/api/fields/{field_id}/history/weather")
def enrich_field_history_weather(field_id: int):
    with connect() as conn:
        field = conn.execute("SELECT id FROM fields WHERE id=?", (field_id,)).fetchone()
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    return _enrich_weather_for_field(field_id)


@router.get("/api/farms/{farm_id}/production-profile")
def farm_production_profile(farm_id: int):
    with connect() as conn:
        fields = rows_to_dicts(conn.execute(
            "SELECT f.id,f.name,f.acres,f.crop,f.irrigation,fl.centroid_lat,fl.centroid_lon,"
            "fs.dominant_muname,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json "
            "FROM fields f "
            "LEFT JOIN LATERAL (SELECT centroid_lat,centroid_lon FROM field_locations x WHERE x.field_id=f.id "
            "ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) fl ON true "
            "LEFT JOIN field_soils fs ON fs.field_id=f.id WHERE f.farm_id=? ORDER BY f.name",
            (farm_id,),
        ).fetchall())
        records = rows_to_dicts(conn.execute(
            "SELECT * FROM crop_records WHERE farm_id=? AND field_id IS NOT NULL ORDER BY field_id,crop_year",
            (farm_id,),
        ).fetchall())
        env = rows_to_dicts(conn.execute(
            "SELECT e.* FROM field_year_environment e JOIN fields f ON f.id=e.field_id WHERE f.farm_id=? "
            "ORDER BY e.field_id,e.crop_year",
            (farm_id,),
        ).fetchall())
        match_counts = conn.execute(
            "SELECT count(*) AS total,count(*) FILTER (WHERE match_status='confirmed') AS confirmed "
            "FROM aph_unit_matches WHERE farm_id=?",
            (farm_id,),
        ).fetchone()

    rec_by_field: dict[int, list[dict[str, Any]]] = {}
    for r in records:
        rec_by_field.setdefault(int(r["field_id"]), []).append(r)
    env_by_key = {(int(e["field_id"]), int(e["crop_year"])): e for e in env}

    out_fields = []
    for f in fields:
        fid = int(f["id"])
        field_records = rec_by_field.get(fid, [])
        years = []
        yield_vals = []
        for r in field_records:
            yv = r.get("yield_value")
            if yv is not None:
                yield_vals.append(float(yv))
            years.append({
                "crop_year": r.get("crop_year"),
                "crop": r.get("crop"),
                "practice": r.get("practice"),
                "planted_acres": r.get("planted_acres"),
                "production": r.get("production"),
                "yield_value": yv,
                "approved_yield": r.get("approved_yield"),
                "environment": env_by_key.get((fid, int(r["crop_year"]))) if r.get("crop_year") is not None else None,
            })
        avg_yield = round(mean(yield_vals), 1) if yield_vals else None
        if len(yield_vals) >= 2 and avg_yield:
            spread = mean(abs(x - avg_yield) for x in yield_vals)
            stability = round(max(0.0, min(100.0, 100.0 - (spread / avg_yield * 180.0))), 0)
        else:
            stability = None
        out_fields.append({
            **f,
            "drainage": _loads(f.get("drainage_summary_json"), {}),
            "production_summary": {
                "year_count": len(years),
                "average_yield": avg_yield,
                "yield_stability_score": stability,
            },
            "years": years,
        })

    return {
        "farm_id": farm_id,
        "aph_matching": {
            "total_units": int(match_counts["total"] or 0) if match_counts else 0,
            "confirmed_units": int(match_counts["confirmed"] or 0) if match_counts else 0,
        },
        "fields": out_fields,
        "weather_source": "Open-Meteo ERA5-Land historical reanalysis, Apr 1–Oct 15",
        "enso_status": "schema ready; NOAA ONI enrichment is the next climate layer",
    }
