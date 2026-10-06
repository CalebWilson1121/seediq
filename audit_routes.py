from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException

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
        result = ingest_file(
            Path(temp_name),
            str(source["original_name"]),
            str(source["document_type"] or "") or None,
            reprocess=False,
            target_farm_id=int(target_farm_id),
        )
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
