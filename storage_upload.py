from __future__ import annotations

import mimetypes
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

from ingestion import ingest_file

BUCKET = "seediq-documents"


def _storage_config() -> tuple[str, str]:
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY") or ""
    if not base or not key:
        raise RuntimeError("Supabase Storage is not configured")
    return base, key


def _safe_name(filename: str) -> str:
    name = Path(filename or "upload.bin").name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "upload.bin"
    return name[:180]


def create_signed_upload(filename: str, content_type: str | None = None) -> dict:
    base, key = _storage_config()
    safe = _safe_name(filename)
    object_path = f"incoming/{uuid.uuid4().hex}/{safe}"
    encoded = quote(f"{BUCKET}/{object_path}", safe="/")
    url = f"{base}/storage/v1/object/upload/sign/{encoded}"
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
    }
    response = httpx.post(url, headers=headers, json={}, timeout=30)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Could not create signed upload URL ({response.status_code}): {response.text[:220]}")
    data = response.json()
    relative = data.get("url") or data.get("signedURL") or data.get("signedUrl")
    if not relative:
        raise RuntimeError("Supabase Storage did not return a signed upload URL")
    signed_url = relative if str(relative).startswith("http") else f"{base}/storage/v1{relative}"
    return {
        "object_path": object_path,
        "signed_url": signed_url,
        "content_type": content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream",
        "expires_in_seconds": 7200,
    }


def _download_private_object(object_path: str, suffix: str) -> Path:
    base, key = _storage_config()
    encoded = quote(f"{BUCKET}/{object_path}", safe="/")
    url = f"{base}/storage/v1/object/authenticated/{encoded}"
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    response = httpx.get(url, headers=headers, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"Uploaded file could not be retrieved ({response.status_code}): {response.text[:220]}")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        tmp.write(response.content)
        return Path(tmp.name)


def _delete_private_object(object_path: str) -> None:
    try:
        base, key = _storage_config()
        encoded = quote(f"{BUCKET}/{object_path}", safe="/")
        url = f"{base}/storage/v1/object/{encoded}"
        headers = {"Authorization": f"Bearer {key}", "apikey": key}
        httpx.delete(url, headers=headers, timeout=30)
    except Exception:
        pass


def ingest_signed_upload(
    object_path: str,
    original_name: str,
    document_type: str | None = None,
    target_farm_id: int | None = None,
    reprocess: bool = False,
    organization_id: int | None = None,
    assigned_salesperson_id: int | None = None,
    created_by_user_id: int | None = None,
) -> dict:
    if not object_path.startswith("incoming/"):
        raise ValueError("Invalid upload object path")
    suffix = Path(original_name or "upload.bin").suffix
    temp_path = _download_private_object(object_path, suffix)
    try:
        result = ingest_file(
            temp_path,
            _safe_name(original_name),
            document_type,
            reprocess=reprocess,
            target_farm_id=target_farm_id,
            organization_id=organization_id,
            assigned_salesperson_id=assigned_salesperson_id,
            created_by_user_id=created_by_user_id,
        )
        result["upload_transport"] = "direct-to-supabase"
        return result
    finally:
        temp_path.unlink(missing_ok=True)
        _delete_private_object(object_path)
