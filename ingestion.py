from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from aph_utils import aph_identity, practice_bucket
from database import backend_name, connect, json_dumps
from models import ParsedDocument
from parsers import PARSER_VERSION, parse_document

UPLOAD_DIR = Path(__file__).with_name("uploads")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _farm_key(parsed: ParsedDocument, sha: str) -> str:
    base = (parsed.farm_name or parsed.producer_name or "farm").strip().lower()
    return f"{base}:{sha[:10]}" if base == "farm" else base.replace(" ", "-")


def _value_columns(value: Any) -> tuple[str | None, float | None]:
    text = None if value is None else str(value)
    try:
        numeric = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        numeric = None
    return text, numeric


def _storage_enabled() -> bool:
    return bool(os.getenv("SUPABASE_URL") and (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY")))


def _store_source_document(temp_path: Path, original_name: str, sha: str) -> str:
    safe_name = Path(original_name).name.replace("/", "_")
    object_path = f"source/{sha[:2]}/{sha}-{safe_name}"
    if _storage_enabled():
        base = os.environ["SUPABASE_URL"].rstrip("/")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY")
        url = f"{base}/storage/v1/object/seediq-documents/{quote(object_path, safe='/')}"
        headers = {
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Content-Type": mimetypes.guess_type(original_name)[0] or "application/octet-stream",
            "x-upsert": "false",
        }
        with temp_path.open("rb") as handle:
            response = httpx.post(url, headers=headers, content=handle.read(), timeout=60)

        # Supabase Storage may report an existing object as HTTP 400 while the
        # JSON payload carries code/statusCode 409 and "Duplicate". During a
        # parser reprocess that is expected: the immutable source PDF is already
        # safely stored at this SHA-derived path, so reuse it instead of failing.
        duplicate = response.status_code == 409
        if response.status_code == 400:
            try:
                payload = response.json()
                duplicate = (
                    str(payload.get("statusCode") or payload.get("code") or "") == "409"
                    or str(payload.get("error") or "").lower() == "duplicate"
                    or "already exists" in str(payload.get("message") or "").lower()
                )
            except Exception:
                duplicate = "already exists" in response.text.lower() or '"duplicate"' in response.text.lower()

        if response.status_code not in (200, 201) and not duplicate:
            raise RuntimeError(f"Supabase Storage upload failed ({response.status_code}): {response.text[:300]}")
        return f"supabase://seediq-documents/{object_path}"
    UPLOAD_DIR.mkdir(exist_ok=True)
    stored = UPLOAD_DIR / f"{sha[:16]}-{safe_name}"
    if not stored.exists():
        shutil.copy2(temp_path, stored)
    return str(stored)


def _insert_id(conn, sql: str, params: tuple) -> int:
    if backend_name() == "supabase-postgres":
        row = conn.execute(sql + " RETURNING id", params).fetchone()
        return int(row["id"])
    cur = conn.execute(sql, params)
    return int(cur.lastrowid)


def _upsert_prospect(conn, farm_id: int, document_id: int, parsed: ParsedDocument) -> int | None:
    """APH, MBAR, or mapped SOI can create/enrich a SeedIQ prospect.

    APH contributes production history. MBAR and mapped SOI contribute permanent field identity/geography.
    Mapped SOI insurance values are intentionally not normalized into SeedIQ.
    """
    doc_type = (parsed.document_type or "").upper()
    if doc_type not in {"APH", "MBAR", "SOI"}:
        row = conn.execute("SELECT id FROM prospects WHERE farm_id=?", (farm_id,)).fetchone()
        return int(row["id"]) if row else None

    prospect_name = (parsed.producer_name or parsed.farm_name or "Imported Prospect").strip()
    total_acres = round(sum(float(f.acres or 0) for f in parsed.fields), 2)
    crops = sorted({str(f.crop).strip() for f in parsed.fields if f.crop}) if doc_type == "APH" else []

    metadata: dict[str, Any] = {"last_document_type": doc_type}
    if doc_type == "APH":
        metadata.update({
            "has_aph": True,
            "carrier": "NAU Country" if any((f.metadata or {}).get("carrier") == "NAU Country" for f in parsed.fields) else None,
            "unit_count": len(parsed.fields),
            "aph_year_rows": len(parsed.crop_records),
            "policy_number": parsed.policy_number,
            "location_ready_units": sum(1 for f in parsed.fields if (f.metadata or {}).get("township_range") and (f.metadata or {}).get("section")),
        })
        source = "aph_upload"
    elif doc_type == "MBAR":
        metadata.update({
            "has_mbar": True,
            "mbar_field_count": len(parsed.fields),
            "mbar_geometry_count": sum(1 for f in parsed.fields if (f.metadata or {}).get("boundary_geojson")),
        })
        source = "mbar_upload"
    else:
        metadata.update({
            "has_mapped_soi": True,
            "mapped_soi_field_count": len(parsed.fields),
            "mapped_soi_reference_geometry_count": sum(1 for f in parsed.fields if (f.metadata or {}).get("reference_boundary_geojson")),
            "mapped_soi_geometry_policy": "reference_only",
            "insurance_data_scrubbed": True,
        })
        source = "mapped_soi_upload"

    conn.execute(
        "INSERT INTO prospects(farm_id,source_document_id,prospect_name,status,source,total_acres,crops_json,metadata_json) "
        "VALUES(?,?,?,?,?,?,?::jsonb,?::jsonb) "
        "ON CONFLICT(farm_id) DO UPDATE SET "
        "source_document_id=excluded.source_document_id, "
        "prospect_name=CASE WHEN prospects.prospect_name='Imported Prospect' THEN excluded.prospect_name ELSE prospects.prospect_name END, "
        "total_acres=CASE WHEN excluded.total_acres>0 THEN excluded.total_acres ELSE prospects.total_acres END, "
        "crops_json=CASE WHEN jsonb_array_length(excluded.crops_json)>0 THEN excluded.crops_json ELSE prospects.crops_json END, "
        "metadata_json=prospects.metadata_json || excluded.metadata_json, "
        "updated_at=CURRENT_TIMESTAMP",
        (farm_id, document_id, prospect_name, "new", source, total_acres, json_dumps(crops), json_dumps(metadata)),
    )
    row = conn.execute("SELECT id FROM prospects WHERE farm_id=?", (farm_id,)).fetchone()
    return int(row["id"]) if row else None


def _prepare_reprocess(existing) -> None:
    document_id = int(existing["id"])
    farm_id = int(existing["farm_id"])
    doc_type = str(existing.get("document_type") or "").upper()
    with connect() as conn:
        # Reprocessing a source document must never globally wipe field geometry
        # or soils. Exact MBAR/manual boundaries are permanent field assets, and
        # mapped-SOI raster shapes are only references. Touched MBAR fields are
        # updated individually later in ingestion.
        if doc_type == "APH":
            conn.execute("DELETE FROM aph_unit_matches WHERE source_document_id=?", (document_id,))
        conn.execute("DELETE FROM source_facts WHERE document_id=?", (document_id,))
        conn.execute("DELETE FROM crop_records WHERE source_document_id=?", (document_id,))
        conn.execute("DELETE FROM documents WHERE id=?", (document_id,))



def _norm_practice(value: Any) -> str:
    return practice_bucket(value)


def _aph_match_candidates(parsed_field, mapped_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    meta = parsed_field.metadata or {}
    aph_farm = str(meta.get("fsa_farm_number") or "").strip()
    aph_tract = str(meta.get("fsa_tract_number") or "").strip()
    aph_field = str(meta.get("fsa_field_number") or "").strip()
    aph_acres = float(parsed_field.acres or 0)
    aph_practice = _norm_practice(parsed_field.practice or parsed_field.irrigation)
    out = []
    for mf in mapped_fields:
        score = 0.0
        reasons = []
        exact_ids = 0
        for aph_value, field_key, label in (
            (aph_farm, "farm_number", "farm"),
            (aph_tract, "tract_number", "tract"),
            (aph_field, "field_number", "field"),
        ):
            mapped_value = str(mf.get(field_key) or "").strip()
            if aph_value and mapped_value and aph_value == mapped_value:
                exact_ids += 1
                score += 0.22
                reasons.append(f"FSA {label} match")
        mapped_meta = {}
        try:
            import json
            mapped_meta = mf.get("metadata_json") if isinstance(mf.get("metadata_json"), dict) else json.loads(mf.get("metadata_json") or "{}")
        except Exception:
            mapped_meta = {}
        # APH matching should use the acreage reported by the source map when
        # available. Exact polygon acreage is a planning/geography measurement
        # and can legitimately differ after roads, waterways or pivots are cut.
        identity_acres = float(mapped_meta.get("reported_acres") or mf.get("acres") or 0)
        mapped_acres = float(mf.get("acres") or 0)
        if aph_acres and identity_acres:
            pct = abs(identity_acres - aph_acres) / max(aph_acres, 1)
            if pct <= 0.01:
                score += 0.28; reasons.append("acreage within 1%")
            elif pct <= 0.03:
                score += 0.20; reasons.append("acreage within 3%")
            elif pct <= 0.08:
                score += 0.10; reasons.append("acreage within 8%")
        mapped_practice = _norm_practice(mf.get("irrigation") or mf.get("practice"))
        if aph_practice and mapped_practice and aph_practice == mapped_practice:
            score += 0.12; reasons.append("practice match")
        if parsed_field.county and mf.get("county") and str(parsed_field.county).lower() == str(mf.get("county")).lower():
            score += 0.04; reasons.append("county match")
        out.append({
            "field_id": int(mf["id"]),
            "field_name": mf.get("name"),
            "score": round(min(score, 0.99), 3),
            "exact_fsa_parts": exact_ids,
            "reasons": reasons,
            "mapped_acres": mapped_acres,
            "identity_acres": identity_acres,
            "aph_acres": aph_acres,
        })
    return sorted(out, key=lambda x: (-x["score"], abs(x["identity_acres"] - x["aph_acres"])))


def _norm_location_token(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _norm_section(value: Any) -> str:
    s = re.sub(r"\D+", "", str(value or ""))
    return str(int(s)) if s else ""


def _norm_field_name(value: Any) -> str:
    s = str(value or "").upper()
    s = re.sub(r"\s*[—-]\s*FIELD\s+\d+\s*$", "", s)
    s = re.sub(r"\bFIELD\s+\d+\s*$", "", s)
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _mapped_meta(field: dict[str, Any]) -> dict[str, Any]:
    try:
        import json
        value = field.get("metadata_json")
        return value if isinstance(value, dict) else json.loads(value or "{}")
    except Exception:
        return {}


def _prepare_aph_map_first(conn, farm_id: int, document_id: int, parsed: ParsedDocument) -> tuple[dict[str, int], int]:
    mapped_fields = [dict(r) for r in conn.execute(
        "SELECT id,name,acres,county,state,farm_number,tract_number,field_number,crop,practice,irrigation,metadata_json FROM fields WHERE farm_id=? ORDER BY name",
        (farm_id,),
    ).fetchall()]
    if not mapped_fields:
        raise ValueError("Map the farm before uploading APH. SeedIQ will not create fields from APH.")
    matches: dict[str, int] = {}
    units = 0
    for pf in parsed.fields:
        meta = pf.metadata or {}
        unit_number = str(meta.get("unit") or pf.field_number or pf.name or f"unit-{units+1}")
        unit_key = aph_identity(pf.crop, pf.practice or pf.irrigation, unit_number)
        # Mapped SOI carries the exact crop-insurance unit number attached to
        # each physical FSA field. This is the strongest APH bridge and can map
        # one APH unit to one or many physical fields without relying on acreage.
        direct_unit_fields = []
        for mf in mapped_fields:
            mf_meta = _mapped_meta(mf)
            mapped_unit = str(mf_meta.get("insurance_unit_number") or "").strip()
            if not mapped_unit or mapped_unit != unit_number:
                continue
            mapped_crop = str(mf_meta.get("source_crop") or "").upper().strip()
            aph_crop = str(pf.crop or "").upper().strip()
            if mapped_crop and aph_crop and mapped_crop != aph_crop:
                continue
            mapped_practice = _norm_practice(mf_meta.get("source_practice") or mf.get("practice") or mf.get("irrigation"))
            aph_match_practice = _norm_practice(pf.practice or pf.irrigation)
            if mapped_practice and aph_match_practice and mapped_practice != aph_match_practice:
                continue
            direct_unit_fields.append(int(mf["id"]))

        # Crop rotation can change the current SOI unit number even though the
        # physical FSA fields are the same. Use the APH summary's FSA farm +
        # PLSS section + IRR/NIRR + common farm name as the second identity
        # bridge, then require the mapped-field acreage group to reconcile.
        location_group_fields: list[int] = []
        location_group_acres = 0.0
        location_group_variance_pct = None
        aph_farm_number = str(meta.get("fsa_farm_number") or "").strip()
        aph_tr = _norm_location_token(meta.get("township_range"))
        aph_section = _norm_section(meta.get("section"))
        aph_name = _norm_field_name(meta.get("farm_name") or pf.name)
        aph_practice_bucket = _norm_practice(pf.practice or pf.irrigation)
        aph_acres_value = float(pf.acres or 0)

        if aph_farm_number and aph_tr and aph_section:
            location_candidates = []
            for mf in mapped_fields:
                mf_meta = _mapped_meta(mf)
                if str(mf.get("farm_number") or "").strip() != aph_farm_number:
                    continue
                if _norm_location_token(mf_meta.get("township_range")) != aph_tr:
                    continue
                if _norm_section(mf_meta.get("legal_section")) != aph_section:
                    continue
                mapped_practice_bucket = _norm_practice(
                    mf_meta.get("source_practice") or mf.get("practice") or mf.get("irrigation")
                )
                if aph_practice_bucket and mapped_practice_bucket and aph_practice_bucket != mapped_practice_bucket:
                    continue
                location_candidates.append(mf)

            if aph_name:
                same_name = [mf for mf in location_candidates if _norm_field_name(mf.get("name")) == aph_name]
                if same_name:
                    location_candidates = same_name

            if location_candidates:
                location_group_fields = sorted({int(mf["id"]) for mf in location_candidates})
                location_group_acres = round(sum(float(mf.get("acres") or 0) for mf in location_candidates), 2)
                if aph_acres_value > 0:
                    location_group_variance_pct = abs(location_group_acres - aph_acres_value) / aph_acres_value * 100.0

        candidates = _aph_match_candidates(pf, mapped_fields)
        top = candidates[0] if candidates else None
        auto_field_id = None
        status = "unmatched"
        method = "needs_confirmation"
        confidence = top["score"] if top else 0.0
        confirmed_group_fields: list[int] = []
        if direct_unit_fields:
            direct_unit_fields = sorted(set(direct_unit_fields))
            confirmed_group_fields = direct_unit_fields
            auto_field_id = direct_unit_fields[0] if len(direct_unit_fields) == 1 else None
            status = "confirmed"
            method = "mapped_soi_unit_identity"
            confidence = 0.99
            if auto_field_id is not None:
                matches[unit_key] = auto_field_id
        elif (
            location_group_fields
            and aph_acres_value > 0
            and location_group_variance_pct is not None
            and (
                abs(location_group_acres - aph_acres_value) <= 2.0
                or location_group_variance_pct <= 4.0
            )
        ):
            confirmed_group_fields = location_group_fields
            auto_field_id = location_group_fields[0] if len(location_group_fields) == 1 else None
            status = "confirmed"
            method = "mapped_soi_location_group"
            confidence = 0.96 if location_group_variance_pct <= 2.0 else 0.92
            if auto_field_id is not None:
                matches[unit_key] = auto_field_id
        # FSA identity remains a safe fallback when a carrier map does not
        # expose unit linkage. Acreage alone never silently assigns APH.
        elif top and top.get("exact_fsa_parts", 0) >= 2 and top["score"] >= 0.55:
            auto_field_id = int(top["field_id"])
            status = "confirmed"
            method = "fsa_identity_auto"
            matches[unit_key] = auto_field_id
        match_meta = {
            "crop": pf.crop,
            "practice": pf.practice,
            "aph_acres": pf.acres,
            "fsa_farm_number": meta.get("fsa_farm_number"),
            "fsa_tract_number": meta.get("fsa_tract_number"),
            "fsa_field_number": meta.get("fsa_field_number"),
            "unit_number": unit_number,
            "identity_key": unit_key,
            "mapped_soi_field_ids": direct_unit_fields,
            "mapped_soi_location_field_ids": location_group_fields,
            "mapped_soi_location_acres": location_group_acres or None,
            "mapped_soi_location_variance_pct": round(location_group_variance_pct, 2) if location_group_variance_pct is not None else None,
            "township_range": meta.get("township_range"),
            "section": meta.get("section"),
            "farm_name": meta.get("farm_name"),
            "candidates": candidates[:8],
        }
        conn.execute(
            "INSERT INTO aph_unit_matches(farm_id,source_document_id,unit_key,field_id,match_status,confidence,method,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?::jsonb) ON CONFLICT(source_document_id,unit_key) DO UPDATE SET "
            "field_id=excluded.field_id,match_status=excluded.match_status,confidence=excluded.confidence,method=excluded.method,"
            "metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP",
            (farm_id, document_id, unit_key, auto_field_id, status, confidence, method, json_dumps(match_meta)),
        )
        if confirmed_group_fields:
            match_row = conn.execute(
                "SELECT id FROM aph_unit_matches WHERE source_document_id=? AND unit_key=?",
                (document_id, unit_key),
            ).fetchone()
            if match_row:
                match_id = int(match_row["id"])
                conn.execute("DELETE FROM aph_unit_field_links WHERE match_id=?", (match_id,))
                for fid in confirmed_group_fields:
                    conn.execute(
                        "INSERT INTO aph_unit_field_links(match_id,field_id) VALUES(?,?) ON CONFLICT(match_id,field_id) DO NOTHING",
                        (match_id, fid),
                    )
        units += 1
    return matches, units

def ingest_file(temp_path: Path, original_name: str, forced_type: str | None = None, reprocess: bool = False, target_farm_id: int | None = None) -> dict[str, Any]:
    sha = sha256_file(temp_path)
    with connect() as conn:
        if target_farm_id is not None:
            existing = conn.execute("SELECT * FROM documents WHERE sha256=? AND farm_id=?", (sha, target_farm_id)).fetchone()
        else:
            existing = conn.execute("SELECT * FROM documents WHERE sha256=? ORDER BY id DESC LIMIT 1", (sha,)).fetchone()
        if existing and not reprocess:
            prospect = conn.execute("SELECT id FROM prospects WHERE farm_id=?", (existing["farm_id"],)).fetchone()
            crop_count = conn.execute("SELECT COUNT(*) AS n FROM crop_records WHERE source_document_id=?", (existing["id"],)).fetchone()
            match_count = conn.execute("SELECT COUNT(*) AS n FROM aph_unit_matches WHERE source_document_id=?", (existing["id"],)).fetchone()
            return {
                "duplicate": True,
                "document_id": existing["id"],
                "farm_id": existing["farm_id"],
                "prospect_id": int(prospect["id"]) if prospect else None,
                "status": existing["status"],
                "storage": existing["stored_path"],
                "aph_units_parsed": int(match_count["n"] or 0) if match_count else 0,
                "crop_records_created": int(crop_count["n"] or 0) if crop_count else 0,
                "message": "Already imported for this farm."
            }
    if existing and reprocess:
        _prepare_reprocess(existing)

    parsed, parser_name = parse_document(temp_path, forced_type)
    stored_path = _store_source_document(temp_path, original_name, sha)

    with connect() as conn:
        if target_farm_id is not None:
            farm = conn.execute("SELECT * FROM farms WHERE id=?", (target_farm_id,)).fetchone()
            if not farm:
                raise KeyError(f"Target farm {target_farm_id} not found")
            farm_id = int(farm["id"])
        else:
            farm_key = _farm_key(parsed, sha)
            conn.execute(
                "INSERT INTO farms(farm_key,farm_name,producer_name) VALUES(?,?,?) ON CONFLICT(farm_key) DO UPDATE SET farm_name=COALESCE(excluded.farm_name,farms.farm_name),producer_name=COALESCE(excluded.producer_name,farms.producer_name),updated_at=CURRENT_TIMESTAMP",
                (farm_key, parsed.farm_name or parsed.producer_name or "Imported Farm", parsed.producer_name),
            )
            farm = conn.execute("SELECT * FROM farms WHERE farm_key=?", (farm_key,)).fetchone()
            farm_id = int(farm["id"])

        document_id = _insert_id(conn, "INSERT INTO documents(farm_id,original_name,stored_path,sha256,mime_type,document_type,status,parser_name,parser_version,warnings_json,raw_preview,parsed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)", (farm_id, original_name, stored_path, sha, mimetypes.guess_type(original_name)[0], parsed.document_type, "parsed", parser_name, PARSER_VERSION, json_dumps(parsed.warnings), parsed.raw_preview))

        field_id_by_key: dict[str, int] = {}
        field_boundaries: list[tuple[int, dict]] = []
        aph_units_parsed = 0
        map_first_aph = parsed.document_type == "APH" and target_farm_id is not None
        if map_first_aph:
            field_id_by_key, aph_units_parsed = _prepare_aph_map_first(conn, farm_id, document_id, parsed)
        else:
            for i, f in enumerate(parsed.fields):
                key_parts = [str(x or "") for x in [f.farm_number, f.tract_number, f.field_number]]
                local_key = "|".join(x for x in key_parts if x) or f.name or f"field-{i+1}"
                field_key = f"{farm_id}:{local_key}"
                source_meta = dict(f.metadata or {})
                if f.acres is not None:
                    source_meta["reported_acres"] = float(f.acres)
                    source_meta["reported_acres_source"] = parsed.document_type
                existing_field = conn.execute(
                    "SELECT metadata_json FROM fields WHERE field_key=?",
                    (field_key,),
                ).fetchone()
                existing_meta = {}
                if existing_field:
                    try:
                        import json
                        existing_meta = existing_field.get("metadata_json") if isinstance(existing_field.get("metadata_json"), dict) else json.loads(existing_field.get("metadata_json") or "{}")
                    except Exception:
                        existing_meta = {}
                # Preserve permanent/manual geometry metadata while refreshing
                # map-source identity metadata.
                merged_meta = {**existing_meta, **source_meta}
                conn.execute(
                    "INSERT INTO fields(farm_id,field_key,name,acres,county,state,farm_number,tract_number,field_number,crop,practice,irrigation,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(field_key) DO UPDATE SET name=excluded.name,acres=COALESCE(excluded.acres,fields.acres),county=COALESCE(excluded.county,fields.county),state=COALESCE(excluded.state,fields.state),farm_number=COALESCE(excluded.farm_number,fields.farm_number),tract_number=COALESCE(excluded.tract_number,fields.tract_number),field_number=COALESCE(excluded.field_number,fields.field_number),crop=CASE WHEN excluded.crop IS NULL THEN fields.crop ELSE excluded.crop END,practice=COALESCE(excluded.practice,fields.practice),irrigation=COALESCE(excluded.irrigation,fields.irrigation),metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP",
                    (farm_id, field_key, f.name, f.acres, f.county, f.state, f.farm_number, f.tract_number, f.field_number, f.crop, f.practice, f.irrigation, json_dumps(merged_meta)),
                )
                field_row = conn.execute("SELECT id FROM fields WHERE field_key=?", (field_key,)).fetchone()
                field_id = int(field_row["id"])
                field_id_by_key[local_key] = field_id
                if f.field_number:
                    field_id_by_key[str(f.field_number)] = field_id
                # Only exact MBAR/GIS geometry becomes an authoritative field
                # boundary automatically. Mapped-SOI raster geometry is retained
                # in metadata as a reference locator and must be confirmed or
                # replaced before SSURGO/seed placement uses it.
                if parsed.document_type == "MBAR" and (f.metadata or {}).get("boundary_geojson"):
                    field_boundaries.append((field_id, f.metadata["boundary_geojson"]))

        for r in parsed.crop_records:
            field_id = field_id_by_key.get(r.field_key)
            conn.execute("INSERT INTO crop_records(farm_id,field_id,crop_year,crop,practice,planted_acres,production,yield_value,approved_yield,coverage_level,unit_structure,metadata_json,source_document_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (farm_id, field_id, r.crop_year, r.crop, r.practice, r.planted_acres, r.production, r.yield_value, r.approved_yield, r.coverage_level, r.unit_structure, json_dumps(r.metadata), document_id))

        for fact in parsed.facts:
            vtext, vnum = _value_columns(fact.value)
            conn.execute("INSERT INTO source_facts(farm_id,document_id,entity_type,entity_key,field_name,value_text,value_numeric,unit,source_locator,confidence) VALUES(?,?,?,?,?,?,?,?,?,?)", (farm_id, document_id, fact.entity_type, fact.entity_key, fact.field_name, vtext, vnum, fact.unit, fact.source_locator, fact.confidence))

        prospect_id = _upsert_prospect(conn, farm_id, document_id, parsed)
        if map_first_aph and prospect_id is not None:
            mapped_acres_row = conn.execute(
                "SELECT COALESCE(SUM(acres),0) AS acres FROM fields WHERE farm_id=?",
                (farm_id,),
            ).fetchone()
            mapped_acres = float(mapped_acres_row["acres"] or 0)
            conn.execute(
                "UPDATE prospects SET total_acres=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (round(mapped_acres, 2), prospect_id),
            )

    if parsed.document_type == "MBAR" and field_boundaries:
        from soil_service import set_exact_boundary
        for field_id, boundary in field_boundaries:
            try:
                set_exact_boundary(field_id, boundary)
            except Exception as exc:
                parsed.warnings.append(f"Field {field_id} boundary could not be saved: {exc}")

    return {"duplicate": False, "reprocessed": bool(existing and reprocess), "document_id": document_id, "farm_id": farm_id, "prospect_id": prospect_id, "document_type": parsed.document_type, "parser": parser_name, "fields_created_or_updated": (0 if (parsed.document_type == "APH" and target_farm_id is not None) else len(parsed.fields)), "aph_units_parsed": aph_units_parsed, "crop_records_created": len(parsed.crop_records), "facts_stored": len(parsed.facts), "warnings": parsed.warnings, "storage": stored_path, "database": backend_name()}
