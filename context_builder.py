from __future__ import annotations

import json
from typing import Any

from database import connect, rows_to_dicts


def build_farm_context(farm_id: int) -> dict[str, Any]:
    with connect() as conn:
        farm = conn.execute("SELECT * FROM farms WHERE id=?", (farm_id,)).fetchone()
        if not farm:
            raise KeyError(f"Farm {farm_id} not found")
        fields = rows_to_dicts(conn.execute("SELECT * FROM fields WHERE farm_id=? ORDER BY name", (farm_id,)).fetchall())
        crop_records = rows_to_dicts(conn.execute("SELECT * FROM crop_records WHERE farm_id=? ORDER BY crop_year DESC,id DESC", (farm_id,)).fetchall())
        docs = rows_to_dicts(conn.execute("SELECT id,original_name,document_type,status,parser_name,parser_version,uploaded_at,parsed_at,warnings_json FROM documents WHERE farm_id=? ORDER BY uploaded_at DESC", (farm_id,)).fetchall())

    for d in docs:
        try:
            d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
        except Exception:
            d["warnings"] = []

    return {
        "farm": dict(farm),
        "fields": fields,
        "crop_records": crop_records,
        "source_documents": docs,
        "context_version": "farm-context-v1",
    }


def compact_ai_context(farm_id: int, field_id: int | None = None) -> dict[str, Any]:
    full = build_farm_context(farm_id)
    fields = full["fields"]
    records = full["crop_records"]
    if field_id is not None:
        fields = [f for f in fields if f["id"] == field_id]
        records = [r for r in records if r["field_id"] == field_id]

    return {
        "farm": {
            "id": full["farm"]["id"],
            "name": full["farm"]["farm_name"],
            "producer": full["farm"]["producer_name"],
        },
        "fields": [
            {k: f.get(k) for k in ["id","name","acres","county","state","crop","practice","irrigation"]}
            for f in fields
        ],
        "crop_history": [
            {k: r.get(k) for k in ["field_id","crop_year","crop","practice","planted_acres","yield_value","approved_yield","coverage_level","unit_structure"]}
            for r in records[:50]
        ],
        "provenance": {
            "documents": [{"id": d["id"], "type": d["document_type"], "name": d["original_name"]} for d in full["source_documents"]]
        },
    }
