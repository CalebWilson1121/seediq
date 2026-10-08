from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ai_service import run_ai_task
from auth_service import admin_overview, create_dealer_user, current_user, dealer_demo_dashboard, dealer_detail, dealer_salesperson_profile, dealer_team, login, logout, require_role, reset_dealer_team_user_password, set_dealer_access, set_dealer_team_user_access, set_user_access, update_dealer_team_user
from catalog_service import import_catalog, list_catalogs, list_organizations, list_products, publish_catalog
from context_builder import build_farm_context
from crop_plan_service import apply_soi_crop_rotation, list_field_plans, rotate_farm, rotate_field, select_seed, set_crop
from database import backend_name, connect, init_db, row_to_dict, rows_to_dicts
from ingestion import ingest_file
from pricing_service import calculated_farmer_price, create_price_override, get_farmer_profile, latest_field_price_request, list_dealer_prices, list_price_requests, review_price_request, upsert_dealer_price, upsert_farmer_profile
from seed_engine import rank_seeds
from soil_service import enrich_field, enrich_prospect, prospect_soil_status

BASE = Path(__file__).parent
SESSION_COOKIE = "seediq_session"
app = FastAPI(title="AcreFit Seed Sales Platform", version="0.8.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class LoginRequest(BaseModel):
    email: str
    password: str

class AccessRequest(BaseModel):
    enabled: bool

class TeamUserRequest(BaseModel):
    email: str
    display_name: str
    role: str = "salesperson"
    sales_enabled: bool = True

class TeamUserUpdateRequest(BaseModel):
    email: str
    display_name: str
    sales_enabled: bool | None = None

class ActivityHeartbeatRequest(BaseModel):
    path: str | None = None

class AITaskRequest(BaseModel):
    task_type: str = "farm_summary"
    instruction: str = "Summarize the farm for an agronomist."
    field_id: int | None = None

class SeedRankRequest(BaseModel):
    field_profile: dict[str, float]
    seeds: list[dict]

class CropRequest(BaseModel):
    crop_year: int
    crop: str | None = None

class FarmDefaultsRequest(BaseModel):
    tillage: str | None = None
    row_spacing: str = "NORMAL"
    planting_window: str = "NORMAL"

class ProspectCreateRequest(BaseModel):
    farm_name: str
    main_contact: str
    state: str
    county: str
    organization_id: int = 2
    assigned_salesperson_id: int | None = None

class RotateRequest(BaseModel):
    from_year: int
    to_year: int
    field_ids: list[int] | None = None

class SeedSelectionRequest(BaseModel):
    crop_year: int
    seed_product_id: int | None = None
    target_population: int | None = None
    notes: str | None = None


class DealerSeedPriceRequest(BaseModel):
    crop_year: int
    seed_product_id: int
    list_price: float | None = None
    base_price: float
    dealer_cost: float | None = None

class FarmerPricingProfileRequest(BaseModel):
    crop_year: int
    volume_discount_pct: float = 0
    early_pay_discount_pct: float = 0
    loyalty_discount_per_unit: float = 0
    custom_discount_per_unit: float = 0
    pricing_tier: str | None = None
    notes: str | None = None

class PriceOverrideRequest(BaseModel):
    crop_year: int
    requested_price: float
    request_note: str | None = None

class PriceReviewRequest(BaseModel):
    decision: str
    approved_price: float | None = None
    review_note: str | None = None

@app.on_event("startup")
def startup() -> None:
    init_db()

def _database_status() -> str:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return "connected"
    except Exception as exc:
        print(f"AcreFit database connection error: {type(exc).__name__}: {exc}", flush=True)
        return "error"

def _user(request: Request):
    if os.getenv("ACREFIT_DEMO_MODE") == "1":
        demo_role = request.cookies.get("acrefit_demo_role")
        demo_ids = {"super_admin": 1, "dealer_admin": 2, "salesperson": 3}
        demo_user_id = demo_ids.get(demo_role or "")
        if demo_user_id:
            with connect() as conn:
                row = conn.execute(
                    "SELECT u.id,u.email,u.display_name,u.global_role,u.status,u.organization_id,u.sales_enabled,"
                    "o.name AS organization_name,o.status AS organization_status,o.access_enabled "
                    "FROM platform_users u LEFT JOIN dealer_organizations o ON o.id=u.organization_id WHERE u.id=?",
                    (demo_user_id,),
                ).fetchone()
            if row and row["status"] == "active":
                result = dict(row)
                result["role"] = _role(result)
                return result
    return current_user(request.cookies.get(SESSION_COOKIE))

def _require(request: Request, *roles: str):
    try:
        return require_role(_user(request), *roles)
    except PermissionError as exc:
        raise HTTPException(status_code=401 if str(exc) == "Login required" else 403, detail=str(exc)) from exc


def _role(user: dict) -> str:
    if user.get("global_role") == "super_admin":
        return "super_admin"
    if user.get("global_role") == "dealer_admin":
        return "dealer_admin"
    return "salesperson"


def _resolve_salesperson(conn, user: dict, organization_id: int, requested_id: int | None = None) -> int:
    role = _role(user)
    if role == "salesperson":
        if int(user.get("organization_id") or 0) != int(organization_id):
            raise HTTPException(status_code=403, detail="Salespeople can only create records in their own dealership")
        if not bool(user.get("sales_enabled")):
            raise HTTPException(status_code=403, detail="This account does not have an active sales book")
        return int(user["id"])
    if requested_id is not None:
        target = conn.execute(
            "SELECT id,organization_id,global_role,status,sales_enabled FROM platform_users WHERE id=?",
            (requested_id,),
        ).fetchone()
        if not target or target["status"] != "active":
            raise HTTPException(status_code=400, detail="Assigned salesperson is unavailable")
        if target["global_role"] not in ("dealer_user", "dealer_admin") or not bool(target["sales_enabled"]):
            raise HTTPException(status_code=400, detail="Assigned user does not have an active sales book")
        if int(target["organization_id"] or 0) != int(organization_id):
            raise HTTPException(status_code=403, detail="Assigned salesperson must belong to the same dealership")
        return int(target["id"])
    if role == "dealer_admin":
        if bool(user.get("sales_enabled")):
            return int(user["id"])
        target = conn.execute(
            "SELECT id FROM platform_users WHERE organization_id=? AND status='active' AND sales_enabled=true "
            "ORDER BY CASE WHEN global_role='dealer_admin' THEN 0 ELSE 1 END,id LIMIT 1",
            (organization_id,),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=400, detail="Choose or add an active salesperson before creating this prospect")
        return int(target["id"])
    if role == "super_admin":
        target = conn.execute(
            "SELECT id FROM platform_users WHERE organization_id=? AND status='active' AND sales_enabled=true AND global_role IN ('dealer_admin','dealer_user') "
            "ORDER BY CASE WHEN global_role='dealer_admin' THEN 0 ELSE 1 END,id LIMIT 1",
            (organization_id,),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=400, detail="This dealership has no active seller to own the prospect")
        return int(target["id"])
    return int(user["id"])


def _dealer_org_for_user(user: dict, requested_organization_id: int | None = None) -> int:
    role = _role(user)
    if role == "super_admin":
        if requested_organization_id is None:
            raise HTTPException(status_code=400, detail="Select a dealer organization")
        return int(requested_organization_id)
    organization_id = user.get("organization_id")
    if organization_id is None:
        raise HTTPException(status_code=403, detail="No dealership is assigned to this account")
    if requested_organization_id is not None and int(requested_organization_id) != int(organization_id):
        raise HTTPException(status_code=403, detail="You can only access your own dealership")
    return int(organization_id)


def _catalog_org(conn, catalog_id: int) -> int:
    row = conn.execute("SELECT organization_id FROM seed_catalogs WHERE id=?", (catalog_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Catalog not found")
    return int(row["organization_id"])


def _prospect_scope(user: dict, scope: str | None = None, requested_organization_id: int | None = None) -> tuple[str, tuple]:
    role = _role(user)
    if role == "super_admin":
        if scope == "mine":
            return " WHERE p.assigned_salesperson_id=?", (int(user["id"]),)
        if requested_organization_id is not None:
            return " WHERE p.organization_id=?", (int(requested_organization_id),)
        return "", ()
    if user.get("organization_id") is None:
        raise HTTPException(status_code=403, detail="No dealership is assigned to this account")
    organization_id = int(user["organization_id"])
    if requested_organization_id is not None and int(requested_organization_id) != organization_id:
        raise HTTPException(status_code=403, detail="You can only access your own dealership")
    if role == "dealer_admin" and scope != "mine":
        return " WHERE p.organization_id=?", (organization_id,)
    return " WHERE p.organization_id=? AND p.assigned_salesperson_id=?", (organization_id, int(user["id"]))


def _require_prospect_access(conn, prospect_id: int, user: dict):
    row = conn.execute(
        "SELECT id,organization_id,assigned_salesperson_id FROM prospects WHERE id=?",
        (prospect_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prospect not found")
    role = _role(user)
    if role == "super_admin":
        return row
    if int(row["organization_id"] or 0) != int(user.get("organization_id") or 0):
        raise HTTPException(status_code=404, detail="Prospect not found")
    if role == "salesperson" and int(row["assigned_salesperson_id"] or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="Prospect not found")
    return row

def _require_farm_access(conn, farm_id: int, user: dict):
    row = conn.execute(
        "SELECT id,organization_id,assigned_salesperson_id FROM farms WHERE id=?",
        (farm_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Farm not found")
    role = _role(user)
    if role == "super_admin":
        return row
    if int(row["organization_id"] or 0) != int(user.get("organization_id") or 0):
        raise HTTPException(status_code=404, detail="Farm not found")
    if role == "salesperson" and int(row["assigned_salesperson_id"] or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="Farm not found")
    return row


def _require_field_access(conn, field_id: int, user: dict):
    row = conn.execute(
        "SELECT f.id,f.farm_id,fa.organization_id,fa.assigned_salesperson_id "
        "FROM fields f JOIN farms fa ON fa.id=f.farm_id WHERE f.id=?",
        (field_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Field not found")
    role = _role(user)
    if role == "super_admin":
        return row
    if int(row["organization_id"] or 0) != int(user.get("organization_id") or 0):
        raise HTTPException(status_code=404, detail="Field not found")
    if role == "salesperson" and int(row["assigned_salesperson_id"] or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="Field not found")
    return row


def _require_aph_match_access(conn, match_id: int, user: dict):
    row = conn.execute(
        "SELECT m.id,m.farm_id,fa.organization_id,fa.assigned_salesperson_id "
        "FROM aph_unit_matches m JOIN farms fa ON fa.id=m.farm_id WHERE m.id=?",
        (match_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="APH unit match not found")
    _require_farm_access(conn, int(row["farm_id"]), user)
    return row


def _require_proposal_access(conn, proposal_id: int, user: dict):
    row = conn.execute(
        "SELECT sp.id,sp.prospect_id,p.organization_id,p.assigned_salesperson_id "
        "FROM seed_proposals sp JOIN prospects p ON p.id=sp.prospect_id WHERE sp.id=?",
        (proposal_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Proposal not found")
    _require_prospect_access(conn, int(row["prospect_id"]), user)
    return row


def _enforce_record_path_access(path: str, user: dict) -> None:
    checks = (
        (r"^/api/farms/(\d+)(?:/|$)", _require_farm_access),
        (r"^/api/fields/(\d+)(?:/|$)", _require_field_access),
        (r"^/api/prospects/(\d+)(?:/|$)", _require_prospect_access),
        (r"^/api/aph-matches/(\d+)(?:/|$)", _require_aph_match_access),
        (r"^/api/proposals/(\d+)(?:/|$)", _require_proposal_access),
    )
    for pattern, checker in checks:
        match = re.match(pattern, path)
        if match:
            with connect() as conn:
                checker(conn, int(match.group(1)), user)
            return

# Demo/production safety boundary. AcreFit's app data is server-rendered through
# FastAPI/Postgres, so protect the application surface even where an individual
# legacy route has not yet added a role decorator. Farmer proposal share links
# remain intentionally public.
_PUBLIC_EXACT_PATHS = {
    "/login.html",
    "/api/auth/login",
    "/api/auth/demo-role/super_admin",
    "/api/auth/demo-role/dealer_admin",
    "/api/auth/demo-role/salesperson",
    "/api/auth/logout",
    "/api/health",
    "/farmer-proposal",
    "/farmer-proposal.html",
    "/api/farmer-proposal-page",
    "/styles.css",
    "/app.js",
    "/favicon.ico",
    "/acrefit-logo.svg",
    "/acrefit-logo-light.svg",
    "/acrefit-icon.svg",
}
_PUBLIC_PREFIXES = (
    "/api/public/proposals/",
)


@app.middleware("http")
async def require_app_session(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_EXACT_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
        return await call_next(request)

    user = _user(request)
    if user:
        if path.startswith("/api/"):
            try:
                _enforce_record_path_access(path, user)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Login required"})

    target = "/login.html"
    if request.url.query:
        # Keep redirects simple and non-sensitive; the login page can return the
        # user to the app home after authentication.
        target += "?next=app"
    return RedirectResponse(url=target, status_code=303)

@app.get("/api/health")
def health():
    storage_ready = bool(os.getenv("SUPABASE_URL") and (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY")))
    db_status = _database_status()
    return {
        "status": "ok" if db_status == "connected" else "degraded",
        "architecture": "super-admin-dealer-team-catalogs-aph-mbar-fields-crops-soils-seed-sales",
        "version": "0.8.0",
        "database": backend_name(),
        "database_status": db_status,
        "storage": "supabase" if storage_ready else "local",
        "ai": "openai" if os.getenv("OPENAI_API_KEY") else "mock",
        "access_control": "session-auth + dealer on/off + user on/off + dealer-managed team",
        "seed_catalogs": "annual dealer catalogs",
        "soil_source": "USDA NRCS SSURGO / Soil Data Access",
        "field_source": "MBAR + exact-boundary override",
    }

@app.post("/api/auth/login")
def auth_login(req: LoginRequest, response: Response):
    try:
        result = login(req.email, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    response.set_cookie(SESSION_COOKIE, result.pop("token"), max_age=7*24*3600, httponly=True, secure=True, samesite="lax", path="/")
    return result

@app.post("/api/auth/demo-role/{role}")
def auth_demo_role(role: str):
    if os.getenv("ACREFIT_DEMO_MODE") != "1":
        raise HTTPException(status_code=404, detail="Not found")
    destinations = {
        "super_admin": "/admin.html",
        "dealer_admin": "/dealer-demo.html",
        "salesperson": "/pipeline.html",
    }
    destination = destinations.get(role)
    if not destination:
        raise HTTPException(status_code=404, detail="Unknown demo role")
    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie("acrefit_demo_role", role, max_age=12*3600, httponly=True, secure=True, samesite="lax", path="/")
    return response

@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie("acrefit_demo_role", path="/")
    return {"ok": True}

@app.get("/api/auth/me")
def auth_me(request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user

@app.get("/api/admin/overview")
def get_admin_overview(request: Request):
    _require(request, "super_admin")
    return admin_overview()

@app.get("/api/admin/dealers/{organization_id}")
def get_admin_dealer(organization_id: int, request: Request):
    _require(request, "super_admin")
    try:
        return dealer_detail(organization_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.put("/api/admin/dealers/{organization_id}/access")
def admin_dealer_access(organization_id: int, req: AccessRequest, request: Request):
    user = _require(request, "super_admin")
    try:
        return set_dealer_access(organization_id, req.enabled, int(user["id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.put("/api/admin/users/{user_id}/access")
def admin_user_access(user_id: int, req: AccessRequest, request: Request):
    user = _require(request, "super_admin")
    try:
        return set_user_access(user_id, req.enabled, int(user["id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/api/dealer/dashboard")
def dealer_dashboard(request: Request, crop_year: int = 2027, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin", "dealer_user")
    org_id = _dealer_org_for_user(user, organization_id)
    salesperson_user_id = int(user["id"]) if _role(user) == "salesperson" else None
    return dealer_demo_dashboard(org_id, salesperson_user_id, crop_year)


@app.get("/api/dealer/pricing")
def dealer_pricing(request: Request, crop_year: int = 2027, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin", "dealer_user")
    org_id = _dealer_org_for_user(user, organization_id)
    rows = list_dealer_prices(org_id, crop_year)
    if _role(user) == "salesperson":
        for row in rows:
            row.pop("dealer_cost", None)
    return {"organization_id": org_id, "crop_year": crop_year, "prices": rows}

@app.put("/api/dealer/pricing")
def dealer_pricing_update(req: DealerSeedPriceRequest, request: Request, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin")
    org_id = _dealer_org_for_user(user, organization_id)
    try:
        return upsert_dealer_price(org_id, req.crop_year, req.seed_product_id, req.list_price, req.base_price, req.dealer_cost)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

@app.get("/api/farms/{farm_id}/pricing-profile")
def farm_pricing_profile(farm_id: int, crop_year: int, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with connect() as conn:
        _require_farm_access(conn, farm_id, user)
    try:
        return get_farmer_profile(farm_id, crop_year)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.put("/api/farms/{farm_id}/pricing-profile")
def farm_pricing_profile_update(farm_id: int, req: FarmerPricingProfileRequest, request: Request):
    user = _require(request, "super_admin", "dealer_admin")
    with connect() as conn:
        farm = _require_farm_access(conn, farm_id, user)
    if _role(user) != "super_admin" and int(farm["organization_id"]) != int(user["organization_id"]):
        raise HTTPException(status_code=403, detail="You can only manage pricing for your dealership")
    return upsert_farmer_profile(
        farm_id, req.crop_year, req.volume_discount_pct, req.early_pay_discount_pct,
        req.loyalty_discount_per_unit, req.custom_discount_per_unit, req.pricing_tier,
        req.notes, int(user["id"]),
    )

@app.get("/api/fields/{field_id}/pricing")
def field_pricing(field_id: int, crop_year: int, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with connect() as conn:
        access = _require_field_access(conn, field_id, user)
        plan = conn.execute(
            "SELECT cp.*,sp.product_name,sp.brand FROM field_crop_plans cp "
            "LEFT JOIN seed_products sp ON sp.id=cp.selected_seed_product_id "
            "WHERE cp.field_id=? AND cp.crop_year=?",
            (field_id, crop_year),
        ).fetchone()
    if not plan or not plan.get("selected_seed_product_id"):
        return {"field_id": field_id, "crop_year": crop_year, "available": False, "reason": "Select a seed product first"}
    standard = calculated_farmer_price(int(access["farm_id"]), crop_year, int(plan["selected_seed_product_id"]))
    if _role(user) == "salesperson":
        standard.pop("dealer_cost", None)
    return {
        "field_id": field_id,
        "crop_year": crop_year,
        "plan": row_to_dict(plan),
        "standard_pricing": standard,
        "latest_request": latest_field_price_request(field_id, crop_year),
    }

@app.post("/api/fields/{field_id}/price-override")
def field_price_override(field_id: int, req: PriceOverrideRequest, request: Request):
    user = _require(request, "dealer_user", "dealer_admin")
    with connect() as conn:
        _require_field_access(conn, field_id, user)
    try:
        return create_price_override(field_id, req.crop_year, req.requested_price, int(user["id"]), req.request_note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/api/dealer/price-approvals")
def dealer_price_approvals(request: Request, status: str | None = "pending", organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin")
    org_id = _dealer_org_for_user(user, organization_id)
    return {"organization_id": org_id, "requests": list_price_requests(org_id, status)}

@app.post("/api/dealer/price-approvals/{request_id}/review")
def dealer_price_approval_review(request_id: int, req: PriceReviewRequest, request: Request, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin")
    org_id = _dealer_org_for_user(user, organization_id)
    try:
        return review_price_request(request_id, org_id, int(user["id"]), req.decision, req.approved_price, req.review_note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/api/dealer/team")
def get_dealer_team(request: Request, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin")
    org_id = _dealer_org_for_user(user, organization_id)
    try:
        return dealer_team(org_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.post("/api/dealer/team")
def add_dealer_team_user(req: TeamUserRequest, request: Request):
    user = _require(request, "dealer_admin")
    if user.get("organization_id") is None:
        raise HTTPException(status_code=400, detail="No dealer organization is assigned to this account")
    try:
        return create_dealer_user(int(user["organization_id"]), req.email, req.display_name, req.role, int(user["id"]), req.sales_enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.put("/api/dealer/team/{user_id}/access")
def dealer_team_user_access(user_id: int, req: AccessRequest, request: Request):
    user = _require(request, "dealer_admin")
    if user.get("organization_id") is None:
        raise HTTPException(status_code=400, detail="No dealer organization is assigned to this account")
    try:
        return set_dealer_team_user_access(int(user["organization_id"]), user_id, req.enabled, int(user["id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/dealer/team/{user_id}/reset-password")
def dealer_team_reset_password(user_id: int, request: Request, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin")
    org_id = _dealer_org_for_user(user, organization_id)
    try:
        return reset_dealer_team_user_password(org_id, user_id, int(user["id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/dealer/team/{user_id}/profile")
def dealer_team_profile(user_id: int, request: Request, crop_year: int = 2027, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin")
    org_id = _dealer_org_for_user(user, organization_id)
    try:
        return dealer_salesperson_profile(org_id, user_id, crop_year)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.put("/api/dealer/team/{user_id}/profile")
def dealer_team_profile_update(user_id: int, req: TeamUserUpdateRequest, request: Request, organization_id: int | None = None):
    user = _require(request, "super_admin", "dealer_admin")
    org_id = _dealer_org_for_user(user, organization_id)
    try:
        return update_dealer_team_user(org_id, user_id, req.display_name, req.email, int(user["id"]), req.sales_enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.post("/api/activity/heartbeat")
def activity_heartbeat(req: ActivityHeartbeatRequest, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    token = request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        raise HTTPException(status_code=401, detail="Login required")
    token_hash = __import__("hashlib").sha256(token.encode()).hexdigest()
    path = (req.path or "")[:240]
    with connect() as conn:
        row = conn.execute(
            "SELECT id,last_seen_at FROM user_activity_sessions WHERE user_id=? AND session_token_hash=? ORDER BY id DESC LIMIT 1",
            (int(user["id"]), token_hash),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE user_activity_sessions SET "
                "active_seconds=active_seconds + CASE WHEN last_seen_at >= CURRENT_TIMESTAMP - INTERVAL '90 seconds' THEN 60 ELSE 0 END,"
                "page_views=page_views+1,last_seen_at=CURRENT_TIMESTAMP,last_path=? WHERE id=?",
                (path, int(row["id"])),
            )
        else:
            conn.execute(
                "INSERT INTO user_activity_sessions(user_id,session_token_hash,page_views,last_path) VALUES(?,?,1,?)",
                (int(user["id"]), token_hash, path),
            )
    return {"ok": True}

@app.post("/api/documents/upload")
async def upload_document(file: Annotated[UploadFile, File(...)], request: Request, document_type: Annotated[str | None, Form()] = None, reprocess: Annotated[bool, Form()] = False, target_farm_id: Annotated[int | None, Form()] = None):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    organization_id = assigned_salesperson_id = created_by_user_id = None
    if target_farm_id is not None:
        with connect() as conn:
            _require_farm_access(conn, int(target_farm_id), user)
    if target_farm_id is None:
        if _role(user) == "super_admin":
            organization_id = 1
            assigned_salesperson_id = int(user["id"])
            created_by_user_id = int(user["id"])
        else:
            if user.get("organization_id") is None:
                raise HTTPException(status_code=403, detail="No dealership is assigned to this account")
            organization_id = int(user["organization_id"])
            with connect() as conn:
                assigned_salesperson_id = _resolve_salesperson(conn, user, organization_id)
            created_by_user_id = int(user["id"])
    suffix = Path(file.filename or "upload.bin").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        tmp.write(await file.read())
        temp_path = Path(tmp.name)
    try:
        return ingest_file(
            temp_path,
            file.filename or "upload.bin",
            document_type,
            reprocess=reprocess,
            target_farm_id=target_farm_id,
            organization_id=organization_id,
            assigned_salesperson_id=assigned_salesperson_id,
            created_by_user_id=created_by_user_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"AcreFit upload error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=400, detail=f"The document could not be imported: {str(exc)[:220]}") from exc
    finally:
        temp_path.unlink(missing_ok=True)

@app.get("/api/dealers")
def dealers(request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if _role(user) == "super_admin":
        return list_organizations()
    org_id = _dealer_org_for_user(user)
    return [x for x in list_organizations() if int(x["id"]) == org_id]

@app.get("/api/dealers/{organization_id}/catalogs")
def dealer_catalogs(organization_id: int, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    _dealer_org_for_user(user, organization_id)
    return list_catalogs(organization_id)

@app.post("/api/dealers/{organization_id}/catalogs/upload")
async def upload_seed_catalog(organization_id: int, request: Request, file: Annotated[UploadFile, File(...)], crop_year: Annotated[int, Form()], catalog_name: Annotated[str, Form()], brand: Annotated[str | None, Form()] = None):
    user = _require(request, "super_admin", "dealer_admin")
    _dealer_org_for_user(user, organization_id)
    suffix = Path(file.filename or "catalog.bin").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        tmp.write(await file.read())
        temp_path = Path(tmp.name)
    try:
        return import_catalog(temp_path, file.filename or "catalog.bin", organization_id, crop_year, catalog_name, brand)
    except Exception as exc:
        print(f"AcreFit catalog upload error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=400, detail=f"Seed catalog could not be imported: {str(exc)[:220]}") from exc
    finally:
        temp_path.unlink(missing_ok=True)

@app.get("/api/catalogs/{catalog_id}/products")
def catalog_products(catalog_id: int, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with connect() as conn:
        org_id = _catalog_org(conn, catalog_id)
    _dealer_org_for_user(user, org_id)
    return list_products(catalog_id)

@app.post("/api/catalogs/{catalog_id}/publish")
def catalog_publish(catalog_id: int, request: Request):
    user = _require(request, "super_admin", "dealer_admin")
    with connect() as conn:
        org_id = _catalog_org(conn, catalog_id)
    _dealer_org_for_user(user, org_id)
    try:
        return publish_catalog(catalog_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/api/farms")
def list_farms(request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    try:
        role = _role(user)
        with connect() as conn:
            if role == "super_admin":
                rows = conn.execute("SELECT * FROM farms ORDER BY updated_at DESC").fetchall()
            elif role == "dealer_admin":
                rows = conn.execute(
                    "SELECT * FROM farms WHERE organization_id=? ORDER BY updated_at DESC",
                    (int(user["organization_id"]),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM farms WHERE organization_id=? AND assigned_salesperson_id=? ORDER BY updated_at DESC",
                    (int(user["organization_id"]), int(user["id"])),
                ).fetchall()
        return rows_to_dicts(rows)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Farm database is temporarily unavailable.") from exc

@app.get("/api/farms/{farm_id}/context")
def farm_context(farm_id: int):
    try:
        return build_farm_context(farm_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/farms/{farm_id}/defaults")
def update_farm_defaults(farm_id: int, req: FarmDefaultsRequest):
    tillage = (req.tillage or "").upper().strip() or None
    row_spacing = (req.row_spacing or "NORMAL").upper().strip()
    planting_window = (req.planting_window or "NORMAL").upper().strip()
    valid_tillage = {None, "NO_TILL", "STRIP_TILL", "MIN_TILL", "CONVENTIONAL"}
    valid_rows = {"NORMAL", "15_IN", "20_IN", "30_IN", "TWIN_ROW"}
    valid_windows = {"EARLY", "NORMAL", "LATE"}
    if tillage not in valid_tillage:
        raise HTTPException(status_code=400, detail="Tillage must be No-Till, Strip-Till, Min-Till, or Conventional")
    if row_spacing not in valid_rows:
        raise HTTPException(status_code=400, detail="Invalid row spacing")
    if planting_window not in valid_windows:
        raise HTTPException(status_code=400, detail="Invalid planting window")
    with connect() as conn:
        farm = conn.execute("SELECT id FROM farms WHERE id=?", (farm_id,)).fetchone()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        conn.execute(
            "UPDATE farms SET default_tillage=?,default_row_spacing=?,default_planting_window=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (tillage, row_spacing, planting_window, farm_id),
        )
        updated = conn.execute(
            "SELECT id,farm_name,producer_name,default_tillage,default_row_spacing,default_planting_window FROM farms WHERE id=?",
            (farm_id,),
        ).fetchone()
    return row_to_dict(updated)

@app.get("/api/farms/{farm_id}/crop-plans")
def get_crop_plans(farm_id: int, crop_year: int):
    return {"farm_id": farm_id, "crop_year": crop_year, "fields": list_field_plans(farm_id, crop_year)}


@app.post("/api/farms/{farm_id}/crop-plans/apply-soi")
def apply_soi_crop_plan(farm_id: int, crop_year: int):
    return apply_soi_crop_rotation(farm_id, crop_year)

@app.put("/api/fields/{field_id}/crop")
def assign_crop(field_id: int, req: CropRequest):
    try:
        return set_crop(field_id, req.crop_year, req.crop)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.post("/api/fields/{field_id}/rotate")
def rotate_one(field_id: int, req: RotateRequest):
    try:
        return rotate_field(field_id, req.from_year, req.to_year)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.post("/api/farms/{farm_id}/rotate")
def rotate_whole_farm(farm_id: int, req: RotateRequest):
    return rotate_farm(farm_id, req.from_year, req.to_year, req.field_ids)

@app.put("/api/fields/{field_id}/seed-selection")
def save_seed_selection(field_id: int, req: SeedSelectionRequest):
    return select_seed(field_id, req.crop_year, req.seed_product_id, req.target_population, req.notes)

@app.post("/api/prospects")
def create_prospect(req: ProspectCreateRequest, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    farm_name = (req.farm_name or "").strip()
    main_contact = (req.main_contact or "").strip()
    state = (req.state or "").strip().upper()
    county = (req.county or "").strip()
    if not farm_name:
        raise HTTPException(status_code=400, detail="Farm Name is required")
    if not main_contact:
        raise HTTPException(status_code=400, detail="Main Contact is required")
    if len(state) != 2:
        raise HTTPException(status_code=400, detail="State must be a 2-letter abbreviation")
    if not county:
        raise HTTPException(status_code=400, detail="County is required")

    role = _role(user)
    if role == "super_admin":
        organization_id = int(req.organization_id)
    else:
        if user.get("organization_id") is None:
            raise HTTPException(status_code=403, detail="No dealership is assigned to this account")
        organization_id = int(user["organization_id"])

    farm_key = f"manual:{uuid.uuid4().hex}"
    try:
        with connect() as conn:
            org = conn.execute(
                "SELECT id FROM dealer_organizations WHERE id=? AND status IN ('active','trial') AND access_enabled=true",
                (organization_id,),
            ).fetchone()
            if not org:
                raise HTTPException(status_code=400, detail="Dealer organization is unavailable")
            assigned_salesperson_id = _resolve_salesperson(conn, user, organization_id, req.assigned_salesperson_id)
            created_by_user_id = int(user["id"])
            if backend_name() == "supabase-postgres":
                farm = conn.execute(
                    "INSERT INTO farms(farm_key,farm_name,producer_name,state,county,organization_id,assigned_salesperson_id,created_by_user_id,default_row_spacing,default_planting_window) VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
                    (farm_key, farm_name, main_contact, state, county, organization_id, assigned_salesperson_id, created_by_user_id, "NORMAL", "NORMAL"),
                ).fetchone()
                farm_id = int(farm["id"])
                prospect = conn.execute(
                    "INSERT INTO prospects(farm_id,prospect_name,status,source,total_acres,crops_json,metadata_json,organization_id,assigned_salesperson_id,created_by_user_id) VALUES(?,?,?,?,?,'[]'::jsonb,'{\"created_from\":\"scratch\"}'::jsonb,?,?,?) RETURNING id",
                    (farm_id, farm_name, "new", "manual_create", 0, organization_id, assigned_salesperson_id, created_by_user_id),
                ).fetchone()
                prospect_id = int(prospect["id"])
            else:
                raise HTTPException(status_code=503, detail="Multi-user prospect ownership requires the production database")
        return {
            "id": prospect_id,
            "farm_id": farm_id,
            "prospect_name": farm_name,
            "farm_name": farm_name,
            "main_contact": main_contact,
            "state": state,
            "county": county,
            "source": "manual_create",
            "assigned_salesperson_id": assigned_salesperson_id,
            "created_by_user_id": created_by_user_id,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Prospect could not be created: {str(exc)[:220]}") from exc


@app.get("/api/prospects")
def list_prospects(request: Request, scope: str | None = None, organization_id: int | None = None):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    try:
        where_sql, params = _prospect_scope(user, scope, organization_id)
        with connect() as conn:
            rows = conn.execute(
                "SELECT p.*, f.producer_name, f.farm_name, "
                "u.display_name AS assigned_salesperson_name, u.email AS assigned_salesperson_email, "
                "(SELECT COUNT(*) FROM fields x WHERE x.farm_id=p.farm_id) AS unit_count, "
                "(SELECT COUNT(*) FROM field_soils fs JOIN fields x ON x.id=fs.field_id WHERE x.farm_id=p.farm_id) AS soil_ready_count "
                "FROM prospects p JOIN farms f ON f.id=p.farm_id "
                "LEFT JOIN platform_users u ON u.id=p.assigned_salesperson_id" +
                where_sql +
                " ORDER BY p.updated_at DESC",
                params,
            ).fetchall()
        result = rows_to_dicts(rows)
        for row in result:
            for key in ("crops_json", "metadata_json"):
                if isinstance(row.get(key), str):
                    try:
                        row[key] = json.loads(row[key])
                    except Exception:
                        pass
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Prospect database is temporarily unavailable.") from exc


@app.get("/api/prospects/{prospect_id}")
def get_prospect(prospect_id: int, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with connect() as conn:
        _require_prospect_access(conn, prospect_id, user)
        row = conn.execute(
            "SELECT p.*, f.producer_name, f.farm_name, u.display_name AS assigned_salesperson_name "
            "FROM prospects p JOIN farms f ON f.id=p.farm_id "
            "LEFT JOIN platform_users u ON u.id=p.assigned_salesperson_id WHERE p.id=?",
            (prospect_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prospect not found")
    prospect = row_to_dict(row) or {}
    for key in ("crops_json", "metadata_json"):
        if isinstance(prospect.get(key), str):
            try:
                prospect[key] = json.loads(prospect[key])
            except Exception:
                pass
    prospect["farm_context"] = build_farm_context(int(prospect["farm_id"]))
    prospect["soil_status"] = prospect_soil_status(prospect_id)
    return prospect


@app.get("/api/prospects/{prospect_id}/soils")
def get_prospect_soils(prospect_id: int, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with connect() as conn:
        _require_prospect_access(conn, prospect_id, user)
    try:
        return prospect_soil_status(prospect_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/prospects/{prospect_id}/soils/enrich")
def enrich_prospect_soils(prospect_id: int, request: Request, force: bool = False):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    with connect() as conn:
        _require_prospect_access(conn, prospect_id, user)
    try:
        return enrich_prospect(prospect_id, force=force)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Soil enrichment failed: {str(exc)[:220]}") from exc


@app.post("/api/fields/{field_id}/soils/enrich")
def enrich_one_field(field_id: int, force: bool = False):
    try: return enrich_field(field_id, force=force)
    except Exception as exc: raise HTTPException(status_code=400, detail=f"Field soil enrichment failed: {str(exc)[:220]}") from exc

@app.post("/api/farms/{farm_id}/ai")
def ai_task(farm_id: int, req: AITaskRequest):
    try: return run_ai_task(farm_id, req.task_type, req.instruction, req.field_id)
    except Exception as exc: raise HTTPException(status_code=503, detail="AI task could not be completed.") from exc

@app.post("/api/seed/rank")
def seed_rank(req: SeedRankRequest):
    return {"ranked": rank_seeds(req.field_profile, req.seeds), "engine_version": "seed-fit-v0.1"}

@app.api_route("/", methods=["GET", "HEAD"])
def root(): return FileResponse(BASE / "index.html")

@app.api_route("/{page_name}.html", methods=["GET", "HEAD"])
def html_page(page_name: str):
    allowed = {"index", "login", "admin", "dealer-demo", "salesperson-profile", "farmers", "field-analysis", "whole-farm-plan", "genetics", "prospects", "prospect-detail", "sales-packet", "pipeline", "product-spec", "data-hub"}
    if page_name not in allowed: raise HTTPException(status_code=404)
    return FileResponse(BASE / f"{page_name}.html")

@app.api_route("/{asset_name}", methods=["GET", "HEAD"])
def static_asset(asset_name: str):
    if asset_name not in {"styles.css", "app.js", "activity.js", "acrefit-logo.svg", "acrefit-logo-light.svg", "acrefit-icon.svg"}: raise HTTPException(status_code=404)
    return FileResponse(BASE / asset_name)
