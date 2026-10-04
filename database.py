from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).with_name("seediq.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS farms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_key TEXT UNIQUE NOT NULL,
  farm_name TEXT,
  producer_name TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  field_key TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  acres REAL,
  county TEXT,
  state TEXT,
  farm_number TEXT,
  tract_number TEXT,
  field_number TEXT,
  crop TEXT,
  practice TEXT,
  irrigation TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crop_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  field_id INTEGER REFERENCES fields(id) ON DELETE CASCADE,
  crop_year INTEGER,
  crop TEXT,
  practice TEXT,
  planted_acres REAL,
  production REAL,
  yield_value REAL,
  approved_yield REAL,
  coverage_level REAL,
  unit_structure TEXT,
  metadata_json TEXT DEFAULT '{}',
  source_document_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_id INTEGER REFERENCES farms(id) ON DELETE SET NULL,
  original_name TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  sha256 TEXT UNIQUE NOT NULL,
  mime_type TEXT,
  document_type TEXT,
  status TEXT NOT NULL DEFAULT 'uploaded',
  parser_name TEXT,
  parser_version TEXT,
  warnings_json TEXT DEFAULT '[]',
  raw_preview TEXT,
  uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
  parsed_at TEXT
);

CREATE TABLE IF NOT EXISTS source_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_id INTEGER REFERENCES farms(id) ON DELETE CASCADE,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  field_name TEXT NOT NULL,
  value_text TEXT,
  value_numeric REAL,
  unit TEXT,
  source_locator TEXT,
  confidence REAL NOT NULL DEFAULT 1.0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS seed_products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company TEXT,
  product_name TEXT NOT NULL,
  relative_maturity REAL,
  drought_score REAL,
  wet_soil_score REAL,
  emergence_score REAL,
  root_score REAL,
  stalk_score REAL,
  disease_score REAL,
  yield_ceiling REAL,
  metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS field_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  field_id INTEGER UNIQUE NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
  awc_score REAL,
  drought_risk REAL,
  wet_risk REAL,
  heat_risk REAL,
  disease_risk REAL,
  yield_environment REAL,
  profile_json TEXT DEFAULT '{}',
  computed_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
  seed_product_id INTEGER NOT NULL REFERENCES seed_products(id) ON DELETE CASCADE,
  fit_score REAL NOT NULL,
  rank_number INTEGER,
  reasons_json TEXT DEFAULT '[]',
  concerns_json TEXT DEFAULT '[]',
  engine_version TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_id INTEGER REFERENCES farms(id) ON DELETE SET NULL,
  field_id INTEGER REFERENCES fields(id) ON DELETE SET NULL,
  task_type TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT,
  input_summary_json TEXT DEFAULT '{}',
  output_text TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)
