from __future__ import annotations

import re
from typing import Any

import httpx
from shapely.geometry import mapping, shape

import soil_service

# Kansas-only PLSS section layer hosted by the Kansas Applied Remote Sensing
# program / University of Kansas. This layer exposes clean numeric TOWNSHIP,
# RANGE and SECTION fields plus direction fields, so we do not have to infer
# ArcGIS display-key formats.
_KS_SECTION_URL = (
    "https://services.kars.geoplatform.ku.edu/arcgis/rest/services/"
    "KansasPLSS/MapServer/0/query"
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


def _dir_matches(value: Any, wanted: str) -> bool:
    if value is None:
        return False
    text = str(value).strip().upper()
    return text == wanted or text.startswith(wanted)


def resolve_kansas_plss(township_range: str, section: str | int) -> dict[str, Any]:
    township, township_dir, range_no, range_dir, sec = _parse_trs(township_range, section)
    params = {
        "where": f"TOWNSHIP={township} AND RANGE={range_no} AND SECTION={sec}",
        "outFields": "LEGAL,TOWNSHIP,RANGE,SECTION,TOWNSHIP_D,RANGE_DIR,MERIDIAN",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    response = httpx.get(_KS_SECTION_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json() or {}
    features = data.get("features") or []

    candidates: list[tuple[Any, dict[str, Any]]] = []
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
        candidates.append((candidate, attrs))

    if not candidates:
        raise LookupError(
            f"Kansas PLSS section not found for T{township}{township_dir} "
            f"R{range_no}{range_dir} S{sec}"
        )

    # Prefer exact direction-field matches from the Kansas layer. If a legacy
    # row has blank direction attributes, fall back to deterministic east/west
    # ordering rather than dropping a valid Kansas section.
    exact = [
        item for item in candidates
        if _dir_matches(item[1].get("TOWNSHIP_D"), township_dir)
        and _dir_matches(item[1].get("RANGE_DIR"), range_dir)
    ]
    if exact:
        chosen, attrs = exact[0]
    else:
        candidates.sort(key=lambda item: item[0].centroid.x)
        chosen, attrs = candidates[-1] if range_dir == "E" else candidates[0]

    centroid = chosen.centroid
    source_ref = attrs.get("LEGAL") or f"T{township}{township_dir}-R{range_no}{range_dir}-S{sec}"

    return {
        "source": "Kansas PLSS / KU KARS",
        "source_reference": source_ref,
        "boundary_geojson": mapping(chosen),
        "centroid_lat": centroid.y,
        "centroid_lon": centroid.x,
        "confidence": 0.95,
        "notes": "Kansas-only KU KARS PLSS section resolved from township/range/section fields.",
    }


# Patch the shared resolver before application/parser imports.
soil_service.resolve_kansas_plss = resolve_kansas_plss
