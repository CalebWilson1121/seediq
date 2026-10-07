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
PRACTICE_RE = re.compile(
    r"\b(NFAC-(?:NIRR|IRR)/(?:NTS|GSG)|NON\s+IRR/(?:NTS|GSG)|IRRIGATED/(?:NTS|GSG)|NIRR/(?:NTS|GSG)|IRR/(?:NTS|GSG))\b",
    re.I,
)


def _normalize_irrigation(raw: str | None) -> str | None:
    value = (raw or "").upper().replace(" ", "")
    if not value:
        return None
    if "NIRR" in value or "NONIRR" in value:
        return "NIRR"
    if "IRR" in value:
        return "IRR"
    return None


def _field_practices(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    # NAU sometimes prints "Field Location Identification Continued" at the top
    # of a page and the Practice/Type line below it, or carries a field across a
    # page break. Search the complete extracted document and assign each field the
    # nearest IRR/NIRR practice within a tight neighborhood instead of assuming the
    # practice always precedes the field text on the same page.
    reader = PdfReader(str(path))
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    full_text = "\n\n<<<PAGE_BREAK>>>\n\n".join(page_texts)
    practices = [
        (m.start(), m.end(), re.sub(r"\s+", " ", m.group(1).upper()).strip())
        for m in PRACTICE_RE.finditer(full_text)
    ]
    found: dict[tuple[str, str, str], dict[str, str]] = {}
    for field_match in FIELD_RE.finditer(full_text):
        g = field_match.groupdict()
        center = (field_match.start() + field_match.end()) // 2
        nearby = []
        for start, end, practice in practices:
            distance = min(abs(start - center), abs(end - center))
            if distance <= 3000:
                nearby.append((distance, practice))
        if not nearby:
            continue
        practice = min(nearby, key=lambda item: item[0])[1]
        irrigation = _normalize_irrigation(practice)
        if not irrigation:
            continue
        found[(g["farm"], g["tract"], g["field"])] = {
            "irrigation": irrigation,
            "practice": practice,
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
        locator = f"page:{field.metadata.get('source_page')}"
        parsed.facts.append(SourceFact("mapped_soi_field", entity_key, "irrigation", agronomic["irrigation"], source_locator=locator, confidence=0.99))
        parsed.facts.append(SourceFact("mapped_soi_field", entity_key, "agronomic_practice", agronomic["practice"], source_locator=locator, confidence=0.99))
        classified += 1

    parsed.warnings.append(
        f"AcreFit retained IRR/NIRR agronomic classification for {classified}/{len(parsed.fields)} mapped fields; insurance financial/election data remains scrubbed."
    )
    return parsed


mapped_soi.parse_nau_mapped_soi_pdf = parse_nau_mapped_soi_pdf_with_agronomics
