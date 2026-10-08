from __future__ import annotations

import io
import hashlib
import json
import logging
import math
import os

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from PIL import Image, ImageDraw
from shapely.geometry import shape

from database import connect, row_to_dict

router = APIRouter()
logger = logging.getLogger(__name__)

MAP_WIDTH = 600
MAP_HEIGHT = 360
MAP_SCALE = 2
MAP_PADDING = 44
TILE_SIZE = 512


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
    try:
        geom = shape(geometry)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid field boundary") from exc
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty:
        raise HTTPException(status_code=422, detail="Empty field boundary")
    return geom


def _mercator_xy(lon: float, lat: float):
    lat = max(-85.05112878, min(85.05112878, lat))
    x = (lon + 180.0) / 360.0
    siny = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)
    return x, y


def _lonlat_from_mercator(x: float, y: float):
    lon = x * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def _camera_for_geometry(geom):
    min_lon, min_lat, max_lon, max_lat = geom.bounds
    x1, y2 = _mercator_xy(min_lon, min_lat)
    x2, y1 = _mercator_xy(max_lon, max_lat)
    min_x, max_x = min(x1, x2), max(x1, x2)
    min_y, max_y = min(y1, y2), max(y1, y2)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    usable_w = max(100, MAP_WIDTH - 2 * MAP_PADDING)
    usable_h = max(100, MAP_HEIGHT - 2 * MAP_PADDING)
    zoom_x = math.log2(usable_w / (TILE_SIZE * span_x))
    zoom_y = math.log2(usable_h / (TILE_SIZE * span_y))
    zoom = max(0.0, min(20.0, min(zoom_x, zoom_y)))
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    center_lon, center_lat = _lonlat_from_mercator(center_x, center_y)
    return center_lon, center_lat, zoom, center_x, center_y


def _mapbox_base_url(center_lon: float, center_lat: float, zoom: float):
    token = os.getenv("MAPBOX_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Map imagery is not configured")
    style = os.getenv("MAPBOX_STYLE", "mapbox/satellite-v9").strip("/")
    parts = style.split("/", 1)
    username, style_id = parts if len(parts) == 2 else ("mapbox", "satellite-v9")
    return (
        f"https://api.mapbox.com/styles/v1/{username}/{style_id}/static/"
        f"{center_lon:.7f},{center_lat:.7f},{zoom:.3f},0/"
        f"{MAP_WIDTH}x{MAP_HEIGHT}@2x?logo=true&attribution=true&access_token={token}"
    )


def _screen_point(lon: float, lat: float, center_x: float, center_y: float, zoom: float):
    x, y = _mercator_xy(lon, lat)
    world = TILE_SIZE * (2 ** zoom) * MAP_SCALE
    px = (x - center_x) * world + (MAP_WIDTH * MAP_SCALE) / 2
    py = (y - center_y) * world + (MAP_HEIGHT * MAP_SCALE) / 2
    return (px, py)


def _rings(geom):
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    for poly in polys:
        yield list(poly.exterior.coords), [list(r.coords) for r in poly.interiors]


def _render_field_map(geom):
    center_lon, center_lat, zoom, center_x, center_y = _camera_for_geometry(geom)
    url = _mapbox_base_url(center_lon, center_lat, zoom)
    try:
        response = httpx.get(url, timeout=25.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.warning("Mapbox request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Map image provider unavailable") from exc
    if response.status_code != 200:
        logger.warning("Mapbox returned %s: %s", response.status_code, response.text[:500])
        raise HTTPException(status_code=502, detail="Map image provider returned an error")

    try:
        image = Image.open(io.BytesIO(response.content)).convert("RGBA")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Map image provider returned invalid imagery") from exc

    if image.size != (MAP_WIDTH * MAP_SCALE, MAP_HEIGHT * MAP_SCALE):
        image = image.resize((MAP_WIDTH * MAP_SCALE, MAP_HEIGHT * MAP_SCALE), Image.Resampling.LANCZOS)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    fill = (47, 160, 90, 58)
    outline_shadow = (255, 255, 255, 245)
    outline = (20, 107, 57, 255)

    for exterior, holes in _rings(geom):
        ext = [_screen_point(lon, lat, center_x, center_y, zoom) for lon, lat in exterior]
        if len(ext) >= 3:
            draw.polygon(ext, fill=fill)
            draw.line(ext, fill=outline_shadow, width=10, joint="curve")
            draw.line(ext, fill=outline, width=6, joint="curve")
        for hole in holes:
            pts = [_screen_point(lon, lat, center_x, center_y, zoom) for lon, lat in hole]
            if len(pts) >= 3:
                draw.polygon(pts, fill=(0, 0, 0, 0))
                draw.line(pts, fill=outline_shadow, width=8, joint="curve")
                draw.line(pts, fill=outline, width=4, joint="curve")

    image = Image.alpha_composite(image, overlay).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=91, optimize=True)
    return out.getvalue()


@router.get("/api/public/proposals/{public_token}/fields/{field_id}/map-image")
def public_field_map_image(public_token: str, field_id: int):
    geom = _field_boundary(public_token, field_id)
    boundary_hash = hashlib.sha256(geom.wkb).hexdigest()
    content = None
    try:
        with connect() as conn:
            cached = conn.execute(
                "SELECT image_bytes FROM field_map_cache WHERE field_id=? AND boundary_hash=?",
                (field_id, boundary_hash),
            ).fetchone()
        if cached and cached.get("image_bytes"):
            content = bytes(cached["image_bytes"])
    except Exception:
        content = None

    if content is None:
        content = _render_field_map(geom)
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO field_map_cache(field_id,boundary_hash,image_bytes) VALUES(?,?,?) "
                    "ON CONFLICT(field_id,boundary_hash) DO UPDATE SET image_bytes=excluded.image_bytes,created_at=CURRENT_TIMESTAMP",
                    (field_id, boundary_hash, content),
                )
                conn.execute(
                    "DELETE FROM field_map_cache WHERE field_id=? AND boundary_hash<>?",
                    (field_id, boundary_hash),
                )
        except Exception:
            pass

    return Response(
        content=content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400, s-maxage=604800, stale-while-revalidate=604800",
            "ETag": f'"{boundary_hash}"',
        },
    )
