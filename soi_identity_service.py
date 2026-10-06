from __future__ import annotations

import json
import re
from typing import Any

from shapely.geometry import shape, mapping
from shapely.ops import unary_union

from database import json_dumps


def _loads(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def practice_bucket(value: Any) -> str:
    raw = str(value or "").upper().replace("-", "").replace(" ", "")
    if "NIRR" in raw or "NONIRR" in raw:
        return "NIRR"
    if "IRR" in raw:
        return "IRR"
    return raw or "UNKNOWN"


def name_tokens(value: Any) -> list[str]:
    text = str(value or "").upper()
    text = re.sub(r"\s*[—-]\s*FIELD\s+\d+\s*$", "", text)
    text = re.sub(r"\s*\(\d+\)\s*$", "", text)
    text = re.sub(r"\b(?:IRR|NIRR|IRRIGATED|NON\s*IRR(?:IGATED)?)\b", " ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return [x for x in re.sub(r"\s+", " ", text).strip().split(" ") if x]


def name_similarity(a: Any, b: Any) -> float:
    aa, bb = set(name_tokens(a)), set(name_tokens(b))
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def exact_root(value: Any) -> str:
    return re.sub(r"\s*\(\d+\)\s*$", "", str(value or "")).strip()


def store_soi_identities(conn, farm_id: int, document_id: int, parsed) -> int:
    """Store mapped-SOI physical identities without creating duplicate SeedIQ fields."""
    conn.execute("DELETE FROM soi_field_identities WHERE source_document_id=?", (document_id,))
    written = 0
    for i, field in enumerate(parsed.fields):
        meta = dict(field.metadata or {})
        key_parts = [str(field.farm_number or ""), str(field.tract_number or ""), str(field.field_number or "")]
        identity_key = "|".join(key_parts) if any(key_parts) else f"soi-{i+1}"
        conn.execute(
            "INSERT INTO soi_field_identities("
            "farm_id,source_document_id,identity_key,name,reported_acres,farm_number,tract_number,field_number,"
            "reference_boundary_geojson,metadata_json"
            ") VALUES(?,?,?,?,?,?,?,?,?::jsonb,?::jsonb) "
            "ON CONFLICT(source_document_id,identity_key) DO UPDATE SET "
            "name=excluded.name,reported_acres=excluded.reported_acres,farm_number=excluded.farm_number,"
            "tract_number=excluded.tract_number,field_number=excluded.field_number,"
            "reference_boundary_geojson=excluded.reference_boundary_geojson,metadata_json=excluded.metadata_json,"
            "updated_at=CURRENT_TIMESTAMP",
            (
                farm_id, document_id, identity_key, field.name, field.acres,
                field.farm_number, field.tract_number, field.field_number,
                json_dumps(meta.get("reference_boundary_geojson")) if meta.get("reference_boundary_geojson") else None,
                json_dumps(meta),
            ),
        )
        written += 1
    return written


def _latest_soi_identities(conn, farm_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.* FROM soi_field_identities s "
        "JOIN documents d ON d.id=s.source_document_id "
        "WHERE s.farm_id=? AND s.source_document_id=("
        " SELECT d2.id FROM documents d2 WHERE d2.farm_id=? AND upper(d2.document_type)='SOI' "
        " ORDER BY d2.parsed_at DESC NULLS LAST,d2.id DESC LIMIT 1"
        ") ORDER BY s.id",
        (farm_id, farm_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _exact_fields(conn, farm_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT f.id,f.name,f.acres,f.irrigation,f.practice,f.metadata_json,fl.boundary_geojson "
        "FROM fields f "
        "LEFT JOIN LATERAL ("
        " SELECT boundary_geojson FROM field_locations x WHERE x.field_id=f.id AND x.boundary_geojson IS NOT NULL "
        " ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1"
        ") fl ON true WHERE f.farm_id=? ORDER BY f.id",
        (farm_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _matching_membership(identity: dict[str, Any], unit_number: str, crop: str, practice: str) -> dict[str, Any] | None:
    meta = _loads(identity.get("metadata_json"), {})
    memberships = meta.get("insurance_unit_memberships") or []
    if not isinstance(memberships, list):
        memberships = []
    if not memberships and meta.get("insurance_unit_number"):
        memberships = [{
            "unit_number": meta.get("insurance_unit_number"),
            "crop": meta.get("source_crop"),
            "practice": meta.get("source_practice"),
            "management_name": meta.get("management_name") or identity.get("name"),
        }]
    for membership in memberships:
        if str(membership.get("unit_number") or "").strip() != unit_number:
            continue
        mcrop = str(membership.get("crop") or "").upper().strip()
        if mcrop and crop and mcrop != crop:
            continue
        mp = practice_bucket(membership.get("practice"))
        if mp != "UNKNOWN" and practice != "UNKNOWN" and mp != practice:
            continue
        return membership
    return None


def suggest_exact_fields_for_aph(conn, farm_id: int, parsed_field) -> dict[str, Any] | None:
    """Use mapped SOI only as identity/locator; exact SeedIQ geometries remain authoritative."""
    meta = parsed_field.metadata or {}
    unit_number = str(meta.get("unit") or parsed_field.field_number or parsed_field.name or "").strip()
    crop = str(parsed_field.crop or "").upper().strip()
    practice = practice_bucket(parsed_field.practice or parsed_field.irrigation)
    aph_acres = float(parsed_field.acres or 0)

    identities = _latest_soi_identities(conn, farm_id)
    matched = []
    for identity in identities:
        membership = _matching_membership(identity, unit_number, crop, practice)
        if membership:
            identity = dict(identity)
            identity["_membership"] = membership
            matched.append(identity)

    # Rotation/history can use a different unit number. Fall back to printed
    # management name + PLSS/practice to locate the same physical ground.
    if not matched:
        aph_name = str(meta.get("farm_name") or meta.get("management_name") or parsed_field.name or "").strip()
        aph_tr = re.sub(r"[^A-Z0-9]+", "", str(meta.get("township_range") or "").upper())
        aph_section = re.sub(r"\D+", "", str(meta.get("section") or ""))
        for identity in identities:
            imeta = _loads(identity.get("metadata_json"), {})
            itr = re.sub(r"[^A-Z0-9]+", "", str(imeta.get("township_range") or "").upper())
            isec = re.sub(r"\D+", "", str(imeta.get("legal_section") or ""))
            ipractice = practice_bucket(imeta.get("source_practice"))
            iname = imeta.get("management_name") or identity.get("name")
            if aph_tr and itr and aph_tr != itr:
                continue
            if aph_section and isec and str(int(aph_section)) != str(int(isec)):
                continue
            if practice != "UNKNOWN" and ipractice != "UNKNOWN" and practice != ipractice:
                continue
            if aph_name and name_similarity(aph_name, iname) >= 0.60:
                identity = dict(identity)
                identity["_membership"] = {
                    "management_name": iname,
                    "practice": imeta.get("source_practice"),
                    "crop": imeta.get("source_crop"),
                }
                matched.append(identity)

    if not matched:
        return None

    refs = []
    management_names = []
    for identity in matched:
        ref = identity.get("reference_boundary_geojson")
        if ref:
            try:
                refs.append(shape(ref).buffer(0))
            except Exception:
                pass
        membership = identity.get("_membership") or {}
        for value in (membership.get("management_name"), _loads(identity.get("metadata_json"), {}).get("management_name"), identity.get("name")):
            if value:
                management_names.append(str(value))

    exact = []
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], Any]]] = {}
    for row in _exact_fields(conn, farm_id):
        if not row.get("boundary_geojson"):
            continue
        try:
            geom = shape(row["boundary_geojson"]).buffer(0)
        except Exception:
            continue
        if geom.is_empty:
            continue
        row = dict(row)
        row["name_root"] = exact_root(row.get("name"))
        exact.append((row, geom))
        groups.setdefault((row["name_root"].upper(), practice_bucket(row.get("irrigation") or row.get("practice"))), []).append((row, geom))

    candidates = []
    ref_union = unary_union(refs).buffer(0) if refs else None
    if ref_union is not None and not ref_union.is_empty:
        for row, geom in exact:
            row_practice = practice_bucket(row.get("irrigation") or row.get("practice"))
            if practice != "UNKNOWN" and row_practice != "UNKNOWN" and practice != row_practice:
                continue
            if not geom.intersects(ref_union):
                continue
            inter = geom.intersection(ref_union)
            if inter.is_empty or inter.area <= 0:
                continue
            source_cover = inter.area / geom.area if geom.area else 0
            ref_cover = inter.area / ref_union.area if ref_union.area else 0
            if source_cover < 0.10 and ref_cover < 0.03:
                continue
            candidates.append({
                "field_id": int(row["id"]),
                "field_name": row.get("name"),
                "acres": float(row.get("acres") or 0),
                "source_coverage_pct": round(source_cover * 100, 1),
                "reference_coverage_pct": round(ref_cover * 100, 1),
            })
    candidates.sort(key=lambda x: (-x["source_coverage_pct"], -x["reference_coverage_pct"], abs(x["acres"] - aph_acres)))

    selected = [x for x in candidates if x["source_coverage_pct"] >= 55.0]
    selected_by_id = {int(x["field_id"]): x for x in selected}

    strong_roots = set()
    for candidate in selected:
        root = exact_root(candidate.get("field_name"))
        if any(name_similarity(root, nm) >= 0.60 for nm in management_names):
            strong_roots.add(root.upper())

    for (root, root_practice), members in groups.items():
        if practice != "UNKNOWN" and root_practice != "UNKNOWN" and practice != root_practice:
            continue
        similarity = max([name_similarity(root, nm) for nm in management_names] or [0.0])
        group_acres = sum(float(row.get("acres") or 0) for row, _ in members)
        group_diff = abs(group_acres - aph_acres) if aph_acres else 0
        group_pct = group_diff / aph_acres * 100.0 if aph_acres else 0
        include_group = root in strong_roots or (
            similarity >= 0.60 and (not aph_acres or group_diff <= 10.0 or group_pct <= 12.0)
        )
        if not include_group:
            continue
        for row, _ in members:
            fid = int(row["id"])
            if fid not in selected_by_id:
                selected_by_id[fid] = {
                    "field_id": fid,
                    "field_name": row.get("name"),
                    "acres": float(row.get("acres") or 0),
                    "source_coverage_pct": 0.0,
                    "reference_coverage_pct": 0.0,
                    "included_by_management_group": True,
                }

    selected = sorted(selected_by_id.values(), key=lambda x: str(x.get("field_name") or ""))
    selected_acres = round(sum(float(x.get("acres") or 0) for x in selected), 2)
    variance = selected_acres - aph_acres if aph_acres else None
    variance_pct = abs(variance) / aph_acres * 100.0 if aph_acres and variance is not None else None
    max_overlap = max([float(x.get("source_coverage_pct") or 0) for x in candidates] or [0.0])
    best_name = max(
        [name_similarity(exact_root(x.get("field_name")), nm) for x in selected for nm in management_names] or [0.0]
    )

    high = bool(selected) and (
        not aph_acres
        or abs(variance or 0) <= 5.0
        or (variance_pct is not None and variance_pct <= 7.0)
        or (best_name >= 0.60 and max_overlap >= 55.0 and variance_pct is not None and variance_pct <= 20.0)
    )

    reference_geojson = None
    if ref_union is not None and not ref_union.is_empty:
        try:
            reference_geojson = mapping(ref_union)
        except Exception:
            pass

    return {
        "status": "high" if high else "review",
        "method": "mapped_soi_exact_crosswalk",
        "field_ids": [int(x["field_id"]) for x in selected],
        "field_names": [x.get("field_name") for x in selected],
        "selected_acres": selected_acres,
        "aph_acres": aph_acres or None,
        "acre_variance": round(variance, 2) if variance is not None else None,
        "acre_variance_pct": round(variance_pct, 2) if variance_pct is not None else None,
        "management_names": sorted(set(management_names)),
        "soi_identity_ids": [int(x["id"]) for x in matched],
        "reference_geojson": reference_geojson,
        "candidates": candidates[:10],
        "max_overlap_pct": max_overlap,
        "name_similarity": round(best_name, 3),
    }
