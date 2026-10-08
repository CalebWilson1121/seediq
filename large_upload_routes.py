from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth_service import current_user, require_role
from database import connect
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


def _new_upload_ownership(user: dict) -> tuple[int, int, int]:
    if user.get("global_role") == "super_admin":
        raise HTTPException(status_code=400, detail="Open a dealer workspace before uploading a new farm")
    organization_id = int(user.get("organization_id") or 0)
    if not organization_id:
        raise HTTPException(status_code=403, detail="No dealership is assigned to this account")
    if user.get("global_role") in ("dealer_user", "dealer_admin") and bool(user.get("sales_enabled")):
        return organization_id, int(user["id"]), int(user["id"])
    if user.get("global_role") == "dealer_admin":
        with connect() as conn:
            seller = conn.execute(
                "SELECT id FROM platform_users WHERE organization_id=? AND status='active' AND sales_enabled=true "
                "ORDER BY CASE WHEN global_role='dealer_admin' THEN 0 ELSE 1 END,id LIMIT 1",
                (organization_id,),
            ).fetchone()
        if seller:
            return organization_id, int(seller["id"]), int(user["id"])
        raise HTTPException(status_code=400, detail="Add or select an active salesperson before uploading a new farm")
    raise HTTPException(status_code=403, detail="This account does not have an active sales book")



def _require_target_farm(user: dict, farm_id: int) -> None:
    with connect() as conn:
        farm = conn.execute(
            "SELECT id,organization_id,assigned_salesperson_id FROM farms WHERE id=?",
            (farm_id,),
        ).fetchone()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    if user.get("global_role") == "super_admin":
        return
    if int(farm["organization_id"] or 0) != int(user.get("organization_id") or 0):
        raise HTTPException(status_code=404, detail="Farm not found")
    if user.get("global_role") == "dealer_user" and int(farm["assigned_salesperson_id"] or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="Farm not found")


@router.post("/api/uploads/sign")
def sign_large_upload(req: SignedUploadRequest, request: Request):
    _require_dealer(request)
    try:
        return create_signed_upload(req.filename, req.content_type)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not prepare upload: {str(exc)[:220]}") from exc


@router.post("/api/uploads/process")
def process_large_upload(req: ProcessUploadRequest, request: Request):
    user = _require_dealer(request)
    try:
        # During parser development, re-uploading the same mapped SOI must rebuild
        # normalized fields/geometry rather than short-circuiting as a duplicate.
        # Auto-detect uploads often arrive with document_type=None, so the filename
        # is also used to recognize the mapped SOI source.
        is_soi = (req.document_type or "").upper() == "SOI" or "SOI" in req.original_name.upper()
        if req.target_farm_id is not None:
            _require_target_farm(user, int(req.target_farm_id))
        ownership = (None, None, None) if req.target_farm_id is not None else _new_upload_ownership(user)
        return ingest_signed_upload(
            req.object_path,
            req.original_name,
            req.document_type,
            target_farm_id=req.target_farm_id,
            reprocess=(req.reprocess or is_soi),
            organization_id=ownership[0],
            assigned_salesperson_id=ownership[1],
            created_by_user_id=ownership[2],
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"The uploaded document could not be imported: {str(exc)[:220]}") from exc
