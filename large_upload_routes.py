from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth_service import current_user, require_role
from storage_upload import create_signed_upload, ingest_signed_upload

router = APIRouter()
SESSION_COOKIE = "seediq_session"


class SignedUploadRequest(BaseModel):
    filename: str
    content_type: str | None = None


class ProcessUploadRequest(BaseModel):
    object_path: str
    original_name: str
    document_type: str | None = None
    target_farm_id: int | None = None
    reprocess: bool = False


def _require_dealer(request: Request):
    try:
        user = current_user(request.cookies.get(SESSION_COOKIE))
        return require_role(user, "dealer_admin", "dealer_user", "super_admin")
    except PermissionError as exc:
        raise HTTPException(status_code=401 if str(exc) == "Login required" else 403, detail=str(exc)) from exc


@router.post("/api/uploads/sign")
def sign_large_upload(req: SignedUploadRequest, request: Request):
    _require_dealer(request)
    try:
        return create_signed_upload(req.filename, req.content_type)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not prepare upload: {str(exc)[:220]}") from exc


@router.post("/api/uploads/process")
def process_large_upload(req: ProcessUploadRequest, request: Request):
    _require_dealer(request)
    try:
        return ingest_signed_upload(
            req.object_path,
            req.original_name,
            req.document_type,
            target_farm_id=req.target_farm_id,
            reprocess=req.reprocess,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"The uploaded document could not be imported: {str(exc)[:220]}") from exc
