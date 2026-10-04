from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ai_service import run_ai_task
from context_builder import build_farm_context
from database import backend_name, connect, init_db, row_to_dict, rows_to_dicts
from ingestion import ingest_file
from seed_engine import rank_seeds

BASE = Path(__file__).parent

app = FastAPI(title="SeedIQ Farm Data Engine", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AITaskRequest(BaseModel):
    task_type: str = "farm_summary"
    instruction: str = "Summarize the farm for an agronomist."
    field_id: int | None = None


class SeedRankRequest(BaseModel):
    field_profile: dict[str, float]
    seeds: list[dict]


@app.on_event("startup")
def startup() -> None:
    init_db()


def _database_status() -> str:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return "connected"
    except Exception as exc:
        print(f"SeedIQ database connection error: {type(exc).__name__}: {exc}", flush=True)
        return "error"


@app.get("/api/health")
def health():
    storage_ready = bool(os.getenv("SUPABASE_URL") and (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY")))
    db_status = _database_status()
    return {
        "status": "ok" if db_status == "connected" else "degraded",
        "architecture": "aph-to-prospect-to-seed-analysis",
        "version": "0.3.0",
        "database": backend_name(),
        "database_status": db_status,
        "storage": "supabase" if storage_ready else "local",
        "ai": "openai" if os.getenv("OPENAI_API_KEY") else "mock",
    }


@app.post("/api/documents/upload")
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    document_type: Annotated[str | None, Form()] = None,
):
    suffix = Path(file.filename or "upload.bin").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        tmp.write(await file.read())
        temp_path = Path(tmp.name)
    try:
        return ingest_file(temp_path, file.filename or "upload.bin", document_type)
    except Exception as exc:
        print(f"SeedIQ upload error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=400, detail="The document could not be imported. Check the Data Hub status and parser support for this file.") from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.get("/api/farms")
def list_farms():
    try:
        with connect() as conn:
            rows = conn.execute("SELECT * FROM farms ORDER BY updated_at DESC").fetchall()
        return rows_to_dicts(rows)
    except Exception as exc:
        print(f"SeedIQ farms query error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="Farm database is temporarily unavailable.") from exc


@app.get("/api/farms/{farm_id}/context")
def farm_context(farm_id: int):
    try:
        return build_farm_context(farm_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        print(f"SeedIQ context query error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="Farm context is temporarily unavailable.") from exc


@app.get("/api/prospects")
def list_prospects():
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT p.*, f.producer_name, f.farm_name, "
                "(SELECT COUNT(*) FROM fields x WHERE x.farm_id=p.farm_id) AS unit_count "
                "FROM prospects p JOIN farms f ON f.id=p.farm_id ORDER BY p.updated_at DESC"
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
    except Exception as exc:
        print(f"SeedIQ prospects query error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="Prospect database is temporarily unavailable.") from exc


@app.get("/api/prospects/{prospect_id}")
def get_prospect(prospect_id: int):
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT p.*, f.producer_name, f.farm_name FROM prospects p "
                "JOIN farms f ON f.id=p.farm_id WHERE p.id=?", (prospect_id,)
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
        return prospect
    except HTTPException:
        raise
    except Exception as exc:
        print(f"SeedIQ prospect query error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="Prospect context is temporarily unavailable.") from exc


@app.post("/api/farms/{farm_id}/ai")
def ai_task(farm_id: int, req: AITaskRequest):
    try:
        return run_ai_task(farm_id, req.task_type, req.instruction, req.field_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        print(f"SeedIQ AI task error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="AI task could not be completed.") from exc


@app.post("/api/seed/rank")
def seed_rank(req: SeedRankRequest):
    return {"ranked": rank_seeds(req.field_profile, req.seeds), "engine_version": "seed-fit-v0.1"}


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return FileResponse(BASE / "index.html")


@app.api_route("/{page_name}.html", methods=["GET", "HEAD"])
def html_page(page_name: str):
    allowed = {
        "index", "farmers", "field-analysis", "whole-farm-plan", "genetics", "prospects",
        "prospect-detail", "sales-packet", "pipeline", "product-spec", "data-hub"
    }
    if page_name not in allowed:
        raise HTTPException(status_code=404)
    return FileResponse(BASE / f"{page_name}.html")


@app.api_route("/{asset_name}", methods=["GET", "HEAD"])
def static_asset(asset_name: str):
    if asset_name not in {"styles.css", "app.js"}:
        raise HTTPException(status_code=404)
    return FileResponse(BASE / asset_name)
