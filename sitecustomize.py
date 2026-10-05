"""SeedIQ runtime compatibility patches.

This module is imported automatically by CPython's site initialization.  Keep
patches here small and explicit; move them into their owning modules during the
next cleanup pass.

Current patch: Kansas PLSS lookups used to select the first matching PLSS_TRS
record.  PLSS_TRS values are not nationally unique, so that could select the
same township/range/section in Alaska.  Constrain the ArcGIS query to the
Kansas envelope and reject any geometry outside Kansas before field-map
georeferencing uses it.
"""
from __future__ import annotations

from typing import Any

import httpx
from shapely.geometry import mapping, shape

import soil_service


_KANSAS_ENVELOPE = (-102.2, 36.9, -94.5, 40.1)  # west, south, east, north


def _resolve_kansas_plss(township_range: str, section: str | int) -> dict[str, Any]:
    trs = soil_service._ks_trs(township_range, section)
    west, south, east, north = _KANSAS_ENVELOPE
    params = {
        "where": f"PLSS_TYPE='Section' AND PLSS_TRS='{trs}'",
        "outFields": "PLSS_TRS,PLSS_TYPE",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "f": "geojson",
    }
    response = httpx.get(soil_service.KS_PLSS_URL, params=params, timeout=30)
    response.raise_for_status()
    features = (response.json() or {}).get("features") or []

    # Do not trust record order.  PLSS township/range/section identifiers can
    # repeat in other states; only accept a geometry whose centroid is in KS.
    for feature in features:
        geom = feature.get("geometry")
        if not geom:
            continue
        candidate = shape(geom)
        if candidate.is_empty:
            continue
        centroid = candidate.centroid
        if west <= centroid.x <= east and south <= centroid.y <= north:
            return {
                "source": "Kansas PLSS / KGS ArcGIS",
                "source_reference": trs,
                "boundary_geojson": mapping(candidate),
                "centroid_lat": centroid.y,
                "centroid_lon": centroid.x,
                "confidence": 0.60,
                "notes": "Kansas-constrained section boundary resolved from township/range/section. Use an exact field boundary for final placement analysis.",
            }

    raise LookupError(f"Kansas PLSS section not found inside Kansas for {trs}")


soil_service.resolve_kansas_plss = _resolve_kansas_plss
