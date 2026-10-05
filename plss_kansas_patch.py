from __future__ import annotations

import re
from typing import Any

import httpx
from shapely.geometry import mapping, shape

import soil_service

# Kansas Geological Survey section data, published as a Kansas-only ArcGIS layer.
# Querying numeric township/range/section fields is more reliable than guessing
# the display-string format used by S_R_T.
_KS_SECTION_URL = (
    "https://services2.arcgis.com/ZOdjAzAQ2B0f85zi/ArcGIS/rest/services/"
    "PLSS_Section_Township_Range/FeatureServer/1/query"
)
_KS_WEST = -102.2
_KS_SOUTH = 36.9
_KS_EAST = -94.5
_KS_NORTH = 40.1


def _parse_trs(township_range: str, section: str | int) -> tuple[int, str, int, str, int]:
    raw = (township_range or "").upper().strip().replace(" ", "")
    match = re.match(r"^0*(\d{1,3})([NS])-?0*(\d{1,3})([EW])$", raw)
    if not match:
        raise ValueError(f"Unsupported township/range format: {township_range}")
    township = int(match.group(1))
    township_dir = match.group(2)
    range_no = int(match.group(3))
    range_dir = match.group(4)
    sec = int(str(section).strip())
    return township, township_dir, range_no, range_dir, sec


def resolve_kansas_plss(township_range: str, section: str | int) -> dict[str, Any]:
    township, township_dir, range_no, range_dir, sec = _parse_trs(township_range, section)
    params = {
        "where": f"TOWNSHIP={township} AND RANGE={range_no} AND SECTION_FL={sec}",
        "outFields": "S_R_T,TOWNSHIP,RANGE,SECTION_FL,TOWNSHIP_F,RANGE_FLAG",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    response = httpx.get(_KS_SECTION_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json() or {}
    features = data.get("features") or []

    candidates: list[tuple[float, Any, dict[str, Any]]] = []
    for feature in features:
        geom = feature.get("geometry")
        if not geom:
            continue
        candidate = shape(geom)
        if candidate.is_empty:
            continue
        centroid = candidate.centroid
        if not (_KS_WEST <= centroid.x <= _KS_EAST and _KS_SOUTH <= centroid.y <= _KS_NORTH):
            continue
        attrs = feature.get("properties") or {}
        candidates.append((centroid.x, candidate, attrs))

    if not candidates:
        raise LookupError(
            f"Kansas PLSS section not found for T{township}{township_dir} "
            f"R{range_no}{range_dir} S{sec}"
        )

    # Kansas has ranges on both sides of the Sixth Principal Meridian. The
    # layer's numeric RANGE value can match both; choose the eastmost candidate
    # for E ranges and westmost candidate for W ranges. This avoids relying on
    # undocumented flag encodings while remaining deterministic inside Kansas.
    candidates.sort(key=lambda item: item[0])
    _, chosen, attrs = candidates[-1] if range_dir == "E" else candidates[0]
    centroid = chosen.centroid
    source_ref = attrs.get("S_R_T") or f"T{township}{township_dir}-R{range_no}{range_dir}-S{sec}"

    return {
        "source": "Kansas Geological Survey PLSS sections",
        "source_reference": source_ref,
        "boundary_geojson": mapping(chosen),
        "centroid_lat": centroid.y,
        "centroid_lon": centroid.x,
        "confidence": 0.90,
        "notes": "Kansas-only KGS PLSS section resolved from numeric township/range/section fields.",
    }


# Patch the shared resolver before application/parser imports.
soil_service.resolve_kansas_plss = resolve_kansas_plss
