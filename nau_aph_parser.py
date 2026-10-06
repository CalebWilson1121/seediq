from __future__ import annotations
import re
from pathlib import Path
from pypdf import PdfReader
from models import CropRecord, ParsedDocument, ParsedField, SourceFact

VERSION = "0.5.0"

def _num(v):
    if v is None:
        return None
    m = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None

def _after(block, label):
    m = re.search(r"(?:^|\n)" + re.escape(label) + r"\n([^\n]*)", block, re.M)
    if not m:
        return None
    v = m.group(1).strip()
    return v or None

def _metric(block, label):
    return _num(_after(block, label))

def _first(pattern, text):
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else None

def _location(block):
    m = re.search(r"TWP-RGE Section FSA Farm # FSA Tract # Fld#\n([^\n]+)", block)
    if not m:
        return {"township_range": None, "section": None, "fsa_farm_number": None, "fsa_tract_number": None, "fsa_field_number": None}
    tokens = re.sub(r"\s+", " ", m.group(1).strip()).split(" ")
    out = {"township_range": None, "section": None, "fsa_farm_number": None, "fsa_tract_number": None, "fsa_field_number": None}
    if tokens:
        out["township_range"] = tokens[0]
    if len(tokens) > 1:
        out["section"] = tokens[1]
    if len(tokens) > 2:
        out["fsa_farm_number"] = tokens[2]
    if len(tokens) > 3:
        out["fsa_tract_number"] = tokens[3]
    if len(tokens) > 4:
        out["fsa_field_number"] = tokens[4]
    return out

def _year_row(line):
    line = re.sub(r"\s+", " ", line.strip())
    m = re.match(r"^(#)?(\d{4})([A-Z])?\s+(.+)$", line)
    if not m:
        return None
    year = int(m.group(2))
    if year < 1980 or year > 2100:
        return None
    tokens = m.group(4).split()
    first = tokens[0]
    row = {"year": year, "excluded": bool(m.group(1)), "year_flag": m.group(3), "raw": line,
           "production": None, "acres": None, "actual_yield": None, "descriptor": None, "preqa": None}
    coded = bool(re.search(r"[A-Z*]", first))
    if len(tokens) >= 3 and not coded:
        row["production"] = _num(tokens[0])
        row["acres"] = _num(tokens[1])
        row["actual_yield"] = _num(tokens[2])
        code = "".join(re.findall(r"[A-Z]", tokens[2]))
        row["descriptor"] = code or None
    else:
        row["actual_yield"] = _num(first)
        code = "".join(re.findall(r"[A-Z]", first))
        row["descriptor"] = code or m.group(3)
    for token in reversed(tokens):
        n = _num(token)
        if n is not None:
            row["preqa"] = n
            break
    return row

def _parse_unit(block, page_number):
    h = re.search(r"^([A-Z]+)\n([A-Z0-9-]+)\nUnit #\n([0-9-]+)\n([A-Z]{1,3})", block, re.M)
    if not h:
        return None
    county_raw = _after(block, "County") or ""
    cm = re.match(r"(?:(\d{3})\s*-\s*)?(.*)", county_raw)
    ap = re.search(r"Adj\. Yield Apprv Yld\n\s*([^\n]+)", block)
    vals = []
    if ap:
        vals = [_num(x) for x in ap.group(1).split()]
        vals = [x for x in vals if x is not None]
    approved = vals[1] if len(vals) > 1 else (vals[0] if vals else None)
    share = re.search(r"Insured's Share\n([0-9.]+)", block)
    start = block.find("Pre-QA\nActual Yield")
    end = block.find("Prac/Type", start + 1) if start >= 0 else -1
    table = block[start:end] if start >= 0 and end > start else block
    years = [r for r in (_year_row(x) for x in table.splitlines()) if r]
    acre_values = [r["acres"] for r in years if r["acres"] is not None]
    loc = _location(block)
    return {
        "page": page_number, "crop": h.group(1), "plan": h.group(2), "unit": h.group(3), "structure": h.group(4),
        "county_code": cm.group(1) if cm else None, "county": cm.group(2).strip() if cm else county_raw,
        "type": _after(block, "Type"), "practice": _after(block, "Practice"), "farm_name": _after(block, "Farm Name"),
        "options": _after(block, "Options"), "yield_limit": _after(block, "Yield Limit"),
        "share": _num(share.group(1)) if share else None, "t_yield": _metric(block, "T Yield"),
        "prior_yield": _metric(block, "Prior Yield"), "yield_floor": _metric(block, "Yld Floor"),
        "rate_yield": _metric(block, "Rate Yld"), "average_yield": _metric(block, "Ave. Yield"),
        "approved_yield": approved, "acres": acre_values[-1] if acre_values else None, "years": years,
        **loc,
    }

def _summary_units(text: str) -> dict[tuple[str, str], dict]:
    """Read current APH unit identity from NAU's Production Reporting Unit Summary.

    The summary is often the cleanest bridge to the mapped SOI because it carries
    current reported acres plus PLSS location and the Fsn/Tract farm identifier.
    """
    out: dict[tuple[str, str], dict] = {}
    header = re.compile(
        r"(?m)^(CORN|SOYBEANS)\n([^\n]+)\n([0-9]{4}-[0-9]{4}-[0-9]{3})\n[A-Z]{1,3}\n\d{3}\n([^\n]+)\n"
    )
    matches = list(header.finditer(text))
    for idx, match in enumerate(matches):
        crop = match.group(1).upper()
        unit = match.group(3)
        practice = re.sub(r"\s+", " ", match.group(4).strip()).upper()
        stop = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(text), match.end() + 1400)
        block = text[match.end():stop]

        acres = None
        acre_match = re.search(
            r"(?m)^([0-9][0-9,]*\.[0-9]{2})\s+[0-9]+(?:\.[0-9]+)?\s+[0-9][0-9,]*(?:\.[0-9]+)?",
            block,
        )
        if acre_match:
            acres = _num(acre_match.group(1))

        township_range = section = fsa_farm_number = farm_name = None
        loc = re.search(
            r"Location:\s*\n?\s*(\d{3}[NS]-\d{3}[EW])-([0-9]{4})(?:\s+Fsn/Tract:\s*(\d+)\s+([^\n]+))?",
            block,
            re.I,
        )
        if loc:
            township_range = loc.group(1).upper()
            section = loc.group(2)
            fsa_farm_number = loc.group(3)
            farm_name = re.sub(r"\s+", " ", (loc.group(4) or "").strip()) or None

        out[(crop, unit)] = {
            "acres": acres,
            "practice": practice,
            "township_range": township_range,
            "section": section,
            "fsa_farm_number": fsa_farm_number,
            "farm_name": farm_name,
        }
    return out


def looks_like_nau_aph(text):
    s = text[:20000].lower()
    return "actual production history (aph) database" in s and "naucountry.com" in s

def parse_nau_aph_pdf(path: Path):
    pages = [p.extract_text() or "" for p in PdfReader(str(path)).pages]
    full = "\n".join(pages)
    producer = _first(r"Insured Name:\s*([^\n]+?)(?:\s+Agency Code:|$)", full) or _first(r"Insured Information\s*\n([^\n]+)", full)
    policy = _first(r"Policy\s*(?:Number|#)\s*:?\s*(?:\\n\\s*)?([A-Z0-9-]{6,})", full)
    out = ParsedDocument(document_type="APH", producer_name=producer, farm_name=producer, policy_number=policy, raw_preview=full[:6000])
    summary_units = _summary_units(full)
    units = []
    for page_number, text in enumerate(pages, 1):
        for block in text.split("Crop Plan\n")[1:]:
            u = _parse_unit(block, page_number)
            if u:
                summary = summary_units.get((str(u.get("crop") or "").upper(), str(u.get("unit") or "")), {})
                if u.get("acres") is None:
                    u["acres"] = summary.get("acres")
                for key in ("township_range", "section", "fsa_farm_number", "farm_name"):
                    if not u.get(key) and summary.get(key):
                        u[key] = summary.get(key)
                if not u.get("practice") and summary.get("practice"):
                    u["practice"] = summary.get("practice")
                units.append(u)
    seen = set()
    for u in units:
        key = (u["unit"], u["crop"], u["practice"])
        if key in seen:
            continue
        seen.add(key)
        meta = {k: u.get(k) for k in ("unit","plan","structure","county_code","type","options","yield_limit","share","t_yield","prior_yield","yield_floor","rate_yield","average_yield","approved_yield","page","township_range","section","fsa_farm_number","fsa_tract_number","fsa_field_number","farm_name")}
        meta.update({"carrier": "NAU Country", "parser_version": VERSION})
        out.fields.append(ParsedField(name=u["farm_name"] or "NAU Unit " + u["unit"], acres=u["acres"], county=u["county"], state="KS", field_number=u["unit"], crop=u["crop"], practice=u["practice"], irrigation="IRRIGATED" if "IRR" in (u["practice"] or "") and "NIRR" not in (u["practice"] or "") else "NON-IRRIGATED", metadata=meta))
        for field_name, value in (("unit_number",u["unit"]),("approved_yield",u["approved_yield"]),("rate_yield",u["rate_yield"]),("t_yield",u["t_yield"]),("insured_share",u["share"]),("township_range",u["township_range"]),("section",u["section"]),("fsa_farm_number",u["fsa_farm_number"])):
            if value is not None:
                out.facts.append(SourceFact("aph_unit", u["unit"], field_name, value, source_locator=f"page:{u['page']}", confidence=.99))
        for y in u["years"]:
            md = {"carrier":"NAU Country","unit_number":u["unit"],"yield_descriptor":y["descriptor"],"year_flag":y["year_flag"],"yield_excluded":y["excluded"],"pre_qa_actual_yield":y["preqa"],"raw_row":y["raw"],"rate_yield":u["rate_yield"],"t_yield":u["t_yield"],"source_page":u["page"]}
            out.crop_records.append(CropRecord(field_key=u["unit"], crop_year=y["year"], crop=u["crop"], practice=u["practice"], planted_acres=y["acres"], production=y["production"], yield_value=y["actual_yield"], approved_yield=u["approved_yield"], unit_structure=u["structure"], metadata=md))
            for field_name, value in (("production",y["production"]),("acres",y["acres"]),("actual_yield",y["actual_yield"]),("pre_qa_actual_yield",y["preqa"])):
                if value is not None:
                    out.facts.append(SourceFact("aph_year", f"{u['unit']}:{y['year']}", field_name, value, source_locator=f"page:{u['page']}", confidence=.98))
    out.warnings.append(f"NAU APH parser v{VERSION}: extracted {len(out.fields)} crop/practice units and {len(out.crop_records)} APH year rows. Township/range/section is captured for provisional soil cross-reference; exact field boundaries should replace PLSS sections for final seed placement.")
    return out
