from __future__ import annotations

import html
import json
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

from database import connect, json_dumps

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
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Bayer returned HTTP {r.status_code}")
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


def _find_product_lists(obj: Any, out: list[list[dict[str, Any]]] | None = None):
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "products" and isinstance(v, list):
                rows = [x for x in v if isinstance(x, dict) and x.get("title") and x.get("seoSlug")]
                if rows:
                    out.append(rows)
            _find_product_lists(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_product_lists(v, out)
    return out


def _page_products(text: str) -> list[dict[str, Any]]:
    data = _next_data(text)
    if not data:
        return []
    pp = ((data.get("props") or {}).get("pageProps") or {})
    lists = _find_product_lists(pp)
    if not lists:
        return []
    return max(lists, key=len)


def _trait_text(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("acronym", "code", "shortName", "name", "title", "value", "label"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        vals = [str(v).strip() for v in value.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        return vals[0] if vals else None
    if isinstance(value, list):
        vals = [_trait_text(v) for v in value]
        vals = [v for v in vals if v]
        return ", ".join(dict.fromkeys(vals)) if vals else None
    return str(value)


def _tags(text: str, crop: str) -> list[str]:
    t = text.lower()
    tags: set[str] = set()
    tests = {
        "high_yield": ("high yield", "top end yield", "top-end yield", "yield potential", "performance potential"),
        "broad_acre": ("broad acre", "broadly adapted", "wide adaptation"),
        "drought": ("drought", "dryland", "dry land"),
        "stress": ("stress", "stressed growing", "heat"),
        "standability": ("standability", "late season intactness"),
        "root_strength": ("root strength", "strong roots", "root development"),
        "stalk_strength": ("stalk strength", "strong stalk", "stalks and roots"),
        "no_till": ("no-till", "no till"),
        "narrow_row": ("narrow row", "narrow-row"),
        "phytophthora": ("phytophthora", "prr"),
        "sds": ("sudden death syndrome", "sds"),
        "scn": ("soybean cyst nematode", "scn"),
        "flex": ("flex ear", "ear flex"),
        "semi_flex": ("semi-flex", "semi flex"),
        "higher_population": ("high planting population", "higher planting population", "high densities", "high density"),
        "medium_population": ("medium planting population", "moderate planting population", "moderate planting densities", "medium density"),
        "lower_population": ("low planting population", "lower planting population", "low densities", "low density"),
        "irrigated": ("irrigation", "irrigated"),
        "high_management": ("high management", "added fertility", "responds to management"),
    }
    for tag, phrases in tests.items():
        if any(p in t for p in phrases):
            tags.add(tag)
    if crop == "SOYBEANS" and "standability" in t:
        tags.add("standability")
    return sorted(tags)


def _crawl_crop(crop_path: str, max_pages: int = 30) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base = f"{BAYER_BASE}/{crop_path}/channel/seed-catalog"
    collected: dict[str, dict[str, Any]] = {}
    page_log: list[dict[str, Any]] = []
    urls = [base] + [f"{base}?page={p}" for p in range(1, max_pages + 1)]
    no_new_streak = 0
    for idx, url in enumerate(urls):
        r = _fetch(url)
        rows = _page_products(r.text)
        before = len(collected)
        for row in rows:
            slug = str(row.get("seoSlug") or "").strip()
            if slug:
                collected[slug] = row
        new_count = len(collected) - before
        page_log.append({"page_request": idx, "url": url, "rows": len(rows), "new": new_count, "total": len(collected)})
        if idx > 0:
            if not rows or new_count == 0:
                no_new_streak += 1
            else:
                no_new_streak = 0
            if no_new_streak >= 2:
                break
    return list(collected.values()), page_log


def _normalized(raw: dict[str, Any], crop: str) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    maturity_raw = raw.get("maturity") or raw.get("maturitySort")
    try:
        maturity = float(maturity_raw) if maturity_raw not in (None, "") else None
    except (TypeError, ValueError):
        maturity = None
    trait = _trait_text(raw.get("trait"))
    strengths = raw.get("strengthsAndManagement") or []
    if not isinstance(strengths, list):
        strengths = [str(strengths)]
    strengths = [str(x).strip() for x in strengths if str(x).strip()]
    placement = " ".join(strengths) or None
    slug = str(raw.get("seoSlug") or "").strip()
    source_url = f"{BAYER_BASE}/{'corn' if crop == 'CORN' else 'soybeans'}/channel/{slug}" if slug else None
    tag_text = " ".join([title, trait or "", placement or ""])
    metadata = {
        "tags": _tags(tag_text, crop),
        "source": "Bayer Crop Science live Channel catalog",
        "source_slug": slug,
        "new_product": bool(raw.get("newProduct")),
        "sync_method": "__NEXT_DATA__ catalog pagination",
        "raw_strengths": strengths,
    }
    return {
        "product_name": title,
        "crop": crop,
        "relative_maturity": maturity,
        "trait_package": trait,
        "placement_text": placement,
        "unit_size_seeds": 80000 if crop == "CORN" else 140000,
        "source_url": source_url,
        "metadata_json": json_dumps(metadata),
    }


def _sync_rows(products: list[dict[str, Any]], organization_id: int, catalog_id: int, crop_year: int) -> dict[str, int]:
    inserted = 0
    updated = 0
    with connect() as conn:
        for p in products:
            existing = conn.execute(
                "SELECT id FROM seed_products WHERE organization_id=? AND crop_year=? AND brand='Channel' AND upper(crop)=? AND product_name=? ORDER BY id LIMIT 1",
                (organization_id, crop_year, p["crop"], p["product_name"]),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE seed_products SET catalog_id=?,company='Channel',brand='Channel',relative_maturity=?,trait_package=?,placement_text=?,active=true,unit_size_seeds=?,source_url=?,metadata_json=? WHERE id=?",
                    (catalog_id, p["relative_maturity"], p["trait_package"], p["placement_text"], p["unit_size_seeds"], p["source_url"], p["metadata_json"], existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO seed_products(organization_id,catalog_id,crop_year,crop,brand,company,product_name,relative_maturity,trait_package,placement_text,active,unit_size_seeds,source_url,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (organization_id, catalog_id, crop_year, p["crop"], "Channel", "Channel", p["product_name"], p["relative_maturity"], p["trait_package"], p["placement_text"], True, p["unit_size_seeds"], p["source_url"], p["metadata_json"]),
                )
                inserted += 1
        conn.execute(
            "UPDATE seed_catalogs SET catalog_name=?,brand='Channel',status='published',product_count=(SELECT COUNT(*) FROM seed_products WHERE catalog_id=?),updated_at=CURRENT_TIMESTAMP,published_at=COALESCE(published_at,CURRENT_TIMESTAMP) WHERE id=?",
            (f"Channel Live Catalog — {crop_year}", catalog_id, catalog_id),
        )
    return {"inserted": inserted, "updated": updated}


@router.get("/api/admin/channel-catalog-probe")
def channel_catalog_probe(crop: str = "corn", page: int = 0):
    crop = crop.lower()
    if crop not in {"corn", "soybeans"}:
        raise HTTPException(status_code=400, detail="crop must be corn or soybeans")
    url = f"{BAYER_BASE}/{crop}/channel/seed-catalog" + (f"?page={page}" if page else "")
    r = _fetch(url)
    rows = _page_products(r.text)
    data = _next_data(r.text) or {}
    pp = ((data.get("props") or {}).get("pageProps") or {})
    dehydrated = pp.get("dehydratedState") or {}
    query_debug = []
    for q in dehydrated.get("queries") or []:
        state = q.get("state") or {}
        qdata = state.get("data")
        query_debug.append({
            "queryKey": q.get("queryKey"),
            "queryHash": q.get("queryHash"),
            "data_type": type(qdata).__name__,
            "data_keys": list(qdata.keys()) if isinstance(qdata, dict) else None,
            "data_meta": {k:v for k,v in qdata.items() if k != "products"} if isinstance(qdata, dict) else None,
        })
    return {
        "url": str(r.url),
        "status": r.status_code,
        "bytes": len(r.content),
        "next_data_found": bool(data),
        "build_id": data.get("buildId"),
        "access_token_prefix": str(pp.get("accessToken") or "")[:24],
        "product_count": len(rows),
        "query_debug": query_debug,
        "products": [
            {
                "title": p.get("title"),
                "maturity": p.get("maturity"),
                "trait": _trait_text(p.get("trait")),
                "seoSlug": p.get("seoSlug"),
            }
            for p in rows
        ],
    }


@router.get("/api/admin/channel-catalog-client-discovery")
def channel_catalog_client_discovery():
    src = "/_next/static/chunks/pages/%5Bcrop%5D/%5Bbrand%5D/seed-catalog-74c25bf669a9b3e6.js"
    js = _fetch(BAYER_BASE + src).text
    needles = ["r(58917)", "(0,r(58917)", "58917)", ".$( "]
    hits = []
    for needle in needles:
        at = 0
        while len(hits) < 20:
            p = js.find(needle, at)
            if p < 0:
                break
            hits.append({"needle": needle, "snippet": js[max(0,p-1800):p+3200]})
            at = p + len(needle)
    return {"src": src, "bytes": len(js), "hits": hits}


# Temporary operator endpoint used to seed the live catalog. Remove/lock down after sync.
@router.get("/api/admin/channel-catalog-sync-once")
def channel_catalog_sync_once(organization_id: int = 2, catalog_id: int = 1, crop_year: int = 2027):
    corn_raw, corn_pages = _crawl_crop("corn")
    soy_raw, soy_pages = _crawl_crop("soybeans")
    corn = [_normalized(x, "CORN") for x in corn_raw]
    soy = [_normalized(x, "SOYBEANS") for x in soy_raw]
    sync = _sync_rows(corn + soy, organization_id, catalog_id, crop_year)
    return {
        "organization_id": organization_id,
        "catalog_id": catalog_id,
        "crop_year": crop_year,
        "corn_products": len(corn),
        "soybean_products": len(soy),
        "total_products": len(corn) + len(soy),
        **sync,
        "corn_pages": corn_pages,
        "soybean_pages": soy_pages,
    }
