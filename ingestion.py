from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from database import connect, json_dumps
from models import ParsedDocument
from parsers import PARSER_VERSION, parse_document

UPLOAD_DIR = Path(__file__).with_name("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _farm_key(parsed: ParsedDocument, sha: str) -> str:
    base = (parsed.farm_name or parsed.producer_name or "farm").strip().lower()
    return f"{base}:{sha[:10]}" if base == "farm" else base.replace(" ", "-")


def _value_columns(value: Any) -> tuple[str | None, float | None]:
    text = None if value is None else str(value)
    try:
        numeric = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        numeric = None
    return text, numeric


def ingest_file(temp_path: Path, original_name: str, forced_type: str | None = None) -> dict[str, Any]:
    sha = sha256_file(temp_path)
    stored = UPLOAD_DIR / f"{sha[:16]}-{Path(original_name).name}"
    if not stored.exists():
        shutil.copy2(temp_path, stored)

    parsed, parser_name = parse_document(stored, forced_type)
    farm_key = _farm_key(parsed, sha)

    with connect() as conn:
        existing = conn.execute("SELECT * FROM documents WHERE sha256=?", (sha,)).fetchone()
        if existing:
            return {"duplicate": True, "document_id": existing["id"], "farm_id": existing["farm_id"], "status": existing["status"]}

        conn.execute(
            "INSERT INTO farms(farm_key,farm_name,producer_name) VALUES(?,?,?) ON CONFLICT(farm_key) DO UPDATE SET farm_name=COALESCE(excluded.farm_name,farms.farm_name), producer_name=COALESCE(excluded.producer_name,farms.producer_name), updated_at=CURRENT_TIMESTAMP",
            (farm_key, parsed.farm_name or parsed.producer_name or "Imported Farm", parsed.producer_name),
        )
        farm = conn.execute("SELECT * FROM farms WHERE farm_key=?", (farm_key,)).fetchone()
        farm_id = farm["id"]

        cur = conn.execute(
            "INSERT INTO documents(farm_id,original_name,stored_path,sha256,mime_type,document_type,status,parser_name,parser_version,warnings_json,raw_preview,parsed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (farm_id, original_name, str(stored), sha, mimetypes.guess_type(original_name)[0], parsed.document_type, "parsed", parser_name, PARSER_VERSION, json_dumps(parsed.warnings), parsed.raw_preview),
        )
        document_id = cur.lastrowid

        field_id_by_key: dict[str, int] = {}
        for i, f in enumerate(parsed.fields):
            key_parts = [str(x or "") for x in [f.farm_number, f.tract_number, f.field_number]]
            local_key = "|".join(x for x in key_parts if x) or f.name or f"field-{i+1}"
            field_key = f"{farm_id}:{local_key}"
            conn.execute(
                """INSERT INTO fields(farm_id,field_key,name,acres,county,state,farm_number,tract_number,field_number,crop,practice,irrigation,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(field_key) DO UPDATE SET name=excluded.name, acres=COALESCE(excluded.acres,fields.acres), county=COALESCE(excluded.county,fields.county), state=COALESCE(excluded.state,fields.state), crop=COALESCE(excluded.crop,fields.crop), practice=COALESCE(excluded.practice,fields.practice), irrigation=COALESCE(excluded.irrigation,fields.irrigation), updated_at=CURRENT_TIMESTAMP""",
                (farm_id, field_key, f.name, f.acres, f.county, f.state, f.farm_number, f.tract_number, f.field_number, f.crop, f.practice, f.irrigation, json_dumps(f.metadata)),
            )
            field_row = conn.execute("SELECT id FROM fields WHERE field_key=?", (field_key,)).fetchone()
            field_id_by_key[local_key] = field_row["id"]

        for r in parsed.crop_records:
            field_id = field_id_by_key.get(r.field_key)
            conn.execute(
                "INSERT INTO crop_records(farm_id,field_id,crop_year,crop,practice,planted_acres,production,yield_value,approved_yield,coverage_level,unit_structure,metadata_json,source_document_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (farm_id, field_id, r.crop_year, r.crop, r.practice, r.planted_acres, r.production, r.yield_value, r.approved_yield, r.coverage_level, r.unit_structure, json_dumps(r.metadata), document_id),
            )

        for fact in parsed.facts:
            vtext, vnum = _value_columns(fact.value)
            conn.execute(
                "INSERT INTO source_facts(farm_id,document_id,entity_type,entity_key,field_name,value_text,value_numeric,unit,source_locator,confidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (farm_id, document_id, fact.entity_type, fact.entity_key, fact.field_name, vtext, vnum, fact.unit, fact.source_locator, fact.confidence),
            )

    return {
        "duplicate": False,
        "document_id": document_id,
        "farm_id": farm_id,
        "document_type": parsed.document_type,
        "parser": parser_name,
        "fields_created_or_updated": len(parsed.fields),
        "crop_records_created": len(parsed.crop_records),
        "facts_stored": len(parsed.facts),
        "warnings": parsed.warnings,
    }
