from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html import unescape
import re
from statistics import mean
from typing import Any

import httpx

from aph_utils import is_valid_production_record, match_identity, record_identity
from database import connect, json_dumps, rows_to_dicts

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
NOAA_RONI = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt"
NOAA_ONI = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
NOAA_ENSO_PROBABILITIES = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso/roni/probabilities/"
NOAA_ENSO_DISCUSSION = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_advisory/ensodisc.html"

# Crop-season ENSO summary. These overlapping seasons center on Jun-Aug and
# avoid reducing an entire crop year to a winter ENSO value.
GROWING_SEASON_ENSO_SEASONS = ("MJJ", "JJA", "JAS")



def _html_text(value: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value or "")
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def current_enso_outlook(crop_year: int | None = None) -> dict[str, Any]:
    """Fetch the current official NOAA CPC ENSO probabilities.

    This is deliberately advisory context. AcreFit never treats ENSO as a
    deterministic field-weather forecast; it uses the outlook to decide which
    parts of a field's own historical risk profile deserve more attention.
    """
    try:
        with httpx.Client(timeout=16.0, follow_redirects=True) as client:
            probs_r = client.get(NOAA_ENSO_PROBABILITIES)
            probs_r.raise_for_status()
            disc_r = client.get(NOAA_ENSO_DISCUSSION)
            disc_r.raise_for_status()
        probs_text = _html_text(probs_r.text)
        disc_text = _html_text(disc_r.text)

        issued = None
        m = re.search(r"Issued\s+([A-Za-z]+\s+\d{4})", probs_text, re.I)
        if m:
            issued = m.group(1)

        table: dict[str, dict[str, int]] = {}
        # NOAA renders rows like:
        # "MAM Mar Apr May 0 18 82"
        # so allow the three month labels between season code and percentages.
        row_pattern = re.compile(
            r"\b([A-Z]{3})\s+(?:[A-Za-z]{3}\s+){3}(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\b"
        )
        for season, la, neutral, el in row_pattern.findall(probs_text):
            if season not in table:
                table[season] = {
                    "la_nina_pct": int(la),
                    "neutral_pct": int(neutral),
                    "el_nino_pct": int(el),
                }

        # Defensive fallback if NOAA changes the month-label markup but keeps
        # the same visible table row structure.
        if not table:
            for season in ("ASO","SON","OND","NDJ","DJF","JFM","FMA","MAM","AMJ","MJJ","JJA","JAS"):
                m = re.search(
                    rf"\b{season}\b[^0-9]{{0,50}}(\d{{1,3}})\s+(\d{{1,3}})\s+(\d{{1,3}})\b",
                    probs_text,
                )
                if m:
                    table[season] = {
                        "la_nina_pct": int(m.group(1)),
                        "neutral_pct": int(m.group(2)),
                        "el_nino_pct": int(m.group(3)),
                    }

        status = None
        sm = re.search(r"ENSO Alert System Status:\s*([^:]+?)(?=Synopsis:|Synopsis|$)", disc_text, re.I)
        if sm:
            status = sm.group(1).strip(" .")

        synopsis = None
        syn = re.search(r"Synopsis:\s*(.+?)(?=In summary|This discussion|$)", disc_text, re.I)
        if syn:
            synopsis = syn.group(1).strip()
            if len(synopsis) > 700:
                synopsis = synopsis[:697].rsplit(" ",1)[0] + "..."

        # For a 2027 seed decision made in fall/winter 2026, MAM is planting
        # season context and AMJ is the earliest summer transition signal.
        planting = table.get("MAM") or table.get("FMA") or table.get("JFM")
        early_summer = table.get("AMJ") or table.get("MJJ") or table.get("JJA")
        winter = table.get("DJF") or table.get("NDJ") or table.get("JFM")

        planting_season = "MAM" if "MAM" in table else ("FMA" if "FMA" in table else "JFM" if "JFM" in table else None)
        early_summer_season = "AMJ" if "AMJ" in table else ("MJJ" if "MJJ" in table else "JJA" if "JJA" in table else None)
        winter_season = "DJF" if "DJF" in table else ("NDJ" if "NDJ" in table else "JFM" if "JFM" in table else None)

        return {
            "status": status,
            "issued": issued,
            "synopsis": synopsis,
            "probabilities": table,
            "winter": {"season": winter_season, **(winter or {})} if winter else None,
            "planting": {"season": planting_season, **(planting or {})} if planting else None,
            "early_summer": {"season": early_summer_season, **(early_summer or {})} if early_summer else None,
            "crop_year": crop_year,
            "source": "NOAA Climate Prediction Center RONI probabilities",
            "source_url": NOAA_ENSO_PROBABILITIES,
            "interpretation_note": "ENSO shifts seasonal odds; it does not predict rainfall or yield for an individual field.",
        }
    except Exception as exc:
        return {
            "status": None,
            "issued": None,
            "probabilities": {},
            "winter": None,
            "planting": None,
            "early_summer": None,
            "crop_year": crop_year,
            "source": "NOAA Climate Prediction Center",
            "error": str(exc)[:180],
            "interpretation_note": "ENSO shifts seasonal odds; it does not predict rainfall or yield for an individual field.",
        }


def _parse_noaa_index(text: str, value_column: int) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = defaultdict(dict)
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) <= value_column:
            continue
        season = parts[0].upper()
        try:
            year = int(parts[1])
            value = float(parts[value_column])
        except Exception:
            continue
        out[year][season] = value
    return dict(out)


def _fetch_enso_tables() -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        roni_r = client.get(NOAA_RONI)
        roni_r.raise_for_status()
        oni_r = client.get(NOAA_ONI)
        oni_r.raise_for_status()
    # RONI: SEAS YR ANOM. ONI: SEAS YR TOTAL ANOM.
    return _parse_noaa_index(roni_r.text, 2), _parse_noaa_index(oni_r.text, 3)


def _season_average(table: dict[int, dict[str, float]], year: int) -> float | None:
    vals = [table.get(year, {}).get(s) for s in GROWING_SEASON_ENSO_SEASONS]
    vals = [float(v) for v in vals if v is not None]
    return round(mean(vals), 2) if vals else None


def _enso_phase(index_value: float | None) -> str | None:
    if index_value is None:
        return None
    if index_value >= 0.5:
        return "El Nino"
    if index_value <= -0.5:
        return "La Nina"
    return "Neutral"


def _grid_key(lat: float, lon: float) -> tuple[float, float]:
    # ERA5 is a 0.25 degree reanalysis grid, not field-level rain-gauge data.
    # Bucket nearby fields to the same native grid so historical precipitation
    # is consistent and duplicate API calls are avoided.
    return (round(lat / 0.25) * 0.25, round(lon / 0.25) * 0.25)


def _fetch_weather_window(lat: float, lon: float, min_year: int, max_year: int) -> dict[int, dict[str, Any]]:
    start = date(min_year, 4, 1)
    end = date(max_year, 10, 15)
    params = {
        "latitude": round(lat, 5),
        "longitude": round(lon, 5),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "models": "era5",
    }
    r = httpx.get(OPEN_METEO_ARCHIVE, params=params, timeout=40.0)
    r.raise_for_status()
    payload = r.json()
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_sum") or []
    missing = [
        name for name, values in {
            "time": dates,
            "temperature_2m_max": highs,
            "temperature_2m_min": lows,
            "precipitation_sum": rain,
        }.items() if not values
    ]
    if missing:
        raise RuntimeError(
            "Open-Meteo ERA5 response missing " + ", ".join(missing) +
            f"; response keys={sorted(payload.keys())}, daily keys={sorted(daily.keys())}"
        )

    grouped: dict[int, list[tuple[datetime, float, float, float]]] = defaultdict(list)
    for ds, hi, lo, pr in zip(dates, highs, lows, rain):
        if hi is None or lo is None or pr is None:
            continue
        dt = datetime.strptime(ds, "%Y-%m-%d")
        if (dt.month, dt.day) < (4, 1) or (dt.month, dt.day) > (10, 15):
            continue
        grouped[dt.year].append((dt, float(hi), float(lo), float(pr)))

    result: dict[int, dict[str, Any]] = {}
    for year, rows in grouped.items():
        monthly_rain: dict[str, float] = defaultdict(float)
        gdd = 0.0
        for dt, hi, lo, pr in rows:
            monthly_rain[f"{dt.month:02d}"] += pr
            bounded_hi = min(86.0, hi)
            bounded_lo = max(50.0, lo)
            gdd += max(0.0, ((bounded_hi + bounded_lo) / 2.0) - 50.0)
        result[year] = {
            "precipitation_in": round(sum(x[3] for x in rows), 2),
            "avg_max_temp_f": round(mean(x[1] for x in rows), 1),
            "avg_min_temp_f": round(mean(x[2] for x in rows), 1),
            "heat_days_90": sum(1 for x in rows if x[1] >= 90),
            "heat_days_95": sum(1 for x in rows if x[1] >= 95),
            "dry_days": sum(1 for x in rows if x[3] < 0.01),
            "gdd_base50": round(gdd, 0),
            "monthly_precipitation_in": {m: round(v, 2) for m, v in sorted(monthly_rain.items())},
            "jun_aug_precipitation_in": round(sum(v for m, v in monthly_rain.items() if m in {"06", "07", "08"}), 2),
        }
    return result


def _field_years(field_ids: list[int]) -> dict[int, set[int]]:
    if not field_ids:
        return {}
    ph = ",".join("?" for _ in field_ids)
    with connect() as conn:
        direct = rows_to_dicts(conn.execute(
            f"SELECT field_id,crop_year,crop,practice,yield_value,metadata_json "
            f"FROM crop_records WHERE field_id IN ({ph}) AND crop_year IS NOT NULL",
            tuple(field_ids),
        ).fetchall())
        linked = rows_to_dicts(conn.execute(
            f"SELECT l.field_id,cr.crop_year,cr.crop,cr.practice,cr.yield_value,cr.metadata_json,"
            f"m.unit_key,m.metadata_json AS match_metadata_json "
            f"FROM aph_unit_field_links l JOIN aph_unit_matches m ON m.id=l.match_id "
            f"JOIN crop_records cr ON cr.source_document_id=m.source_document_id "
            f"WHERE l.field_id IN ({ph}) AND m.match_status='confirmed' AND cr.crop_year IS NOT NULL",
            tuple(field_ids),
        ).fetchall())

    out: dict[int, set[int]] = defaultdict(set)
    for row in direct:
        if is_valid_production_record(row):
            out[int(row["field_id"])].add(int(row["crop_year"]))

    for row in linked:
        if not is_valid_production_record(row):
            continue
        if record_identity(row) != match_identity({
            "unit_key": row.get("unit_key"),
            "metadata_json": row.get("match_metadata_json"),
        }):
            continue
        out[int(row["field_id"])].add(int(row["crop_year"]))
    return dict(out)

def enrich_climate_for_fields(field_ids: list[int]) -> dict[str, Any]:
    field_ids = sorted({int(x) for x in field_ids if int(x) > 0})
    if not field_ids:
        return {"field_count": 0, "years_written": 0, "results": []}

    ph = ",".join("?" for _ in field_ids)
    with connect() as conn:
        locations = rows_to_dicts(conn.execute(
            f"SELECT f.id AS field_id,f.name,fl.centroid_lat,fl.centroid_lon "
            f"FROM fields f LEFT JOIN LATERAL (SELECT centroid_lat,centroid_lon FROM field_locations x "
            f"WHERE x.field_id=f.id ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) fl ON true "
            f"WHERE f.id IN ({ph})",
            tuple(field_ids),
        ).fetchall())

    years_by_field = _field_years(field_ids)
    try:
        roni, oni = _fetch_enso_tables()
        enso_error = None
    except Exception as exc:
        roni, oni = {}, {}
        enso_error = str(exc)[:180]

    grid_groups: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
    skipped = []
    for loc in locations:
        fid = int(loc["field_id"])
        years = years_by_field.get(fid, set())
        if not years:
            skipped.append({"field_id": fid, "reason": "No matched APH crop years"})
            continue
        if loc.get("centroid_lat") is None or loc.get("centroid_lon") is None:
            skipped.append({"field_id": fid, "reason": "Field centroid unavailable"})
            continue
        lat, lon = float(loc["centroid_lat"]), float(loc["centroid_lon"])
        loc["years"] = years
        loc["lat"] = lat
        loc["lon"] = lon
        grid_groups[_grid_key(lat, lon)].append(loc)

    results = []
    years_written = 0
    weather_calls = 0

    weather_by_grid: dict[tuple[float,float], dict[int,dict[str,Any]]] = {}
    weather_errors: dict[tuple[float,float], str | None] = {}

    # Historical grid pulls are independent; parallelize them so a large farm
    # does not spend the whole Vercel request window waiting serially.
    def _fetch_grid(item):
        (grid_lat, grid_lon), members = item
        all_years = sorted({y for m in members for y in m["years"]})
        try:
            data = _fetch_weather_window(grid_lat, grid_lon, min(all_years), max(all_years))
            return (grid_lat, grid_lon), data, None
        except Exception as exc:
            return (grid_lat, grid_lon), {}, str(exc)[:180]

    max_workers = min(6, max(1, len(grid_groups)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_grid, item) for item in grid_groups.items()]
        for fut in as_completed(futures):
            key, data, err = fut.result()
            weather_by_grid[key] = data
            weather_errors[key] = err
            weather_calls += 1

    rows_to_write: list[tuple[Any,...]] = []
    for (grid_lat, grid_lon), members in grid_groups.items():
        weather = weather_by_grid.get((grid_lat, grid_lon), {})
        weather_error = weather_errors.get((grid_lat, grid_lon))
        for m in members:
            fid = int(m["field_id"])
            field_results = []
            for year in sorted(m["years"]):
                wx = weather.get(year)
                roni_value = _season_average(roni, year)
                oni_value = _season_average(oni, year)
                primary_index = roni_value if roni_value is not None else oni_value
                phase = _enso_phase(primary_index)
                meta = {
                    "field_latitude": m["lat"],
                    "field_longitude": m["lon"],
                    "weather_grid_latitude": round(grid_lat, 4),
                    "weather_grid_longitude": round(grid_lon, 4),
                    "weather_model": "era5",
                    "monthly_precipitation_in": (wx or {}).get("monthly_precipitation_in"),
                    "jun_aug_precipitation_in": (wx or {}).get("jun_aug_precipitation_in"),
                    "enso_primary_index": "RONI" if roni_value is not None else ("ONI" if oni_value is not None else None),
                    "roni_growing_season": roni_value,
                    "oni_growing_season": oni_value,
                    "enso_seasons_used": list(GROWING_SEASON_ENSO_SEASONS),
                    "enso_phase_method": "AcreFit crop-season phase from mean MJJ/JJA/JAS index; not NOAA official episode designation",
                    "weather_error": weather_error,
                    "enso_error": enso_error,
                }
                rows_to_write.append((
                    fid, year, f"{year}-04-01", f"{year}-10-15",
                    (wx or {}).get("precipitation_in"),
                    (wx or {}).get("avg_max_temp_f"),
                    (wx or {}).get("avg_min_temp_f"),
                    (wx or {}).get("heat_days_90"),
                    (wx or {}).get("heat_days_95"),
                    (wx or {}).get("dry_days"),
                    (wx or {}).get("gdd_base50"),
                    phase, primary_index,
                    "Open-Meteo ERA5 + NOAA CPC RONI/ONI",
                    json_dumps(meta),
                ))
                years_written += 1
                field_results.append({
                    "crop_year": year,
                    "status": "ready" if wx or phase else "partial",
                    "precipitation_in": (wx or {}).get("precipitation_in"),
                    "jun_aug_precipitation_in": (wx or {}).get("jun_aug_precipitation_in"),
                    "heat_days_95": (wx or {}).get("heat_days_95"),
                    "enso_phase": phase,
                    "roni": roni_value,
                    "oni": oni_value,
                })
            results.append({"field_id": fid, "field_name": m.get("name"), "years": field_results})

    # One database connection for the whole farm instead of hundreds of
    # connection/transaction round trips.
    if rows_to_write:
        with connect() as conn:
            sql = (
                "INSERT INTO field_year_environment(field_id,crop_year,season_start,season_end,precipitation_in,"
                "avg_max_temp_f,avg_min_temp_f,heat_days_90,heat_days_95,dry_days,gdd_base50,enso_phase,enso_index,source,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb) "
                "ON CONFLICT(field_id,crop_year) DO UPDATE SET "
                "season_start=excluded.season_start,season_end=excluded.season_end,"
                "precipitation_in=COALESCE(excluded.precipitation_in,field_year_environment.precipitation_in),"
                "avg_max_temp_f=COALESCE(excluded.avg_max_temp_f,field_year_environment.avg_max_temp_f),"
                "avg_min_temp_f=COALESCE(excluded.avg_min_temp_f,field_year_environment.avg_min_temp_f),"
                "heat_days_90=COALESCE(excluded.heat_days_90,field_year_environment.heat_days_90),"
                "heat_days_95=COALESCE(excluded.heat_days_95,field_year_environment.heat_days_95),"
                "dry_days=COALESCE(excluded.dry_days,field_year_environment.dry_days),"
                "gdd_base50=COALESCE(excluded.gdd_base50,field_year_environment.gdd_base50),"
                "enso_phase=COALESCE(excluded.enso_phase,field_year_environment.enso_phase),"
                "enso_index=COALESCE(excluded.enso_index,field_year_environment.enso_index),"
                "source=excluded.source,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP"
            )
            for row in rows_to_write:
                conn.execute(sql, row)

    return {
        "field_count": len(results),
        "years_written": years_written,
        "weather_api_calls": weather_calls,
        "grid_groups": len(grid_groups),
        "enso_source": "NOAA CPC RONI (primary) + ONI",
        "weather_source": "Open-Meteo ERA5",
        "skipped": skipped,
        "results": results,
    }


def enrich_climate_for_farm(farm_id: int) -> dict[str, Any]:
    with connect() as conn:
        linked_ids = [
            int(r["field_id"]) for r in conn.execute(
                "SELECT DISTINCT l.field_id FROM aph_unit_field_links l "
                "JOIN aph_unit_matches m ON m.id=l.match_id "
                "WHERE m.farm_id=? AND m.match_status='confirmed' ORDER BY l.field_id",
                (farm_id,),
            ).fetchall()
        ]
        direct_ids = [
            int(r["field_id"]) for r in conn.execute(
                "SELECT DISTINCT field_id FROM crop_records WHERE farm_id=? AND field_id IS NOT NULL ORDER BY field_id",
                (farm_id,),
            ).fetchall()
        ]
    result = enrich_climate_for_fields(sorted(set(linked_ids + direct_ids)))
    result["farm_id"] = farm_id
    return result
