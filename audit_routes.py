from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException
from shapely.geometry import shape

from database import connect
from ingestion import ingest_file

router = APIRouter()


def _storage_parts(stored_path: str) -> tuple[str, str]:
    prefix = "supabase://"
    if not stored_path.startswith(prefix):
        raise ValueError("Audit source is not in Supabase Storage")
    tail = stored_path[len(prefix):]
    bucket, object_path = tail.split("/", 1)
    return bucket, object_path


@router.get("/api/audit/reprocess-source")
def audit_reprocess_source(source_document_id: int, target_farm_id: int):
    """Audit-branch-only helper for isolated parser/reprocess verification.

    Safety guard: target farm must have an audit:* farm_key. The source document
    is downloaded with the server-side Supabase service key, parsed by the audit
    branch, and ingested into the disposable audit farm.
    """
    with connect() as conn:
        source = conn.execute(
            "SELECT * FROM documents WHERE id=?",
            (source_document_id,),
        ).fetchone()
        target = conn.execute(
            "SELECT id,farm_key,farm_name FROM farms WHERE id=?",
            (target_farm_id,),
        ).fetchone()
    if not source:
        raise HTTPException(status_code=404, detail="Source document not found")
    if not target or not str(target.get("farm_key") or "").startswith("audit:"):
        raise HTTPException(status_code=403, detail="Audit reprocess can only target an audit:* farm")

    bucket, object_path = _storage_parts(str(source["stored_path"]))
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise HTTPException(status_code=500, detail="Server storage credentials unavailable")

    url = f"{base}/storage/v1/object/{quote(bucket, safe='')}/{quote(object_path, safe='/')}"
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    try:
        response = httpx.get(url, headers=headers, timeout=90.0)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Audit source download failed: {str(exc)[:200]}") from exc

    suffix = Path(str(source["original_name"])).suffix or ".pdf"
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(response.content)
            temp_name = handle.name
        try:
            result = ingest_file(
                Path(temp_name),
                str(source["original_name"]),
                str(source["document_type"] or "") or None,
                reprocess=False,
                target_farm_id=int(target_farm_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Audit ingest failed: {type(exc).__name__}: {str(exc)[:500]}",
            ) from exc
        return {
            "audit": True,
            "source_document_id": source_document_id,
            "target_farm_id": target_farm_id,
            "target_farm_name": target.get("farm_name"),
            **result,
        }
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except Exception:
                pass


@router.get("/api/audit/geometry-crosswalk")
def audit_geometry_crosswalk(source_farm_id: int, target_farm_id: int):
    """Compare exact source geometries to mapped-SOI reference shapes.

    Read-only: reports spatial candidates, never writes boundaries.
    """
    with connect() as conn:
        target = conn.execute(
            "SELECT id,farm_key,farm_name FROM farms WHERE id=?",
            (target_farm_id,),
        ).fetchone()
        if not target or not str(target.get("farm_key") or "").startswith("audit:"):
            raise HTTPException(status_code=403, detail="Geometry crosswalk target must be an audit:* farm")

        source_rows = conn.execute(
            "SELECT f.id,f.name,f.acres,fl.boundary_geojson "
            "FROM fields f JOIN LATERAL ("
            " SELECT boundary_geojson FROM field_locations x WHERE x.field_id=f.id "
            " AND x.boundary_geojson IS NOT NULL ORDER BY x.updated_at DESC NULLS LAST,x.id DESC LIMIT 1"
            ") fl ON true WHERE f.farm_id=? ORDER BY f.id",
            (source_farm_id,),
        ).fetchall()
        target_rows = conn.execute(
            "SELECT id,name,acres,metadata_json FROM fields WHERE farm_id=? ORDER BY id",
            (target_farm_id,),
        ).fetchall()

    source = []
    for row in source_rows:
        try:
            geom = shape(row["boundary_geojson"])
            if not geom.is_empty:
                source.append((dict(row), geom.buffer(0)))
        except Exception:
            continue

    results = []
    single_high = 0
    ambiguous = 0
    no_reference = 0
    for raw in target_rows:
        row = dict(raw)
        try:
            import json
            meta = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else json.loads(row.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        ref_json = meta.get("reference_boundary_geojson")
        if not ref_json:
            no_reference += 1
            results.append({
                "target_field_id": int(row["id"]),
                "target_name": row.get("name"),
                "target_acres": row.get("acres"),
                "status": "no_reference",
                "candidates": [],
            })
            continue
        try:
            ref = shape(ref_json).buffer(0)
        except Exception:
            results.append({
                "target_field_id": int(row["id"]),
                "target_name": row.get("name"),
                "target_acres": row.get("acres"),
                "status": "bad_reference",
                "candidates": [],
            })
            continue

        candidates = []
        if not ref.is_empty and ref.area > 0:
            for source_row, source_geom in source:
                if not ref.bounds or not source_geom.intersects(ref):
                    continue
                try:
                    inter = source_geom.intersection(ref)
                except Exception:
                    continue
                if inter.is_empty or inter.area <= 0:
                    continue
                ref_cover = inter.area / ref.area
                source_cover = inter.area / source_geom.area if source_geom.area > 0 else 0
                if ref_cover < 0.03 and source_cover < 0.03:
                    continue
                candidates.append({
                    "source_field_id": int(source_row["id"]),
                    "source_name": source_row.get("name"),
                    "source_acres": source_row.get("acres"),
                    "reference_coverage_pct": round(ref_cover * 100, 1),
                    "source_coverage_pct": round(source_cover * 100, 1),
                    "acre_difference": round(float(source_row.get("acres") or 0) - float(row.get("acres") or 0), 2),
                })
        candidates.sort(
            key=lambda x: (
                -max(x["reference_coverage_pct"], x["source_coverage_pct"]),
                -min(x["reference_coverage_pct"], x["source_coverage_pct"]),
                abs(x["acre_difference"]),
            )
        )
        top = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        status = "review"
        if top:
            top_strength = min(top["reference_coverage_pct"], top["source_coverage_pct"])
            second_strength = min(second["reference_coverage_pct"], second["source_coverage_pct"]) if second else 0
            if top_strength >= 65 and (not second or top_strength - second_strength >= 25):
                status = "high_single"
                single_high += 1
            elif len(candidates) > 1:
                status = "ambiguous"
                ambiguous += 1
        results.append({
            "target_field_id": int(row["id"]),
            "target_name": row.get("name"),
            "target_acres": row.get("acres"),
            "status": status,
            "candidates": candidates[:6],
        })

    return {
        "source_farm_id": source_farm_id,
        "target_farm_id": target_farm_id,
        "source_exact_field_count": len(source),
        "target_field_count": len(target_rows),
        "high_single_count": single_high,
        "ambiguous_count": ambiguous,
        "no_reference_count": no_reference,
        "results": results,
    }
