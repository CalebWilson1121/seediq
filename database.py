from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DB_PATH = Path(__file__).with_name("seediq.db")

SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS farms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_key TEXT UNIQUE NOT NULL,
  farm_name TEXT,
  producer_name TEXT,
  state TEXT,
  county TEXT,
  default_tillage TEXT,
  default_row_spacing TEXT DEFAULT 'NORMAL',
  default_planting_window TEXT DEFAULT 'NORMAL',
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
  source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS aph_unit_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  source_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  unit_key TEXT NOT NULL,
  field_id INTEGER REFERENCES fields(id) ON DELETE SET NULL,
  match_status TEXT NOT NULL DEFAULT 'unmatched',
  confidence REAL,
  method TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_document_id, unit_key)
);

CREATE TABLE IF NOT EXISTS field_year_environment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
  crop_year INTEGER NOT NULL,
  season_start TEXT,
  season_end TEXT,
  precipitation_in REAL,
  avg_max_temp_f REAL,
  avg_min_temp_f REAL,
  heat_days_90 INTEGER,
  heat_days_95 INTEGER,
  dry_days INTEGER,
  gdd_base50 REAL,
  enso_phase TEXT,
  enso_index REAL,
  source TEXT DEFAULT 'Open-Meteo ERA5-Land',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(field_id, crop_year)
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


def backend_name() -> str:
    return "supabase-postgres" if os.getenv("POSTGRES_URL") else "sqlite-local"


def _clean_postgres_url(raw_url: str) -> str:
    """Remove Vercel/Supabase integration metadata query params psycopg/libpq does not understand."""
    parts = urlsplit(raw_url)
    allowed = {
        "sslmode", "connect_timeout", "application_name", "options",
        "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count",
        "target_session_attrs",
    }
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k in allowed]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _postgres_connection():
    import psycopg
    from psycopg.rows import dict_row

    url = _clean_postgres_url(os.environ["POSTGRES_URL"])
    # Supabase/Vercel commonly connect through a transaction pooler. Named
    # prepared statements are session-scoped and can collide when pooled
    # server connections are reused (e.g. DuplicatePreparedStatement: _pg3_0).
    # Disable automatic server-side prepare so every request is pooler-safe.
    return psycopg.connect(
        url,
        row_factory=dict_row,
        connect_timeout=10,
        prepare_threshold=None,
    )


class ConnectionAdapter:
    def __init__(self):
        self.is_postgres = bool(os.getenv("POSTGRES_URL"))
        if self.is_postgres:
            self.conn = _postgres_connection()
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()

    def _query(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    def execute(self, sql: str, params: tuple | list = ()):
        return self.conn.execute(self._query(sql), params)

    def executescript(self, sql: str):
        if self.is_postgres:
            raise RuntimeError("executescript is only used for local SQLite initialization")
        return self.conn.executescript(sql)


def connect() -> ConnectionAdapter:
    return ConnectionAdapter()


def init_db() -> None:
    if os.getenv("POSTGRES_URL"):
        return
    with connect() as conn:
        conn.executescript(SQLITE_SCHEMA)


def row_to_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [row_to_dict(r) or {} for r in rows]


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)
