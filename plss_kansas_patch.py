from __future__ import annotations

import re
from typing import Any

import httpx
from shapely.geometry import mapping, shape

import soil_service

# Kansas Geological Survey section data, published as a Kansas-only ArcGIS layer.
# Using a Kansas-only source avoids the nationally duplicated township/range
# identifiers that previously sent mapped SOI fields to Alaska.
_KS_SECTION_URL = (
    "https://services2.arcgis.com/ZOdjAzAQ2B0f85zi/ArcGIS/rest/services/"
    "PLSS_Section_Township_Range/FeatureServer/1/query"
)
_KS_WEST = -102.2
_KS_SOUTH = 36.9
_KS_EAST = -94.5
_KS_NORTH = 40.1


def _srt_key(township_range: str, section: str | int) -> str:
    raw = (township_range or "").upper().strip().replace(" ", "")
    match = re.match(r"^0*(\d{1,3})([NS])-?0*(\d{1,3})([EW])$", raw)
    if not match:
        raise ValueError(f"Unsupported township/range format: {township_range}")
    township = int(match.group(1))
    township_dir = match.group(2)
    range_no = int(match.group(3))
    range_dir = match.group(4)
    sec = int(str(section).strip())
    return f"S{sec}-T{township}{township_dir}-R{range_no}{range_dir}"


def resolve_kansas_plss(township_range: str, section: str | int) -> dict[str, Any]:
    srt = _srt_key(township_range, section)
    params = {
        "where": f"S_R_T='{srt}'",
        "outFields": "S_R_T,TOWNSHIP,RANGE",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    response = httpx.get(_KS_SECTION_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json() or {}
    features = data.get("features") or []

    for feature in features:
        geom = feature.get("geometry")
        if not geom:
            continue
        candidate = shape(geom)
        if candidate.is_empty:
            continue
        centroid = candidate.centroid
        if _KS_WEST <= centroid.x <= _KS_EAST and _KS_SOUTH <= centroid.y <= _KS_NORTH:
            return {
                "source": "Kansas Geological Survey PLSS sections",
                "source_reference": srt,
                "boundary_geojson": mapping(candidate),
                "centroid_lat": centroid.y,
                "centroid_lon": centroid.x,
                "confidence": 0.90,
                "notes": "Kansas-only KGS PLSS section used to georeference a mapped field.",
            }

    raise LookupError(f"Kansas PLSS section not found for {srt}")


# Patch the shared resolver before application/parser imports.
soil_service.resolve_kansas_plss = resolve_kansas_plss
