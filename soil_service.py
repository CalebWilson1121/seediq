from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

import httpx
from shapely.geometry import shape, mapping
from shapely.geometry.polygon import orient
from shapely import wkt as shapely_wkt

from database import connect, json_dumps, rows_to_dicts

KS_PLSS_URL = "https://services.arcgis.com/f4rR7WnIfGBdVYFd/arcgis/rest/services/Townships_and_Sections/FeatureServer/0/query"
SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"


def _loads(value: Any, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _float(value: Any) -> float | None:
    if value in (None, "", "NULL", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ks_trs(township_range: str, section: str | int) -> str:
    # NAU commonly prints either 001S-016E or compact 001S016E.
    # Kansas PLSS service stores compact 8-char TRS such as 01S16E34.
    raw = (township_range or "").upper().strip().replace(" ", "")
    m = re.match(r"^0*(\d{1,3})([NS])-?0*(\d{1,3})([EW])$", raw)
    if not m:
        raise ValueError(f"Unsupported township/range format: {township_range}")
    sec = int(str(section).strip())
    return f"{int(m.group(1)):02d}{m.group(2)}{int(m.group(3)):02d}{m.group(4)}{sec:02d}"


def resolve_kansas_plss(township_range: str, section: str | int) -> dict[str, Any]:
    trs = _ks_trs(township_range, section)
    params = {
        "where": f"PLSS_TYPE='Section' AND PLSS_TRS='{trs}'",
        "outFields": "PLSS_TRS,PLSS_TYPE",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    r = httpx.get(KS_PLSS_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    features = data.get("features") or []
    if not features:
        raise LookupError(f"Kansas PLSS section not found for {trs}")
    geom = features[0].get("geometry")
    if not geom:
        raise LookupError(f"Kansas PLSS returned no geometry for {trs}")
    g = shape(geom)
    c = g.centroid
    return {
        "source": "Kansas PLSS / KGS ArcGIS",
        "source_reference": trs,
        "boundary_geojson": mapping(g),
        "centroid_lat": c.y,
        "centroid_lon": c.x,
        "confidence": 0.60,
        "notes": "Section-level boundary resolved from APH township/range/section. Use an exact field boundary for final placement analysis.",
    }


def _sda_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        table = payload.get("Table") or payload.get("table") or payload.get("Data") or []
    else:
        table = payload
    if not isinstance(table, list) or not table:
        return []
    if isinstance(table[0], dict):
        return table
    headers = table[0]
    return [dict(zip(headers, row)) for row in table[1:] if isinstance(row, list)]


def fetch_ssurgo(boundary_geojson: dict[str, Any]) -> dict[str, Any]:
    geom = shape(boundary_geojson)
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Soil enrichment requires a Polygon or MultiPolygon boundary")
    if geom.is_empty:
        raise ValueError("Soil enrichment boundary is empty")
    if not geom.is_valid:
        geom = geom.buffer(0)
    # USDA SDA ultimately runs the WKT through SQL Server geometry/geography.
    # Generated pivot polygons can contain duplicate closure vertices or more
    # coordinate precision than that pipeline likes. Normalize, simplify by
    # ~10 cm, orient polygon rings consistently and emit bounded precision WKT.
    geom = geom.simplify(0.000001, preserve_topology=True)
    if geom.geom_type == "Polygon":
        geom = orient(geom, sign=1.0)
    elif geom.geom_type == "MultiPolygon":
        from shapely.geometry import MultiPolygon
        geom = MultiPolygon([orient(g, sign=1.0) for g in geom.geoms])
    if geom.is_empty or not geom.is_valid:
        raise ValueError("Field boundary could not be normalized for USDA Soil Data Access")
    wkt = shapely_wkt.dumps(geom, rounding_precision=7, trim=True).replace("'", "''")
    query = f"""
~DeclareGeometry(@aoi)~
select @aoi = geometry::STGeomFromText('{wkt}', 4326)
~DeclareIdGeomTable(@intersectedPolygonGeometries)~
~GetClippedMapunits(@aoi,polygon,geo,@intersectedPolygonGeometries)~
~DeclareIdGeogTable(@intersectedPolygonGeographies)~
~GetGeogFromGeomWgs84(@intersectedPolygonGeometries,@intersectedPolygonGeographies)~
select id, sum(geog.STArea()) as area_m2
into #aggarea
from @intersectedPolygonGeographies
group by id;
select
  cast(M.mukey as varchar(30)) as mukey,
  M.musym,
  M.muname,
  L.areasymbol,
  A.area_m2 / 4046.8564224 as area_acres,
  MA.slopegradwta,
  MA.aws025wta,
  MA.aws050wta,
  MA.aws0100wta,
  MA.aws0150wta,
  MA.drclassdcd,
  MA.drclasswettest,
  MA.hydgrpdcd,
  MA.flodfreqdcd,
  MA.pondfreqprs,
  MA.brockdepmin,
  MA.niccdcd,
  MA.iccdcd
from #aggarea A
join mapunit M on A.id = M.mukey
join legend L on M.lkey = L.lkey
left join muaggatt MA on M.mukey = MA.mukey
order by A.area_m2 desc;
"""
    r = httpx.post(SDA_URL, data={"query": query, "format": "JSON+COLUMNNAME"}, timeout=60)
    r.raise_for_status()
    rows = _sda_rows(r.json())
    if not rows:
        raise LookupError("USDA Soil Data Access returned no SSURGO map units for this boundary")

    normalized = []
    total = 0.0
    for row in rows:
        acres = _float(row.get("area_acres")) or 0.0
        total += acres
        normalized.append({
            "mukey": str(row.get("mukey") or ""),
            "musym": row.get("musym"),
            "muname": row.get("muname"),
            "areasymbol": row.get("areasymbol"),
            "acres": acres,
            "slope_pct": _float(row.get("slopegradwta")),
            "aws025_cm": _float(row.get("aws025wta")),
            "aws050_cm": _float(row.get("aws050wta")),
            "aws100_cm": _float(row.get("aws0100wta")),
            "aws150_cm": _float(row.get("aws0150wta")),
            "drainage": row.get("drclassdcd"),
            "wettest_drainage": row.get("drclasswettest"),
            "hydrologic_group": row.get("hydgrpdcd"),
            "flood_frequency": row.get("flodfreqdcd"),
            "ponding": row.get("pondfreqprs"),
            "bedrock_depth_cm": _float(row.get("brockdepmin")),
            "nonirrigated_capability": row.get("niccdcd"),
            "irrigated_capability": row.get("iccdcd"),
        })
    if total <= 0:
        total = sum(x["acres"] for x in normalized) or 1.0
    for x in normalized:
        x["percent"] = round(100.0 * x["acres"] / total, 2)

    def weighted(key: str):
        vals = [(x[key], x["acres"]) for x in normalized if x.get(key) is not None and x["acres"] > 0]
        den = sum(a for _, a in vals)
        return round(sum(v * a for v, a in vals) / den, 3) if den else None

    def distribution(key: str):
        d: dict[str, float] = defaultdict(float)
        for x in normalized:
            if x.get(key):
                d[str(x[key])] += x["acres"]
        return {k: round(v / total * 100.0, 2) for k, v in sorted(d.items(), key=lambda z: -z[1])}

    dominant = normalized[0]
    return {
        "source": "USDA NRCS SSURGO / Soil Data Access",
        "total_area_acres": round(total, 2),
        "dominant_mukey": dominant.get("mukey"),
        "dominant_musym": dominant.get("musym"),
        "dominant_muname": dominant.get("muname"),
        "weighted_aws025_cm": weighted("aws025_cm"),
        "weighted_aws050_cm": weighted("aws050_cm"),
        "weighted_aws100_cm": weighted("aws100_cm"),
        "weighted_aws150_cm": weighted("aws150_cm"),
        "weighted_slope_pct": weighted("slope_pct"),
        "drainage_summary": distribution("drainage"),
        "hydrologic_group_summary": distribution("hydrologic_group"),
        "mapunits": normalized,
    }


def _save_location(field_id: int, location: dict[str, Any], township_range=None, section=None, fsa_farm_number=None) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            "INSERT INTO field_locations(field_id,source,source_reference,township_range,section,fsa_farm_number,boundary_geojson,centroid_lat,centroid_lon,confidence,status,notes) "
            "VALUES(?,?,?,?,?,?,?::jsonb,?,?,?,?,?) "
            "ON CONFLICT(field_id) DO UPDATE SET source=excluded.source,source_reference=excluded.source_reference,township_range=excluded.township_range,section=excluded.section,fsa_farm_number=excluded.fsa_farm_number,boundary_geojson=excluded.boundary_geojson,centroid_lat=excluded.centroid_lat,centroid_lon=excluded.centroid_lon,confidence=excluded.confidence,status=excluded.status,notes=excluded.notes,updated_at=CURRENT_TIMESTAMP",
            (field_id, location["source"], location.get("source_reference"), township_range, str(section) if section is not None else None, fsa_farm_number,
             json_dumps(location["boundary_geojson"]), location.get("centroid_lat"), location.get("centroid_lon"), location.get("confidence"), "resolved", location.get("notes")),
        )
        row = conn.execute("SELECT * FROM field_locations WHERE field_id=?", (field_id,)).fetchone()
        return dict(row)


def set_exact_boundary(field_id: int, boundary_geojson: dict[str, Any]) -> dict[str, Any]:
    g = shape(boundary_geojson)
    if g.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Boundary must be Polygon or MultiPolygon GeoJSON")
    c = g.centroid
    loc = {
        "source": "user_exact_boundary",
        "source_reference": "manual/external field boundary",
        "boundary_geojson": mapping(g),
        "centroid_lat": c.y,
        "centroid_lon": c.x,
        "confidence": 1.0,
        "notes": "Exact field boundary supplied from MBAR/Mapped SOI/manual import.",
    }
    return _save_location(field_id, loc)


def save_section_location(field_id: int, township_range: str, section: str | int, fsa_farm_number=None) -> dict[str, Any]:
    loc = resolve_kansas_plss(township_range, section)
    return _save_location(field_id, loc, township_range, section, fsa_farm_number)


def _field_location(field_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM field_locations WHERE field_id=?", (field_id,)).fetchone()


def _field_row(field_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()


def _ensure_location(field_id: int) -> dict[str, Any]:
    location = _field_location(field_id)
    if location:
        return dict(location)
    field = _field_row(field_id)
    if not field:
        raise KeyError(f"Field {field_id} not found")
    metadata = _loads(field["metadata_json"], {})
    boundary = metadata.get("boundary_geojson")
    if boundary:
        return set_exact_boundary(field_id, boundary)
    township_range = metadata.get("township_range")
    section = metadata.get("section")
    if township_range and section:
        return save_section_location(field_id, township_range, section, field.get("farm_number"))
    raise LookupError("No usable field boundary or township/range/section location is available for this field")


def _soil_payload(row) -> dict[str, Any]:
    d = dict(row)
    d["mapunits"] = _loads(d.pop("mapunits_json", None), [])
    d["drainage_summary"] = _loads(d.pop("drainage_summary_json", None), {})
    d["hydrologic_group_summary"] = _loads(d.pop("hydrologic_group_summary_json", None), {})
    return d


def enrich_field(field_id: int, force: bool = False) -> dict[str, Any]:
    with connect() as conn:
        if not force:
            existing = conn.execute("SELECT * FROM field_soils WHERE field_id=?", (field_id,)).fetchone()
            if existing:
                return _soil_payload(existing)
    location = _ensure_location(field_id)
    boundary = _loads(location.get("boundary_geojson"), {})
    soil = fetch_ssurgo(boundary)
    with connect() as conn:
        conn.execute(
            "INSERT INTO field_soils(field_id,source,total_area_acres,dominant_mukey,dominant_musym,dominant_muname,weighted_aws025_cm,weighted_aws050_cm,weighted_aws100_cm,weighted_aws150_cm,weighted_slope_pct,drainage_summary_json,hydrologic_group_summary_json,mapunits_json,status,enriched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?::jsonb,'ready',CURRENT_TIMESTAMP) "
            "ON CONFLICT(field_id) DO UPDATE SET source=excluded.source,total_area_acres=excluded.total_area_acres,dominant_mukey=excluded.dominant_mukey,dominant_musym=excluded.dominant_musym,dominant_muname=excluded.dominant_muname,weighted_aws025_cm=excluded.weighted_aws025_cm,weighted_aws050_cm=excluded.weighted_aws050_cm,weighted_aws100_cm=excluded.weighted_aws100_cm,weighted_aws150_cm=excluded.weighted_aws150_cm,weighted_slope_pct=excluded.weighted_slope_pct,drainage_summary_json=excluded.drainage_summary_json,hydrologic_group_summary_json=excluded.hydrologic_group_summary_json,mapunits_json=excluded.mapunits_json,status='ready',enriched_at=CURRENT_TIMESTAMP",
            (field_id, soil["source"], soil["total_area_acres"], soil["dominant_mukey"], soil["dominant_musym"], soil["dominant_muname"], soil["weighted_aws025_cm"], soil["weighted_aws050_cm"], soil["weighted_aws100_cm"], soil["weighted_aws150_cm"], soil["weighted_slope_pct"], json_dumps(soil["drainage_summary"]), json_dumps(soil["hydrologic_group_summary"]), json_dumps(soil["mapunits"])),
        )
    return soil


def enrich_prospect(prospect_id: int, force: bool = False) -> dict[str, Any]:
    with connect() as conn:
        prospect = conn.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if not prospect:
            raise KeyError(f"Prospect {prospect_id} not found")
        fields = conn.execute("SELECT id FROM fields WHERE farm_id=? ORDER BY id", (prospect["farm_id"],)).fetchall()
    results = []
    for field in fields:
        try:
            results.append({"field_id": field["id"], "status": "ready", "soil": enrich_field(field["id"], force=force)})
        except Exception as exc:
            results.append({"field_id": field["id"], "status": "error", "error": str(exc)})
    return {"prospect_id": prospect_id, "fields": results}


def prospect_soil_status(prospect_id: int) -> dict[str, Any]:
    with connect() as conn:
        prospect = conn.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if not prospect:
            raise KeyError(f"Prospect {prospect_id} not found")
        rows = conn.execute("SELECT f.id AS field_id,f.name,f.acres,f.county,f.state,f.farm_number,f.tract_number,f.field_number,fl.source AS location_source,fl.source_reference,fl.township_range,fl.section,fl.boundary_geojson,fl.centroid_lat,fl.centroid_lon,fl.confidence,fs.status AS soil_status,fs.total_area_acres,fs.dominant_musym,fs.dominant_muname,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json,fs.hydrologic_group_summary_json FROM fields f LEFT JOIN field_locations fl ON fl.field_id=f.id LEFT JOIN field_soils fs ON fs.field_id=f.id WHERE f.farm_id=? ORDER BY f.name", (prospect["farm_id"],)).fetchall()
    result = rows_to_dicts(rows)
    for row in result:
        for key in ("boundary_geojson","drainage_summary_json","hydrologic_group_summary_json"):
            row[key] = _loads(row.get(key), {} if key != "boundary_geojson" else None)
    return {"prospect_id": prospect_id, "farm_id": int(prospect["farm_id"]), "fields": result}
