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

BAYER_BFF = "https://bff.us-east-1.farmer.bayer.com"
PRODUCT_QUERY = """
query GetSeedProducts($params: SeedCatalogParams!, $pagination: ProductPagination!, $location: LocationParams!) {
  getSeedProducts(params: $params, pagination: $pagination, location: $location) {
    total
    products {
      chu
      chuMin
      chuMax
      adaptedForRegion
      productClassName
      cropType
      hybridPrefix
      hybridSuffix
      imageUrl
      daysToFlower
      maturity
      maturitySort
      newProduct
      seoSlug
      silageProven
      silageReady
      strengthsAndManagement
      trait
      hybridQualities
      characteristics {
        type
        label
        items {
          characteristicId
          characteristicName
          value
        }
      }
    }
  }
}
"""


def _graphql_product_page(crop: str, offset: int = 0, size: int = 100) -> dict[str, Any]:
    variables = {
        "params": {
            "brand": "CHANNEL",
            "crop": crop,
            "productName": None,
            "filters": [],
            "limitedRelease": True,
        },
        "pagination": {"from": offset, "size": size},
        "location": {"locale": "US", "region": None},
    }
    headers = dict(HEADERS)
    headers.update({"Content-Type": "application/json", "Authorization": "Bearer token"})
    try:
        r = httpx.post(
            f"{BAYER_BFF}/graphql",
            headers=headers,
            json={"query": PRODUCT_QUERY, "variables": variables},
            timeout=30.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Bayer GraphQL request failed: {exc}") from exc
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Bayer GraphQL returned HTTP {r.status_code}: {r.text[:300]}")
    try:
        payload = r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Bayer GraphQL returned invalid JSON") from exc
    if payload.get("errors"):
        raise HTTPException(status_code=502, detail=f"Bayer GraphQL error: {payload['errors'][:1]}")
    data = ((payload.get("data") or {}).get("getSeedProducts") or {})
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Bayer GraphQL response missing getSeedProducts")
    return data


def _graphql_all_products(crop: str, size: int = 100) -> tuple[list[dict[str, Any]], int]:
    first = _graphql_product_page(crop, 0, size)
    total = int(first.get("total") or 0)
    products = list(first.get("products") or [])
    offset = len(products)
    while offset < total:
        page = _graphql_product_page(crop, offset, size)
        rows = list(page.get("products") or [])
        if not rows:
            break
        products.extend(rows)
        offset += len(rows)
    deduped: dict[str, dict[str, Any]] = {}
    for row in products:
        slug = str(row.get("seoSlug") or "")
        key = slug or f"{row.get('hybridPrefix')}|{row.get('hybridSuffix')}"
        deduped[key] = row
    return list(deduped.values()), total


def _prepare_graphql_product(raw: dict[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    prefix = str(row.get("hybridPrefix") or "").strip()
    suffix = str(row.get("hybridSuffix") or "").strip()
    row["title"] = prefix or str(row.get("seoSlug") or "").strip()
    row["titleSuffix"] = suffix or None
    return row
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
        "sync_method": "Bayer GraphQL getSeedProducts",
        "raw_strengths": strengths,
        "hybrid_qualities": raw.get("hybridQualities"),
        "characteristics": raw.get("characteristics"),
        "adapted_for_region": raw.get("adaptedForRegion"),
        "product_class_name": raw.get("productClassName"),
        "crop_type": raw.get("cropType"),
        "days_to_flower": raw.get("daysToFlower"),
        "silage_proven": raw.get("silageProven"),
        "silage_ready": raw.get("silageReady"),
        "chu": raw.get("chu"),
        "chu_min": raw.get("chuMin"),
        "chu_max": raw.get("chuMax"),
        "title_suffix": raw.get("titleSuffix") or raw.get("hybridSuffix"),
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


def sync_channel_catalog(organization_id: int = 2, catalog_id: int = 1, crop_year: int = 2027) -> dict[str, Any]:
    """Internal service function for refreshing the Channel master catalog from Bayer."""
    corn_raw, corn_total = _graphql_all_products("CORN")
    soy_raw, soy_total = _graphql_all_products("SOYBEANS")
    corn = [_normalized(_prepare_graphql_product(x), "CORN") for x in corn_raw]
    soy = [_normalized(_prepare_graphql_product(x), "SOYBEANS") for x in soy_raw]
    if len(corn) != corn_total or len(soy) != soy_total:
        raise RuntimeError(
            f"Incomplete Bayer catalog response: corn {len(corn)}/{corn_total}, soybeans {len(soy)}/{soy_total}"
        )
    sync = _sync_rows(corn + soy, organization_id, catalog_id, crop_year)
    return {
        "organization_id": organization_id,
        "catalog_id": catalog_id,
        "crop_year": crop_year,
        "corn_products": len(corn),
        "corn_bayer_total": corn_total,
        "soybean_products": len(soy),
        "soybean_bayer_total": soy_total,
        "total_products": len(corn) + len(soy),
        **sync,
        "source": f"{BAYER_BFF}/graphql",
    }

