from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from models import ParsedDocument, ParsedField, SourceFact

VERSION = "0.1.0"


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _first(row: dict[str, Any], names: tuple[str, ...]):
    lookup = {re.sub(r"[^a-z0-9]+", " ", str(k).lower()).strip(): v for k, v in row.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if key in lookup and lookup[key] not in (None, ""):
            return lookup[key]
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    m = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", str(value))
    return float(m.group(0).replace(",", "")) if m else None


def _geometry(value: Any) -> dict | None:
    if not value:
        return None
    if isinstance(value, dict):
        obj = value
    else:
        try:
            obj = json.loads(str(value))
        except Exception:
            return None
    if obj.get("type") == "Feature":
        obj = obj.get("geometry") or {}
    if obj.get("type") in {"Polygon", "MultiPolygon"} and obj.get("coordinates"):
        return obj
    return None


def parse_mbar_rows(rows: list[dict[str, Any]]) -> ParsedDocument:
    """Parse MBAR-style tabular exports as a permanent field/geometry layer.

    Crop values are deliberately ignored. Crop assignment belongs to field_crop_plans
    and changes by crop year; MBAR establishes field identity, acreage and geography.
    """
    out = ParsedDocument(document_type="MBAR")
    seen: set[str] = set()
    for idx, row in enumerate(rows, start=2):
        producer = _first(row, ("producer", "producer name", "insured", "insured name", "operation"))
        out.producer_name = out.producer_name or (_norm(producer) or None)
        out.farm_name = out.farm_name or out.producer_name
        out.policy_number = out.policy_number or (_norm(_first(row, ("policy", "policy number", "policy #"))) or None)

        farm = _norm(_first(row, ("fsa farm", "fsa farm #", "farm number", "farm #")))
        tract = _norm(_first(row, ("fsa tract", "fsa tract #", "tract number", "tract #")))
        field = _norm(_first(row, ("fsa field", "fsa field #", "field number", "field #")))
        name = _norm(_first(row, ("field name", "common name", "name")))
        county = _norm(_first(row, ("county", "county name")))
        state = _norm(_first(row, ("state", "state code")))
        acres = _number(_first(row, ("acres", "reported acres", "field acres", "determined acres")))
        boundary = _geometry(_first(row, ("boundary geojson", "boundary_geojson", "geojson", "geometry")))
        centroid_lat = _number(_first(row, ("latitude", "lat", "centroid lat", "centroid latitude")))
        centroid_lon = _number(_first(row, ("longitude", "lon", "lng", "centroid lon", "centroid longitude")))
        key = "|".join(x for x in (farm, tract, field) if x) or name or f"mbar-row-{idx}"
        if key in seen:
            continue
        seen.add(key)
        meta = {
            "source": "MBAR",
            "parser_version": VERSION,
            "boundary_geojson": boundary,
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "geometry_status": "exact" if boundary else ("centroid" if centroid_lat is not None and centroid_lon is not None else "missing"),
        }
        out.fields.append(ParsedField(
            name=name or (f"Farm {farm} Tract {tract} Field {field}" if any((farm, tract, field)) else f"MBAR Field {idx-1}"),
            acres=acres,
            county=county or None,
            state=state or None,
            farm_number=farm or None,
            tract_number=tract or None,
            field_number=field or None,
            crop=None,
            practice=None,
            irrigation=None,
            metadata=meta,
        ))
        for fname, value in (("farm_number", farm), ("tract_number", tract), ("field_number", field), ("acres", acres), ("boundary_geojson", boundary), ("centroid_lat", centroid_lat), ("centroid_lon", centroid_lon)):
            if value not in (None, ""):
                out.facts.append(SourceFact("mbar_field", key, fname, value, source_locator=f"row:{idx}", confidence=1.0))
    if not out.fields:
        out.warnings.append("No MBAR field rows were recognized. A carrier/export-specific mapping is needed for this form.")
    elif not any((f.metadata or {}).get("boundary_geojson") for f in out.fields):
        out.warnings.append("Fields were recognized, but no exact GeoJSON boundaries were present in this export. AcreFit can still retain field identity and later accept exact boundaries.")
    return out


def parse_mbar_pdf_text(text: str) -> ParsedDocument:
    """Safe starter for MBAR PDFs.

    We do not invent geometry from PDF text. Until a real MBAR PDF is mapped,
    retain the source and flag it for parser validation.
    """
    out = ParsedDocument(document_type="MBAR", raw_preview=text[:6000])
    producer = re.search(r"(?:Producer|Insured)(?: Name)?\s*[:#]?\s*([^\n]+)", text, re.I)
    if producer:
        out.producer_name = producer.group(1).strip()
        out.farm_name = out.producer_name
    out.warnings.append("MBAR PDF detected. Exact field/geometry extraction requires a validated sample-specific parser; no geometry was guessed from unvalidated PDF text.")
    return out
