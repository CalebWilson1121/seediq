from __future__ import annotations

import json
import os
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from database import connect, row_to_dict

router = APIRouter()


def _loads_boundary(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _field_boundary(public_token: str, field_id: int):
    with connect() as conn:
        row = conn.execute(
            "SELECT fl.boundary_geojson "
            "FROM seed_proposals sp "
            "JOIN fields f ON f.farm_id=sp.farm_id "
            "LEFT JOIN field_locations fl ON fl.id=("
            "SELECT x.id FROM field_locations x WHERE x.field_id=f.id "
            "ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) "
            "WHERE sp.public_token=? AND f.id=?",
            (public_token, field_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Field not found")
    boundary = _loads_boundary((row_to_dict(row) or {}).get("boundary_geojson"))
    if not boundary:
        raise HTTPException(status_code=404, detail="Field boundary unavailable")
    geometry = boundary.get("geometry") if boundary.get("type") == "Feature" else boundary
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise HTTPException(status_code=422, detail="Unsupported field boundary")
    return geometry


def _static_image_url(geometry: dict):
    mapbox_key = os.getenv("MAPBOX_TOKEN")
    if not mapbox_key:
        raise HTTPException(status_code=503, detail="Map imagery is not configured")
    style = os.getenv("MAPBOX_STYLE", "mapbox/satellite-v9").strip("/")
    parts = style.split("/", 1)
    username, style_id = parts if len(parts) == 2 else ("mapbox", "satellite-v9")
    feature = {
        "type": "Feature",
        "properties": {
            "stroke": "#146b39",
            "stroke-width": 4,
            "stroke-opacity": 1,
            "fill": "#32a05a",
            "fill-opacity": 0.18,
        },
        "geometry": geometry,
    }
    overlay = quote(json.dumps(feature, separators=(",", ":")), safe="")
    credential = quote(mapbox_key, safe="")
    return (
        f"https://api.mapbox.com/styles/v1/{username}/{style_id}/static/"
        f"geojson({overlay})/auto/600x360@2x?padding=36&logo=true&attribution=true&access_token={credential}"
    )


@router.get("/api/public/proposals/{public_token}/fields/{field_id}/map-image")
def public_field_map_image(public_token: str, field_id: int):
    url = _static_image_url(_field_boundary(public_token, field_id))
    try:
        response = httpx.get(url, timeout=25.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Map image provider unavailable") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Map image provider returned an error")
    media_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
    return Response(
        content=response.content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=604800"},
    )
