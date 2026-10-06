from __future__ import annotations

import html
import json
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()

BAYER_BASE = "https://www.cropscience.bayer.us"
HEADERS = {
    "User-Agent": "SeedIQCatalogSync/1.0 (+https://seediq-w5-3abe.vercel.app)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}


def _fetch(url: str) -> httpx.Response:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Bayer fetch failed: {exc}") from exc
    return r


def _next_data(text: str) -> dict[str, Any] | None:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        text,
        re.I | re.S,
    )
    if not m:
        return None
    raw = html.unescape(m.group(1)).strip()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _walk(obj: Any, path: str = "$", out: list[dict[str, Any]] | None = None):
    if out is None:
        out = []
    if isinstance(obj, dict):
        keys = {str(k).lower() for k in obj.keys()}
        productish = any(k in keys for k in ("productname", "product_name", "relative_maturity", "relativematurity", "trait", "brand"))
        if productish:
            out.append({"path": path, "keys": list(obj.keys())[:30], "sample": {k: obj[k] for k in list(obj.keys())[:12]}})
        for k, v in obj.items():
            _walk(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:500]):
            _walk(v, f"{path}[{i}]", out)
    return out


@router.get("/api/admin/channel-catalog-probe")
def channel_catalog_probe(crop: str = "corn"):
    crop = crop.lower()
    if crop not in {"corn", "soybeans"}:
        raise HTTPException(status_code=400, detail="crop must be corn or soybeans")
    url = f"{BAYER_BASE}/{crop}/channel/seed-catalog"
    r = _fetch(url)
    data = _next_data(r.text)
    result = {
        "url": str(r.url),
        "status": r.status_code,
        "content_type": r.headers.get("content-type"),
        "bytes": len(r.content),
        "next_data_found": bool(data),
        "html_head": re.sub(r"\s+", " ", r.text[:500]),
    }
    if data:
        result["next_data_keys"] = list(data.keys())
        result["build_id"] = data.get("buildId")
        pp = ((data.get("props") or {}).get("pageProps") or {})
        result["page_props_keys"] = list(pp.keys()) if isinstance(pp, dict) else []
        candidates = _walk(pp)
        result["candidate_count"] = len(candidates)
        result["candidates"] = candidates[:30]
    return result
