from __future__ import annotations

import json
import re
from datetime import date
from itertools import combinations
from statistics import mean
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aph_utils import collapse_production_records, match_identity, normalize_crop, practice_bucket, record_identity
from database import connect, json_dumps, rows_to_dicts
from climate_service import enrich_climate_for_fields, enrich_climate_for_farm

router = APIRouter()


class ConfirmAPHMatchRequest(BaseModel):
    field_ids: list[int]


def _loads(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default



def _practice_bucket(value: Any) -> str:
    return practice_bucket(value)


def _field_root(name: Any) -> str:
    s = str(name or "").strip().lower()
    s = re.sub(r"\s+new(?:\s+\d+)?$", "", s)
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    s = re.sub(r"\b(?:irr|nirr|irrigated|non\s*irr(?:igated)?)\b", " ", s)
    s = re.sub(r"\b\d+\b", " ", s)
    s = re.sub(r"[^a-z]+", " ", s)
    return re.sub(r"\s+", " ", s).strip() or str(name or "").strip().lower()


def _name_tokens(value: Any) -> set[str]:
    s = _field_root(value)
    stop = {"farm", "farms", "place", "field", "the"}
    return {x for x in s.split() if x and x not in stop}


def _name_similarity(a: Any, b: Any) -> float:
    aa, bb = _name_tokens(a), _name_tokens(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa <= bb or bb <= aa:
        return 0.9
    return len(aa & bb) / len(aa | bb)


def _field_aliases(field: dict[str, Any]) -> list[str]:
    md = _loads(field.get("metadata_json"), {})
    aliases = md.get("aph_identity_aliases") or []
    if not isinstance(aliases, list):
        aliases = []
    values = [str(field.get("name") or "").strip()]
    values.extend(str(x).strip() for x in aliases if str(x).strip())
    return list(dict.fromkeys(x for x in values if x))



def _group_candidates(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build logical management-field groups from mapped polygons.

    Names like 'Bins (1)', 'Bins (2)', 'Bins (3)' are one group. Single fields
    stay as one-member groups. This is intentionally much faster and safer than
    arbitrary subset search.
    """
    grouped: dict[tuple[str,str], list[dict[str, Any]]] = {}
    for fld in fields:
        practice = _practice_bucket(fld.get("irrigation") or fld.get("practice"))
        root = _field_root(fld.get("name"))
        grouped.setdefault((practice, root), []).append(fld)

    out: list[dict[str, Any]] = []
    for (practice, root), members in grouped.items():
        members = sorted(members, key=lambda x: str(x.get("name") or ""))
        total = round(sum(float(x.get("acres") or 0) for x in members), 2)
        aliases = []
        for member in members:
            aliases.extend(_field_aliases(member))
        aliases = list(dict.fromkeys(x for x in aliases if x))
        out.append({
            "practice": practice,
            "root": root,
            "field_ids": [int(x["id"]) for x in members],
            "field_names": [x.get("name") for x in members],
            "selected_acres": total,
            "identity_aliases": aliases,
        })
    return out


def _aph_auto_suggestions(farm_id: int) -> dict[str, Any]:
    with connect() as conn:
        matches = rows_to_dicts(conn.execute(
            "SELECT * FROM aph_unit_matches WHERE farm_id=? ORDER BY id",
            (farm_id,),
        ).fetchall())
        fields = rows_to_dicts(conn.execute(
            "SELECT id,name,acres,irrigation,practice,farm_number,tract_number,field_number,metadata_json "
            "FROM fields WHERE farm_id=? ORDER BY name",
            (farm_id,),
        ).fetchall())

    groups = _group_candidates(fields)

    # Build candidate rankings independently for every APH unit. Acreage and
    # practice are the main signals; names are only used to keep split polygons
    # together as a management field.
    candidate_map: dict[int, list[dict[str, Any]]] = {}
    for m in matches:
        md = _loads(m.get("metadata_json"), {})
        target_raw = md.get("aph_acres")
        target = float(target_raw) if target_raw not in (None, "") else 0.0
        crop = str(md.get("crop") or "").upper()
        practice = _practice_bucket(md.get("practice"))
        candidates: list[dict[str, Any]] = []

        soi = md.get("soi_crosswalk") if isinstance(md.get("soi_crosswalk"), dict) else None
        if soi and soi.get("field_ids"):
            selected_acres = float(soi.get("selected_acres") or 0)
            diff = selected_acres - target if target else 0.0
            pct = abs(diff) / max(target, 1.0) * 100.0 if target else 0.0
            candidates.append({
                "field_ids": [int(x) for x in (soi.get("field_ids") or [])],
                "field_names": soi.get("field_names") or [],
                "field_root": "Mapped SOI identity",
                "selected_acres": round(selected_acres, 2),
                "aph_acres": round(target, 2) if target else None,
                "variance_acres": round(diff, 2),
                "variance_pct": round(pct, 2),
                "score": 120.0 if soi.get("status") == "high" else 90.0,
                "acreage_score": 100.0 if soi.get("status") == "high" else 75.0,
                "name_similarity": float(soi.get("name_similarity") or 0),
                "aph_management_name": (soi.get("management_names") or [None])[0],
                "identity_aliases": soi.get("management_names") or [],
                "crop": crop,
                "practice": practice,
                "reason": "Mapped SOI identity → exact SeedIQ field crosswalk"
                    + (f"; spatial overlap {float(soi.get('max_overlap_pct') or 0):.0f}%" if soi.get("max_overlap_pct") is not None else ""),
                "source": "mapped_soi_crosswalk",
                "has_reference_geometry": bool(soi.get("reference_geojson")),
                "soi_status": soi.get("status"),
            })

        if target > 0:
            for g in groups:
                if practice != "UNKNOWN" and g["practice"] != practice:
                    continue
                total = float(g["selected_acres"])
                diff = round(total - target, 2)
                abs_diff = abs(diff)
                pct = abs_diff / max(target, 1.0) * 100.0

                # Keep the review list useful. We generally do not care about
                # groups that are wildly different in acreage.
                if abs_diff > max(12.0, target * 0.08):
                    continue

                aph_name = md.get("farm_name") or md.get("management_name") or ""
                name_similarity = max(
                    [_name_similarity(aph_name, alias) for alias in (g.get("identity_aliases") or [])] or [0.0]
                )

                # Acreage tells us whether a group is plausible; management
                # identity tells us whether it is the right group. Exact acreage
                # alone must never silently beat a differently named field.
                if abs_diff <= 0.25:
                    acreage_score = 100.0
                elif abs_diff <= 0.75:
                    acreage_score = 98.0
                elif abs_diff <= 1.50:
                    acreage_score = 95.0
                elif pct <= 1.0:
                    acreage_score = 93.0
                elif pct <= 2.0:
                    acreage_score = 88.0
                elif pct <= 3.5:
                    acreage_score = 80.0
                elif pct <= 5.0:
                    acreage_score = 70.0
                else:
                    acreage_score = 55.0

                generic_aph_name = _field_root(aph_name) in {"", "other ident", "other", "unknown", "nau unit"}
                if generic_aph_name:
                    score = acreage_score * 0.55
                else:
                    score = (name_similarity * 70.0) + (acreage_score * 0.30)

                candidates.append({
                    "field_ids": g["field_ids"],
                    "field_names": g["field_names"],
                    "field_root": g["root"],
                    "selected_acres": round(total, 2),
                    "aph_acres": round(target, 2),
                    "variance_acres": diff,
                    "variance_pct": round(pct, 2),
                    "score": score,
                    "acreage_score": acreage_score,
                    "name_similarity": round(name_similarity, 3),
                    "aph_management_name": aph_name or None,
                    "identity_aliases": g.get("identity_aliases") or [],
                    "crop": crop,
                    "practice": practice,
                    "reason": (
                        f"{g['root'] or 'field group'} totals {total:.2f} ac vs APH {target:.2f} ac"
                        + (f"; management-name match {round(name_similarity * 100)}%" if name_similarity > 0 else "")
                    ),
                })

        deduped = []
        seen_groups = set()
        for candidate in sorted(candidates, key=lambda x: (-x["score"], abs(x["variance_acres"]), len(x["field_ids"]), x["field_root"])):
            key = tuple(sorted(int(x) for x in candidate.get("field_ids") or []))
            if not key or key in seen_groups:
                continue
            seen_groups.add(key)
            deduped.append(candidate)
        candidate_map[int(m["id"])] = deduped[:8]

    # Avoid assigning the same mapped group to two APH units within the same
    # crop/practice. We resolve the most exact/unique acreage matches first.
    by_bucket: dict[tuple[str,str], list[tuple[dict[str,Any],dict[str,Any]]]] = {}
    for m in matches:
        md = _loads(m.get("metadata_json"), {})
        bucket = (str(md.get("crop") or "").upper(), _practice_bucket(md.get("practice")))
        by_bucket.setdefault(bucket, []).append((m, md))

    suggestions: dict[int, dict[str, Any] | None] = {}
    for bucket, items in by_bucket.items():
        remaining = {int(m["id"]): list(candidate_map.get(int(m["id"]), [])) for m, _ in items}
        used_groups: set[tuple[int,...]] = set()

        # Iteratively take the best "most certain" match: lowest acreage
        # variance first, then biggest lead over the second-best candidate.
        while remaining:
            choices = []
            for mid, cands in remaining.items():
                available = [x for x in cands if tuple(sorted(x["field_ids"])) not in used_groups]
                if not available:
                    choices.append((999999.0, 999999.0, mid, None))
                    continue
                top = available[0]
                second = available[1] if len(available) > 1 else None
                lead = (top["score"] - second["score"]) if second else 100.0
                choices.append((abs(top["variance_acres"]), -lead, mid, top))
            choices.sort(key=lambda x: (x[0], x[1]))
            _, _, mid, chosen = choices[0]
            suggestions[mid] = chosen
            if chosen:
                used_groups.add(tuple(sorted(chosen["field_ids"])))
            remaining.pop(mid, None)

    result = []
    for m in matches:
        md = _loads(m.get("metadata_json"), {})
        chosen = suggestions.get(int(m["id"]))
        confidence = "none"
        if chosen:
            abs_diff = abs(float(chosen["variance_acres"]))
            pct = float(chosen["variance_pct"])
            name_similarity = float(chosen.get("name_similarity") or 0)
            aph_name = str(chosen.get("aph_management_name") or "")
            generic_aph_name = _field_root(aph_name) in {"", "other ident", "other", "unknown", "nau unit"}
            if chosen.get("source") == "mapped_soi_crosswalk":
                confidence = "high" if chosen.get("soi_status") == "high" else "review"
            # Safety rule: acreage-only matches are suggestions, never automatic
            # confirmations. High/medium requires corroborating management name
            # (including an alias learned from a prior dealer confirmation).
            elif name_similarity >= 0.90 and (abs_diff <= 10.0 or pct <= 12.0):
                confidence = "high"
            elif name_similarity >= 0.60 and (abs_diff <= 8.0 or pct <= 8.0):
                confidence = "medium"
            elif generic_aph_name and (abs_diff <= 1.0 or pct <= 0.75):
                confidence = "review"
            else:
                confidence = "review"
        result.append({
            "match_id": int(m["id"]),
            "unit_key": m.get("unit_key"),
            "crop": md.get("crop"),
            "practice": md.get("practice"),
            "aph_acres": md.get("aph_acres"),
            "suggestion": chosen,
            "confidence": confidence,
            "alternatives": candidate_map.get(int(m["id"]), [])[:5],
        })

    return {
        "farm_id": farm_id,
        "suggestions": result,
        "high_confidence_count": sum(1 for x in result if x["confidence"] == "high"),
        "medium_confidence_count": sum(1 for x in result if x["confidence"] == "medium"),
        "needs_review_count": sum(1 for x in result if x["confidence"] not in {"high","medium"}),
        "method": "Fast group-first APH matching: mapped split polygons are grouped by field name root; acreage + IRR/NIRR drive the match.",
    }


def _unit_from_record(record: dict[str, Any]) -> str | None:
    meta = _loads(record.get("metadata_json"), {})
    unit = meta.get("unit_number")
    return str(unit) if unit not in (None, "") else None


def _conflicting_assignments(conn, match: dict[str, Any], field_ids: list[int]) -> list[dict[str, Any]]:
    """Find same-document/same-crop/same-practice APH assignments for fields.

    One mapped management field may participate in only one confirmed APH unit
    for a crop/practice within a source document unless we add an explicit,
    reviewed aggregation workflow later.
    """
    if not field_ids:
        return []
    metadata = _loads(match.get("metadata_json"), {})
    target_crop = normalize_crop(metadata.get("crop"))
    target_practice = _practice_bucket(metadata.get("practice"))
    placeholders = ",".join("?" for _ in field_ids)
    rows = rows_to_dicts(conn.execute(
        f"SELECT l.field_id,m.id AS match_id,m.unit_key,m.metadata_json "
        f"FROM aph_unit_field_links l JOIN aph_unit_matches m ON m.id=l.match_id "
        f"WHERE m.source_document_id=? AND m.id<>? AND m.match_status='confirmed' "
        f"AND l.field_id IN ({placeholders})",
        (match["source_document_id"], match["id"], *field_ids),
    ).fetchall())
    conflicts = []
    for row in rows:
        md = _loads(row.get("metadata_json"), {})
        if normalize_crop(md.get("crop")) == target_crop and _practice_bucket(md.get("practice")) == target_practice:
            conflicts.append({
                "field_id": int(row["field_id"]),
                "match_id": int(row["match_id"]),
                "unit_key": row.get("unit_key"),
            })
    return conflicts


def _weather_for_field_year(field_id: int, crop_year: int) -> dict[str, Any]:
    with connect() as conn:
        loc = conn.execute(
            "SELECT centroid_lat,centroid_lon FROM field_locations WHERE field_id=? "
            "ORDER BY updated_at DESC NULLS LAST,id DESC LIMIT 1",
            (field_id,),
        ).fetchone()
    if not loc or loc.get("centroid_lat") is None or loc.get("centroid_lon") is None:
        return {"status": "skipped", "reason": "Field centroid unavailable"}

    lat = float(loc["centroid_lat"])
    lon = float(loc["centroid_lon"])
    start = date(crop_year, 4, 1)
    end = date(crop_year, 10, 15)
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "models": "era5_land",
    }
    try:
        r = httpx.get("https://archive-api.open-meteo.com/v1/archive", params=params, timeout=35.0)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        return {"status": "error", "reason": f"Historical weather unavailable: {str(exc)[:180]}"}

    daily = payload.get("daily") or {}
    highs = [float(x) for x in (daily.get("temperature_2m_max") or []) if x is not None]
    lows = [float(x) for x in (daily.get("temperature_2m_min") or []) if x is not None]
    rain = [float(x) for x in (daily.get("precipitation_sum") or []) if x is not None]
    if not highs or not lows or not rain:
        return {"status": "error", "reason": "Historical weather response was incomplete"}

    n = min(len(highs), len(lows), len(rain))
    highs, lows, rain = highs[:n], lows[:n], rain[:n]
    gdd = 0.0
    for hi, lo in zip(highs, lows):
        bounded_hi = min(86.0, hi)
        bounded_lo = max(50.0, lo)
        gdd += max(0.0, ((bounded_hi + bounded_lo) / 2.0) - 50.0)

    metrics = {
        "precipitation_in": round(sum(rain), 2),
        "avg_max_temp_f": round(mean(highs), 1),
        "avg_min_temp_f": round(mean(lows), 1),
        "heat_days_90": sum(1 for x in highs if x >= 90),
        "heat_days_95": sum(1 for x in highs if x >= 95),
        "dry_days": sum(1 for x in rain if x < 0.01),
        "gdd_base50": round(gdd, 0),
    }
    with connect() as conn:
        conn.execute(
            "INSERT INTO field_year_environment(field_id,crop_year,season_start,season_end,precipitation_in,"
            "avg_max_temp_f,avg_min_temp_f,heat_days_90,heat_days_95,dry_days,gdd_base50,enso_phase,source,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb) "
            "ON CONFLICT(field_id,crop_year) DO UPDATE SET "
            "season_start=excluded.season_start,season_end=excluded.season_end,precipitation_in=excluded.precipitation_in,"
            "avg_max_temp_f=excluded.avg_max_temp_f,avg_min_temp_f=excluded.avg_min_temp_f,"
            "heat_days_90=excluded.heat_days_90,heat_days_95=excluded.heat_days_95,dry_days=excluded.dry_days,"
            "gdd_base50=excluded.gdd_base50,source=excluded.source,metadata_json=excluded.metadata_json,"
            "updated_at=CURRENT_TIMESTAMP",
            (
                field_id, crop_year, start.isoformat(), end.isoformat(),
                metrics["precipitation_in"], metrics["avg_max_temp_f"], metrics["avg_min_temp_f"],
                metrics["heat_days_90"], metrics["heat_days_95"], metrics["dry_days"], metrics["gdd_base50"],
                None, "Open-Meteo ERA5-Land",
                json_dumps({"latitude": lat, "longitude": lon, "weather_model": "era5_land", "enso_status": "pending"}),
            ),
        )
    return {"status": "ready", **metrics}


def _enrich_weather_for_field(field_id: int) -> dict[str, Any]:
    with connect() as conn:
        years = [
            int(r["crop_year"])
            for r in conn.execute(
                "SELECT DISTINCT crop_year FROM crop_records WHERE field_id=? AND crop_year IS NOT NULL ORDER BY crop_year",
                (field_id,),
            ).fetchall()
        ]
    results = []
    for year in years:
        results.append({"crop_year": year, **_weather_for_field_year(field_id, year)})
    return {"field_id": field_id, "years": results}


@router.get("/api/farms/{farm_id}/aph-auto-match")
def aph_auto_match(farm_id: int):
    return _aph_auto_suggestions(farm_id)


class AutoConfirmRequest(BaseModel):
    include_medium: bool = False


@router.post("/api/farms/{farm_id}/aph-auto-match/confirm")
def aph_auto_confirm(farm_id: int, req: AutoConfirmRequest):
    plan = _aph_auto_suggestions(farm_id)
    accepted = []
    skipped = []
    for item in plan["suggestions"]:
        allowed = item["confidence"] == "high" or (req.include_medium and item["confidence"] == "medium")
        suggestion = item.get("suggestion")
        if not allowed or not suggestion:
            skipped.append({"match_id": item["match_id"], "confidence": item["confidence"]})
            continue
        match_id = int(item["match_id"])
        field_ids = [int(x) for x in suggestion["field_ids"]]
        with connect() as conn:
            match = conn.execute("SELECT * FROM aph_unit_matches WHERE id=? AND farm_id=?", (match_id, farm_id)).fetchone()
            if not match:
                continue
            conflicts = _conflicting_assignments(conn, dict(match), field_ids)
            if conflicts:
                skipped.append({
                    "match_id": match_id,
                    "confidence": item["confidence"],
                    "reason": "mapped field already belongs to another APH unit for this crop/practice",
                    "conflicts": conflicts,
                })
                continue
            conn.execute("DELETE FROM aph_unit_field_links WHERE match_id=?", (match_id,))
            for fid in field_ids:
                conn.execute(
                    "INSERT INTO aph_unit_field_links(match_id,field_id) VALUES(?,?) ON CONFLICT(match_id,field_id) DO NOTHING",
                    (match_id, fid),
                )
            primary = field_ids[0] if len(field_ids) == 1 else None
            conn.execute(
                "UPDATE aph_unit_matches SET field_id=?,match_status='confirmed',confidence=?,method='auto_group_match',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (primary, float(suggestion["score"]) / 100.0, match_id),
            )
            recs = rows_to_dicts(conn.execute(
                "SELECT id,crop,practice,metadata_json FROM crop_records WHERE source_document_id=?",
                (match["source_document_id"],),
            ).fetchall())
            target_identity = match_identity(dict(match))
            for rec in recs:
                if record_identity(rec) == target_identity:
                    conn.execute("UPDATE crop_records SET field_id=? WHERE id=?", (primary, rec["id"]))
        accepted.append({
            "match_id": match_id,
            "unit_key": item["unit_key"],
            "field_ids": field_ids,
            "selected_acres": suggestion["selected_acres"],
            "aph_acres": suggestion["aph_acres"],
            "confidence": item["confidence"],
        })
    return {
        "farm_id": farm_id,
        "accepted": accepted,
        "accepted_count": len(accepted),
        "skipped": skipped,
        "message": "Auto-matches saved. Run historical weather refresh after reviewing remaining units.",
    }


@router.get("/api/farms/{farm_id}/aph-matches")
def aph_matches(farm_id: int):
    with connect() as conn:
        matches = rows_to_dicts(conn.execute(
            "SELECT m.*,d.original_name FROM aph_unit_matches m JOIN documents d ON d.id=m.source_document_id "
            "WHERE m.farm_id=? ORDER BY d.uploaded_at DESC,m.unit_key",
            (farm_id,),
        ).fetchall())
        fields = rows_to_dicts(conn.execute(
            "SELECT id,name,acres,crop,practice,irrigation,farm_number,tract_number,field_number FROM fields "
            "WHERE farm_id=? ORDER BY name",
            (farm_id,),
        ).fetchall())
    with connect() as conn:
        links = rows_to_dicts(conn.execute(
            "SELECT l.match_id,l.field_id FROM aph_unit_field_links l "
            "JOIN aph_unit_matches m ON m.id=l.match_id WHERE m.farm_id=? ORDER BY l.match_id,l.field_id",
            (farm_id,),
        ).fetchall())
    link_map: dict[int,list[int]] = {}
    for link in links:
        link_map.setdefault(int(link["match_id"]), []).append(int(link["field_id"]))
    field_acres = {int(f["id"]): float(f.get("acres") or 0) for f in fields}
    for m in matches:
        m["metadata"] = _loads(m.pop("metadata_json", None), {})
        soi = m["metadata"].get("soi_crosswalk")
        if isinstance(soi, dict) and soi.get("reference_geojson"):
            soi = dict(soi)
            soi["has_reference_geometry"] = True
            soi.pop("reference_geojson", None)
            m["metadata"]["soi_crosswalk"] = soi
        ids = link_map.get(int(m["id"]), [])
        if not ids and m.get("field_id"):
            ids = [int(m["field_id"])]
        m["field_ids"] = ids
        m["selected_acres"] = round(sum(field_acres.get(fid,0) for fid in ids),2)
    return {"farm_id": farm_id, "matches": matches, "fields": fields}


@router.get("/api/aph-matches/{match_id}/review-reference")
def aph_match_review_reference(match_id: int):
    with connect() as conn:
        match = conn.execute(
            "SELECT id,farm_id,metadata_json FROM aph_unit_matches WHERE id=?",
            (match_id,),
        ).fetchone()
    if not match:
        raise HTTPException(status_code=404, detail="APH unit match not found")
    metadata = _loads(match.get("metadata_json"), {})
    soi = metadata.get("soi_crosswalk") if isinstance(metadata.get("soi_crosswalk"), dict) else {}
    return {
        "match_id": match_id,
        "farm_id": int(match["farm_id"]),
        "reference_geojson": soi.get("reference_geojson"),
        "field_ids": [int(x) for x in (soi.get("field_ids") or [])],
        "field_names": soi.get("field_names") or [],
        "management_names": soi.get("management_names") or [],
        "aph_acres": soi.get("aph_acres"),
        "selected_acres": soi.get("selected_acres"),
        "acre_variance_pct": soi.get("acre_variance_pct"),
        "max_overlap_pct": soi.get("max_overlap_pct"),
    }


@router.put("/api/aph-matches/{match_id}/confirm")
def confirm_aph_match(match_id: int, req: ConfirmAPHMatchRequest):
    field_ids = sorted({int(x) for x in req.field_ids if int(x) > 0})
    if not field_ids:
        raise HTTPException(status_code=400, detail="Select at least one mapped field")
    with connect() as conn:
        match = conn.execute("SELECT * FROM aph_unit_matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(status_code=404, detail="APH unit match not found")
        placeholders = ",".join("?" for _ in field_ids)
        fields = rows_to_dicts(conn.execute(
            f"SELECT id,farm_id,name,acres FROM fields WHERE id IN ({placeholders})",
            tuple(field_ids),
        ).fetchall())
        if len(fields) != len(field_ids) or any(int(f["farm_id"]) != int(match["farm_id"]) for f in fields):
            raise HTTPException(status_code=400, detail="Every selected field must belong to this farm")

        conflicts = _conflicting_assignments(conn, dict(match), field_ids)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A selected field is already matched to another APH unit for this crop/practice.",
                    "conflicts": conflicts,
                },
            )

        conn.execute("DELETE FROM aph_unit_field_links WHERE match_id=?", (match_id,))
        for fid in field_ids:
            conn.execute(
                "INSERT INTO aph_unit_field_links(match_id,field_id) VALUES(?,?) ON CONFLICT(match_id,field_id) DO NOTHING",
                (match_id,fid),
            )
        primary = field_ids[0] if len(field_ids)==1 else None
        conn.execute(
            "UPDATE aph_unit_matches SET field_id=?,match_status='confirmed',confidence=1.0,method=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (primary, "manual_multi_confirm" if len(field_ids)>1 else "manual_confirm", match_id),
        )

        # Keep crop_records unassigned for multi-field units. History is linked
        # through aph_unit_field_links to avoid duplicating farm production rows.
        records = rows_to_dicts(conn.execute(
            "SELECT id,crop,practice,metadata_json FROM crop_records WHERE source_document_id=?",
            (match["source_document_id"],),
        ).fetchall())
        updated = 0
        target_identity = match_identity(dict(match))
        for rec in records:
            if record_identity(rec) == target_identity:
                conn.execute("UPDATE crop_records SET field_id=? WHERE id=?", (primary, rec["id"]))
                updated += 1

        # Remember a dealer-confirmed management identity on the exact fields.
        # This is intentionally field metadata rather than document-specific
        # matching, so future APH uploads can reuse the relationship.
        match_meta = _loads(match.get("metadata_json"), {})
        identity_alias = str(
            match_meta.get("farm_name") or match_meta.get("management_name") or ""
        ).strip()
        if identity_alias:
            for fid in field_ids:
                row = conn.execute("SELECT metadata_json FROM fields WHERE id=?", (fid,)).fetchone()
                field_meta = _loads(row.get("metadata_json") if row else None, {})
                aliases = field_meta.get("aph_identity_aliases") or []
                if not isinstance(aliases, list):
                    aliases = []
                normalized = {str(x).strip().lower() for x in aliases if str(x).strip()}
                if identity_alias.lower() not in normalized:
                    aliases.append(identity_alias)
                field_meta["aph_identity_aliases"] = aliases[-20:]
                conn.execute(
                    "UPDATE fields SET metadata_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json_dumps(field_meta), fid),
                )

    climate = enrich_climate_for_fields(field_ids)
    return {
        "match_id": match_id,
        "field_ids": field_ids,
        "fields": fields,
        "selected_acres": round(sum(float(f.get("acres") or 0) for f in fields),2),
        "crop_records_linked": updated,
        "climate": climate,
    }


@router.post("/api/fields/{field_id}/history/weather")
def enrich_field_history_weather(field_id: int):
    with connect() as conn:
        field = conn.execute("SELECT id FROM fields WHERE id=?", (field_id,)).fetchone()
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    return enrich_climate_for_fields([field_id])


@router.post("/api/farms/{farm_id}/history/weather")
def enrich_farm_history_weather(farm_id: int):
    # Backward-compatible route; now enriches rainfall + heat + NOAA ENSO.
    return enrich_climate_for_farm(farm_id)


@router.post("/api/farms/{farm_id}/history/climate")
def enrich_farm_history_climate(farm_id: int):
    return enrich_climate_for_farm(farm_id)


@router.get("/api/farms/{farm_id}/production-profile")
def farm_production_profile(farm_id: int):
    with connect() as conn:
        fields = rows_to_dicts(conn.execute(
            "SELECT f.id,f.name,f.acres,f.crop,f.irrigation,fl.centroid_lat,fl.centroid_lon,"
            "fs.dominant_muname,fs.weighted_aws150_cm,fs.weighted_slope_pct,fs.drainage_summary_json "
            "FROM fields f "
            "LEFT JOIN LATERAL (SELECT centroid_lat,centroid_lon FROM field_locations x WHERE x.field_id=f.id "
            "ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1) fl ON true "
            "LEFT JOIN field_soils fs ON fs.field_id=f.id WHERE f.farm_id=? ORDER BY f.name",
            (farm_id,),
        ).fetchall())
        records = rows_to_dicts(conn.execute(
            "SELECT * FROM crop_records WHERE farm_id=? ORDER BY crop_year",
            (farm_id,),
        ).fetchall())
        aph_links = rows_to_dicts(conn.execute(
            "SELECT l.field_id,m.source_document_id,m.unit_key,m.metadata_json "
            "FROM aph_unit_field_links l JOIN aph_unit_matches m ON m.id=l.match_id "
            "WHERE m.farm_id=? AND m.match_status='confirmed'",
            (farm_id,),
        ).fetchall())
        env = rows_to_dicts(conn.execute(
            "SELECT e.* FROM field_year_environment e JOIN fields f ON f.id=e.field_id WHERE f.farm_id=? "
            "ORDER BY e.field_id,e.crop_year",
            (farm_id,),
        ).fetchall())
        match_counts = conn.execute(
            "SELECT count(*) AS total,count(*) FILTER (WHERE match_status='confirmed') AS confirmed "
            "FROM aph_unit_matches WHERE farm_id=?",
            (farm_id,),
        ).fetchone()

    rec_by_field: dict[int, list[dict[str, Any]]] = {}
    linked_keys: dict[tuple[int,str], list[int]] = {}
    for l in aph_links:
        linked_keys.setdefault((int(l["source_document_id"]), match_identity(l)), []).append(int(l["field_id"]))
    for r in records:
        linked = linked_keys.get((int(r["source_document_id"]), record_identity(r)), [])
        if linked:
            for fid in linked:
                rec_by_field.setdefault(fid, []).append(r)
        elif r.get("field_id") is not None:
            rec_by_field.setdefault(int(r["field_id"]), []).append(r)
    env_by_key = {}
    for e in env:
        e = dict(e)
        e["metadata"] = _loads(e.pop("metadata_json", None), {})
        env_by_key[(int(e["field_id"]), int(e["crop_year"]))] = e

    out_fields = []
    for f in fields:
        fid = int(f["id"])
        raw_field_records = rec_by_field.get(fid, [])
        field_records = collapse_production_records(raw_field_records)
        years = []
        yield_vals = []
        for r in field_records:
            yv = r.get("yield_value")
            if yv is not None:
                yield_vals.append(float(yv))
            years.append({
                "crop_year": r.get("crop_year"),
                "crop": r.get("crop"),
                "practice": r.get("practice"),
                "planted_acres": r.get("planted_acres"),
                "production": r.get("production"),
                "yield_value": yv,
                "approved_yield": r.get("approved_yield"),
                "source_record_count": int(r.get("_source_record_count") or 1),
                "environment": env_by_key.get((fid, int(r["crop_year"]))) if r.get("crop_year") is not None else None,
            })
        by_crop: dict[str, dict[str, Any]] = {}
        crop_groups: dict[str, list[float]] = {}
        for row in field_records:
            crop_name = str(row.get("crop") or "UNKNOWN").upper()
            if row.get("yield_value") is not None:
                crop_groups.setdefault(crop_name, []).append(float(row["yield_value"]))
        for crop_name, crop_yields in crop_groups.items():
            crop_avg = round(mean(crop_yields), 1) if crop_yields else None
            if len(crop_yields) >= 2 and crop_avg:
                crop_spread = mean(abs(x - crop_avg) for x in crop_yields)
                crop_stability = round(max(0.0, min(100.0, 100.0 - (crop_spread / crop_avg * 180.0))), 0)
            else:
                crop_stability = None
            by_crop[crop_name] = {
                "year_count": len(crop_yields),
                "average_yield": crop_avg,
                "yield_stability_score": crop_stability,
            }

        # A bushel average across corn and soybeans is not agronomically valid.
        # Preserve a simple top-level summary only when the field has one crop in
        # its usable history; otherwise consumers must use by_crop.
        single_crop = next(iter(by_crop.values())) if len(by_crop) == 1 else None
        out_fields.append({
            **f,
            "drainage": _loads(f.get("drainage_summary_json"), {}),
            "production_summary": {
                "year_count": len(years),
                "average_yield": single_crop.get("average_yield") if single_crop else None,
                "yield_stability_score": single_crop.get("yield_stability_score") if single_crop else None,
                "by_crop": by_crop,
                "ignored_or_collapsed_source_rows": max(0, len(raw_field_records) - len(field_records)),
            },
            "years": years,
        })

    return {
        "farm_id": farm_id,
        "aph_matching": {
            "total_units": int(match_counts["total"] or 0) if match_counts else 0,
            "confirmed_units": int(match_counts["confirmed"] or 0) if match_counts else 0,
        },
        "fields": out_fields,
        "weather_source": "Open-Meteo ERA5 historical reanalysis, Apr 1–Oct 15",
        "enso_source": "NOAA CPC RONI (primary) + ONI; SeedIQ crop-season phase uses mean MJJ/JJA/JAS",
    }
