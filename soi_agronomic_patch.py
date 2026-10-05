from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

import nau_mapped_soi_parser as mapped_soi
from models import SourceFact

_ORIGINAL_PARSE = mapped_soi.parse_nau_mapped_soi_pdf
FIELD_RE = re.compile(
    r"f(?P<nau>\d+)\s+F(?P<farm>\d+)-T(?P<tract>\d+)-(?P<field>\d+)\s+(?P<acres>[0-9.]+)A",
    re.I,
)

# We intentionally keep only agronomic practice useful to SeedIQ. Insurance
# premium, liability, coverage, elections, unit structure, etc. stay scrubbed.
PRACTICE_PATTERNS = [
    re.compile(r"\b(NFAC-(?:NIRR|IRR)/(?:NTS|GSG))\b", re.I),
    re.compile(r"\b(NON\s+IRR/(?:NTS|GSG))\b", re.I),
    re.compile(r"\b(IRRIGATED/(?:NTS|GSG))\b", re.I),
    re.compile(r"\b(NIRR/(?:NTS|GSG))\b", re.I),
    re.compile(r"\b(IRR/(?:NTS|GSG))\b", re.I),
]


def _normalize_irrigation(raw: str | None) -> str | None:
    value = (raw or "").upper().replace(" ", "")
    if not value:
        return None
    if "NIRR" in value or "NONIRR" in value:
        return "NIRR"
    if "IRR" in value:
        return "IRR"
    return None


def _nearest_practice(text: str, position: int) -> str | None:
    window = text[max(0, position - 1700):position]
    candidates: list[tuple[int, str]] = []
    for pattern in PRACTICE_PATTERNS:
        for match in pattern.finditer(window):
            candidates.append((match.end(), re.sub(r"\s+", " ", match.group(1).upper()).strip()))
    return max(candidates, default=(0, None), key=lambda x: x[0])[1]


def _field_practices(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    reader = PdfReader(str(path))
    found: dict[tuple[str, str, str], dict[str, str]] = {}
    for page in reader.pages:
        text = page.extract_text() or ""
        for block in re.finditer(
            r"Field Location Identification(?: Continued)?:\s*(.*?)(?=\d{4}\s+Total Prod|Other:|Tenant/ Landlord|$)",
            text,
            re.I | re.S,
        ):
            practice = _nearest_practice(text, block.start())
            irrigation = _normalize_irrigation(practice)
            if not irrigation:
                continue
            for match in FIELD_RE.finditer(block.group(1)):
                g = match.groupdict()
                found[(g["farm"], g["tract"], g["field"])] = {
                    "irrigation": irrigation,
                    "practice": practice or irrigation,
                }
    return found


def parse_nau_mapped_soi_pdf_with_agronomics(path: Path):
    parsed = _ORIGINAL_PARSE(path)
    try:
        practices = _field_practices(path)
    except Exception as exc:
        parsed.warnings.append(f"Mapped SOI irrigation classification could not be completed: {exc}")
        return parsed

    classified = 0
    for field in parsed.fields:
        key = (str(field.farm_number or ""), str(field.tract_number or ""), str(field.field_number or ""))
        agronomic = practices.get(key)
        if not agronomic:
            continue
        field.irrigation = agronomic["irrigation"]
        field.practice = agronomic["practice"]
        field.metadata["irrigation_source"] = "NAU Mapped SOI agronomic practice"
        field.metadata["source_irrigation"] = agronomic["irrigation"]
        field.metadata["source_practice"] = agronomic["practice"]
        entity_key = "|".join(key)
        parsed.facts.append(SourceFact("mapped_soi_field", entity_key, "irrigation", agronomic["irrigation"], source_locator=f"page:{field.metadata.get('source_page')}", confidence=0.99))
        parsed.facts.append(SourceFact("mapped_soi_field", entity_key, "agronomic_practice", agronomic["practice"], source_locator=f"page:{field.metadata.get('source_page')}", confidence=0.99))
        classified += 1

    parsed.warnings.append(
        f"SeedIQ retained IRR/NIRR agronomic classification for {classified}/{len(parsed.fields)} mapped fields; insurance financial/election data remains scrubbed."
    )
    return parsed


mapped_soi.parse_nau_mapped_soi_pdf = parse_nau_mapped_soi_pdf_with_agronomics
