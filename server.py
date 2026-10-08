from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ai_service import run_ai_task
from auth_service import admin_overview, create_dealer_user, current_user, dealer_demo_dashboard, dealer_detail, dealer_team, login, logout, require_role, set_dealer_access, set_dealer_team_user_access, set_user_access
from catalog_service import import_catalog, list_catalogs, list_organizations, list_products, publish_catalog
from context_builder import build_farm_context
from crop_plan_service import list_field_plans, rotate_farm, rotate_field, select_seed, set_crop
from database import backend_name, connect, init_db, row_to_dict, rows_to_dicts
from ingestion import ingest_file
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
        return int(user["id"])
    if requested_id is not None:
        target = conn.execute(
            "SELECT id,organization_id,global_role,status FROM platform_users WHERE id=?",
            (requested_id,),
        ).fetchone()
        if not target or target["status"] != "active":
            raise HTTPException(status_code=400, detail="Assigned salesperson is unavailable")
        if target["global_role"] != "dealer_user":
            raise HTTPException(status_code=400, detail="Assigned user must be a salesperson")
        if int(target["organization_id"] or 0) != int(organization_id):
            raise HTTPException(status_code=403, detail="Assigned salesperson must belong to the same dealership")
        return int(target["id"])
    if role == "dealer_admin":
        target = conn.execute(
            "SELECT id FROM platform_users WHERE organization_id=? AND global_role='dealer_user' AND status='active' ORDER BY id LIMIT 1",
            (organization_id,),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=400, detail="Add an active salesperson before creating a prospect")
        return int(target["id"])
    return int(user["id"])


def _prospect_scope(user: dict) -> tuple[str, tuple]:
    role = _role(user)
    if role == "super_admin":
        return "", ()
    if user.get("organization_id") is None:
        raise HTTPException(status_code=403, detail="No dealership is assigned to this account")
    if role == "dealer_admin":
        return " WHERE p.organization_id=?", (int(user["organization_id"]),)
    return " WHERE p.organization_id=? AND p.assigned_salesperson_id=?", (int(user["organization_id"]), int(user["id"]))


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

# Demo/production safety boundary. AcreFit's app data is server-rendered through
# FastAPI/Postgres, so protect the application surface even where an individual
# legacy route has not yet added a role decorator. Farmer proposal share links
# remain intentionally public.
_PUBLIC_EXACT_PATHS = {
    "/login.html",
    "/api/auth/login",
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

@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
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
def dealer_dashboard(request: Request):
    user = _require(request, "dealer_admin", "dealer_user")
    if user.get("organization_id") is None:
        raise HTTPException(status_code=400, detail="No dealer organization is assigned to this account")
    return dealer_demo_dashboard(int(user["organization_id"]))

@app.get("/api/dealer/team")
def get_dealer_team(request: Request):
    user = _require(request, "dealer_admin")
    if user.get("organization_id") is None:
        raise HTTPException(status_code=400, detail="No dealer organization is assigned to this account")
    try:
        return dealer_team(int(user["organization_id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.post("/api/dealer/team")
def add_dealer_team_user(req: TeamUserRequest, request: Request):
    user = _require(request, "dealer_admin")
    if user.get("organization_id") is None:
        raise HTTPException(status_code=400, detail="No dealer organization is assigned to this account")
    try:
        return create_dealer_user(int(user["organization_id"]), req.email, req.display_name, req.role, int(user["id"]))
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

@app.post("/api/documents/upload")
async def upload_document(file: Annotated[UploadFile, File(...)], document_type: Annotated[str | None, Form()] = None, reprocess: Annotated[bool, Form()] = False, target_farm_id: Annotated[int | None, Form()] = None):
    suffix = Path(file.filename or "upload.bin").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        tmp.write(await file.read())
        temp_path = Path(tmp.name)
    try:
        return ingest_file(temp_path, file.filename or "upload.bin", document_type, reprocess=reprocess, target_farm_id=target_farm_id)
    except Exception as exc:
        print(f"AcreFit upload error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=400, detail=f"The document could not be imported: {str(exc)[:220]}") from exc
    finally:
        temp_path.unlink(missing_ok=True)

@app.get("/api/dealers")
def dealers():
    return list_organizations()

@app.get("/api/dealers/{organization_id}/catalogs")
def dealer_catalogs(organization_id: int):
    return list_catalogs(organization_id)

@app.post("/api/dealers/{organization_id}/catalogs/upload")
async def upload_seed_catalog(organization_id: int, file: Annotated[UploadFile, File(...)], crop_year: Annotated[int, Form()], catalog_name: Annotated[str, Form()], brand: Annotated[str | None, Form()] = None):
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
def catalog_products(catalog_id: int):
    return list_products(catalog_id)

@app.post("/api/catalogs/{catalog_id}/publish")
def catalog_publish(catalog_id: int):
    try:
        return publish_catalog(catalog_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/api/farms")
def list_farms():
    try:
        with connect() as conn:
            rows = conn.execute("SELECT * FROM farms ORDER BY updated_at DESC").fetchall()
        return rows_to_dicts(rows)
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
def list_prospects(request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    try:
        where_sql, params = _prospect_scope(user)
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
    allowed = {"index", "login", "admin", "dealer-demo", "farmers", "field-analysis", "whole-farm-plan", "genetics", "prospects", "prospect-detail", "sales-packet", "pipeline", "product-spec", "data-hub"}
    if page_name not in allowed: raise HTTPException(status_code=404)
    return FileResponse(BASE / f"{page_name}.html")

@app.api_route("/{asset_name}", methods=["GET", "HEAD"])
def static_asset(asset_name: str):
    if asset_name not in {"styles.css", "app.js", "acrefit-logo.svg", "acrefit-logo-light.svg", "acrefit-icon.svg"}: raise HTTPException(status_code=404)
    return FileResponse(BASE / asset_name)
