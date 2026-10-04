from __future__ import annotations

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
from database import connect, init_db, rows_to_dicts
from ingestion import ingest_file
from seed_engine import rank_seeds

BASE = Path(__file__).parent

app = FastAPI(title="SeedIQ Farm Data Engine", version="0.1.0")
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


@app.get("/api/health")
def health():
    return {"status": "ok", "architecture": "read-once-normalize-reuse", "version": "0.1.0"}


@app.post("/api/documents/upload")
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    document_type: Annotated[str | None, Form()] = None,
):
    suffix = Path(file.filename or "upload.bin").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        temp_path = Path(tmp.name)
    try:
        return ingest_file(temp_path, file.filename or "upload.bin", document_type)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.get("/api/farms")
def list_farms():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM farms ORDER BY updated_at DESC").fetchall()
    return rows_to_dicts(rows)


@app.get("/api/farms/{farm_id}/context")
def farm_context(farm_id: int):
    try:
        return build_farm_context(farm_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/farms/{farm_id}/ai")
def ai_task(farm_id: int, req: AITaskRequest):
    try:
        return run_ai_task(farm_id, req.task_type, req.instruction, req.field_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
