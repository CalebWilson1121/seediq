from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import connect, row_to_dict, rows_to_dicts

router = APIRouter()


class ProposalCreateRequest(BaseModel):
    crop_year: int = 2027
    salesperson_name: str | None = None
    salesperson_email: str | None = None
    salesperson_phone: str | None = None
    intro_message: str | None = None


class ProposalStatusRequest(BaseModel):
    status: str


class ApprovalRequest(BaseModel):
    approval_name: str | None = None


def _proposal_summary(farm_id: int, crop_year: int) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.id AS field_id,f.name AS field_name,f.acres,f.irrigation,cp.crop,cp.yield_goal,cp.target_population,cp.selected_seed_product_id,cp.seed_price_per_unit,cp.seeds_per_unit,cp.units_required,cp.seed_cost_per_acre,cp.total_seed_cost,sp.product_name,sp.brand,sp.trait_package,sp.relative_maturity "
            "FROM fields f LEFT JOIN field_crop_plans cp ON cp.field_id=f.id AND cp.crop_year=? "
            "LEFT JOIN seed_products sp ON sp.id=cp.selected_seed_product_id WHERE f.farm_id=? ORDER BY f.name",
            (crop_year, farm_id),
        ).fetchall()
    fields = rows_to_dicts(rows)
    selected = [f for f in fields if f.get("selected_seed_product_id")]
    crop_acres: dict[str, float] = {}
    irr_acres: dict[str, float] = {"IRR": 0.0, "NIRR": 0.0}
    product_totals: dict[str, dict[str, Any]] = {}
    for f in fields:
        acres = float(f.get("acres") or 0)
        crop = (f.get("crop") or "UNASSIGNED").upper()
        crop_acres[crop] = crop_acres.get(crop, 0.0) + acres
        irr = (f.get("irrigation") or "").upper()
        if irr in irr_acres:
            irr_acres[irr] += acres
        pid = f.get("selected_seed_product_id")
        if not pid:
            continue
        key = str(pid)
        line = product_totals.setdefault(key, {
            "seed_product_id": pid,
            "product_name": f.get("product_name"),
            "brand": f.get("brand"),
            "trait_package": f.get("trait_package"),
            "acres": 0.0,
            "units_required": 0.0,
            "estimated_value": 0.0,
            "field_count": 0,
        })
        line["acres"] += acres
        line["units_required"] += float(f.get("units_required") or 0)
        line["estimated_value"] += float(f.get("total_seed_cost") or 0)
        line["field_count"] += 1
    products = []
    import math
    for line in product_totals.values():
        products.append({
            **line,
            "acres": round(line["acres"], 2),
            "units_required": round(line["units_required"], 3),
            "sold_units": math.ceil(line["units_required"] - 1e-9),
            "estimated_value": round(line["estimated_value"], 2),
        })
    return {
        "fields": fields,
        "selected_field_count": len(selected),
        "total_field_count": len(fields),
        "planned_acres": round(sum(float(f.get("acres") or 0) for f in selected), 2),
        "crop_acres": {k: round(v, 2) for k, v in crop_acres.items()},
        "irrigation_acres": {k: round(v, 2) for k, v in irr_acres.items()},
        "products": sorted(products, key=lambda x: (x.get("brand") or "", x.get("product_name") or "")),
        "sold_units": sum(int(x["sold_units"]) for x in products),
        "estimated_seed_value": round(sum(float(x["estimated_value"]) for x in products), 2),
        "yield_goals_complete": sum(1 for f in selected if f.get("yield_goal") is not None),
        "population_complete": sum(1 for f in selected if f.get("target_population") is not None),
        "pricing_complete": sum(1 for f in selected if f.get("seed_price_per_unit") is not None),
    }


def _full_proposal(row: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        p = conn.execute(
            "SELECT p.prospect_name,p.total_acres,p.status AS prospect_status,f.farm_name,f.producer_name FROM prospects p JOIN farms f ON f.id=p.farm_id WHERE p.id=?",
            (row["prospect_id"],),
        ).fetchone()
    base = row_to_dict(p) or {}
    return {**row, **base, "summary": _proposal_summary(int(row["farm_id"]), int(row["crop_year"]))}


@router.post("/api/prospects/{prospect_id}/proposals")
def create_or_update_proposal(prospect_id: int, req: ProposalCreateRequest):
    with connect() as conn:
        p = conn.execute("SELECT id,farm_id FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if not p:
            raise HTTPException(status_code=404, detail="Prospect not found")
        existing = conn.execute("SELECT * FROM seed_proposals WHERE prospect_id=? AND crop_year=?", (prospect_id, req.crop_year)).fetchone()
        if existing:
            conn.execute(
                "UPDATE seed_proposals SET salesperson_name=?,salesperson_email=?,salesperson_phone=?,intro_message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (req.salesperson_name, req.salesperson_email, req.salesperson_phone, req.intro_message, existing["id"]),
            )
            row = conn.execute("SELECT * FROM seed_proposals WHERE id=?", (existing["id"],)).fetchone()
        else:
            token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO seed_proposals(prospect_id,farm_id,crop_year,status,public_token,salesperson_name,salesperson_email,salesperson_phone,intro_message) VALUES(?,?,?,?,?,?,?,?,?)",
                (prospect_id, p["farm_id"], req.crop_year, "draft", token, req.salesperson_name, req.salesperson_email, req.salesperson_phone, req.intro_message),
            )
            row = conn.execute("SELECT * FROM seed_proposals WHERE prospect_id=? AND crop_year=?", (prospect_id, req.crop_year)).fetchone()
    return _full_proposal(row_to_dict(row) or {})


@router.get("/api/prospects/{prospect_id}/proposals/latest")
def latest_proposal(prospect_id: int, crop_year: int = 2027):
    with connect() as conn:
        row = conn.execute("SELECT * FROM seed_proposals WHERE prospect_id=? AND crop_year=?", (prospect_id, crop_year)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Proposal not created yet")
    return _full_proposal(row_to_dict(row) or {})


@router.put("/api/proposals/{proposal_id}/status")
def update_proposal_status(proposal_id: int, req: ProposalStatusRequest):
    allowed = {"draft", "ready", "sent", "approved", "declined"}
    if req.status not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported proposal status")
    with connect() as conn:
        row = conn.execute("SELECT * FROM seed_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Proposal not found")
        sent_sql = ",sent_at=COALESCE(sent_at,CURRENT_TIMESTAMP)" if req.status == "sent" else ""
        approved_sql = ",approved_at=COALESCE(approved_at,CURRENT_TIMESTAMP)" if req.status == "approved" else ""
        conn.execute(f"UPDATE seed_proposals SET status=?,updated_at=CURRENT_TIMESTAMP{sent_sql}{approved_sql} WHERE id=?", (req.status, proposal_id))
        prospect_status = {"ready": "proposal_ready", "sent": "proposal_sent", "approved": "won", "declined": "lost"}.get(req.status)
        if prospect_status:
            conn.execute("UPDATE prospects SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (prospect_status, row["prospect_id"]))
        out = conn.execute("SELECT * FROM seed_proposals WHERE id=?", (proposal_id,)).fetchone()
    return _full_proposal(row_to_dict(out) or {})


@router.get("/api/public/proposals/{token}")
def public_proposal(token: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM seed_proposals WHERE public_token=?", (token,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if not row.get("viewed_at"):
            conn.execute("UPDATE seed_proposals SET viewed_at=CURRENT_TIMESTAMP,status=CASE WHEN status='sent' THEN 'viewed' ELSE status END,updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
            row = conn.execute("SELECT * FROM seed_proposals WHERE id=?", (row["id"],)).fetchone()
    proposal = _full_proposal(row_to_dict(row) or {})
    proposal.pop("public_token", None)
    return proposal


@router.post("/api/public/proposals/{token}/approve")
def approve_public_proposal(token: str, req: ApprovalRequest):
    with connect() as conn:
        row = conn.execute("SELECT * FROM seed_proposals WHERE public_token=?", (token,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Proposal not found")
        conn.execute(
            "UPDATE seed_proposals SET status='approved',approval_name=?,approved_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (req.approval_name, row["id"]),
        )
        conn.execute("UPDATE prospects SET status='won',updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["prospect_id"],))
        out = conn.execute("SELECT * FROM seed_proposals WHERE id=?", (row["id"],)).fetchone()
    return _full_proposal(row_to_dict(out) or {})
