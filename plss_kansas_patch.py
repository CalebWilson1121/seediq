from __future__ import annotations

from typing import Any

import httpx
from shapely.geometry import mapping, shape

import soil_service

# Kansas bounding box in WGS84. This is intentionally a little generous so
# border sections are not rejected, while Alaska and other PLSS duplicates are.
_KS_WEST = -102.2
_KS_SOUTH = 36.9
_KS_EAST = -94.5
_KS_NORTH = 40.1


def resolve_kansas_plss(township_range: str, section: str | int) -> dict[str, Any]:
    trs = soil_service._ks_trs(township_range, section)
    params = {
        "where": f"PLSS_TYPE='Section' AND PLSS_TRS='{trs}'",
        "outFields": "PLSS_TRS,PLSS_TYPE",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometry": f"{_KS_WEST},{_KS_SOUTH},{_KS_EAST},{_KS_NORTH}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "f": "geojson",
    }
    response = httpx.get(soil_service.KS_PLSS_URL, params=params, timeout=30)
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
                "source": "Kansas PLSS / KGS ArcGIS",
                "source_reference": trs,
                "boundary_geojson": mapping(candidate),
                "centroid_lat": centroid.y,
                "centroid_lon": centroid.x,
                "confidence": 0.75,
                "notes": "Kansas-constrained PLSS section used to georeference a mapped field.",
            }

    raise LookupError(f"Kansas PLSS section not found inside Kansas for {trs}")


# Patch the function on the actual module object before the application imports
# any parser code. The NAU mapped-SOI parser imports this function at parse time.
soil_service.resolve_kansas_plss = resolve_kansas_plss
