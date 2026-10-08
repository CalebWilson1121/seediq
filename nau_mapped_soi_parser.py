from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from pypdf import PdfReader
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union

from models import ParsedDocument, ParsedField, SourceFact

VERSION = "0.3.0"

FIELD_RE = re.compile(
    r"f(?P<nau>\d+)\s+F(?P<farm>\d+)-T(?P<tract>\d+)-(?P<field>\d+)\s+(?P<acres>[0-9.]+)A",
    re.I,
)
LEGAL_RE = re.compile(r"\b(?P<section>\d{4})-(?P<tr>\d{3}[NS]\d{3}[EW])\b", re.I)
UNIT_RE = re.compile(r"\b(\d{4}-\d{4}-\d{3})\b")
PRACTICE_RE = re.compile(
    r"\b(NFAC-(?:NIRR|IRR)/(?:NTS|GSG)|NON\s+IRR/(?:NTS|GSG)|IRRIGATED/(?:NTS|GSG)|NIRR/(?:NTS|GSG)|IRR/(?:NTS|GSG))\b",
    re.I,
)


@dataclass
class _FieldEntry:
    nau_id: str
    farm: str
    tract: str
    field: str
    acres: float
    crop: str | None
    practice: str | None
    unit_number: str | None
    common_name: str | None
    page: int
    section: str
    township_range: str


@dataclass
class _RasterComponent:
    area: int
    runs: list[tuple[int, int, int]]
    estimated_acres: float = 0.0


def _practice_bucket(value: Any) -> str:
    raw = str(value or "").upper().replace("-", "").replace(" ", "")
    if "NIRR" in raw or "NONIRR" in raw:
        return "NIRR"
    if "IRR" in raw:
        return "IRR"
    return raw or "UNKNOWN"


def _unit_memberships(full_text: str) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Build authoritative current SOI unit -> physical field memberships.

    The prior parser inferred unit numbers by looking backward from each field
    occurrence. On dense SOI pages that can bleed a neighboring unit into the
    next Field Location block. NAU's Total Unit Summary is a much safer anchor:
    the immediately preceding Field Location Identification list is the set of
    physical FSA fields that belongs to that unit.
    """
    out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    summary_re = re.compile(
        r"Total Unit Summary(?P<unit>\d{4}-\d{4})(?:-\d{3})?\n"
        r"(?P<crop>CORN|SOYBEANS?)\n(?P<practice>[^\n]+)\n"
        r"Acres:\s*(?P<acres>[0-9,.]+)",
        re.I,
    )
    summaries = list(summary_re.finditer(full_text))
    previous_end = 0
    for sm in summaries:
        search_start = max(previous_end, full_text.rfind("Field Location Identification:", previous_end, sm.start()))
        block = full_text[search_start:sm.start()]
        fl = re.search(
            r"Field Location Identification(?: Continued)?:\s*(.*?)(?=2026 Total Prod|Other:|$)",
            block,
            re.I | re.S,
        )
        if not fl:
            previous_end = sm.end()
            continue
        crop = sm.group("crop").upper()
        crop = "SOYBEANS" if crop.startswith("SOY") else "CORN"
        practice = re.sub(r"\s+", " ", sm.group("practice").strip().upper())
        unit_number = sm.group("unit") + "-000"
        unit_acres = float(sm.group("acres").replace(",", ""))
        names = re.findall(r"Other:\s*Farm Name:\s*([^\n]+)", block, re.I)
        management_name = None
        if names:
            candidate = re.sub(r"\s+", " ", names[-1]).strip(" -")
            if candidate and not re.fullmatch(r"\d+/\d+/\d+", candidate):
                management_name = candidate
        for fm in FIELD_RE.finditer(fl.group(1)):
            g = fm.groupdict()
            key = (g["farm"], g["tract"], g["field"])
            membership = {
                "unit_number": unit_number,
                "crop": crop,
                "practice": practice,
                "practice_bucket": _practice_bucket(practice),
                "unit_reported_acres": unit_acres,
                "management_name": management_name,
                "source_field_location_id": g["nau"],
                "physical_reported_acres": float(g["acres"]),
            }
            existing = out.setdefault(key, [])
            ident = (unit_number, crop, _practice_bucket(practice))
            if not any((x.get("unit_number"), x.get("crop"), x.get("practice_bucket")) == ident for x in existing):
                existing.append(membership)
        previous_end = sm.end()
    return out


def looks_like_nau_mapped_soi(text: str) -> bool:
    t = text.upper()
    return (
        "SCHEDULE OF INSURANCE" in t
        and "AVAILABLE UNITS FOR MAP VIEW" in t
        and "FIELD LOCATION IDENTIFICATION" in t
    )


def _producer_name(text: str) -> str | None:
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    rejects = ("FRONTIER", "NAU COUNTRY", "INSURANCE", "AGENCY", "BRANCH", "SCHEDULE")
    for line in lines[:120]:
        u = line.upper()
        if any(x in u for x in rejects):
            continue
        if not re.fullmatch(r"[A-Z0-9 &',./()-]{5,}", u):
            continue
        if any(token in u for token in ("FARM", "RANCH", "LLC", "INC", "PARTNERSHIP")):
            return line
    return None


def _crop_before(text: str, pos: int) -> str | None:
    pre = text[max(0, pos - 1600):pos]
    crops = re.findall(r"\b(CORN|SOYBEA(?:N|NS)?)\b", pre, re.I)
    if not crops:
        return None
    crop = crops[-1].upper()
    return "SOYBEANS" if crop.startswith("SOY") else "CORN"


def _unit_before(text: str, pos: int) -> str | None:
    pre = text[max(0, pos - 2400):pos]
    units = UNIT_RE.findall(pre)
    return units[-1] if units else None


def _practice_before(text: str, pos: int) -> str | None:
    pre = text[max(0, pos - 2400):pos]
    practices = PRACTICE_RE.findall(pre)
    if not practices:
        return None
    return re.sub(r"\s+", " ", practices[-1].upper()).strip()


def _common_name_after(text: str, pos: int) -> str | None:
    tail = text[pos:pos + 650]
    m = re.search(r"Farm Name:\s*([^\n]*)", tail, re.I)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip(" -")
    if not name or re.fullmatch(r"\d+/\d+/\d+", name):
        return None
    return name


def _field_entries(group_text: str, page_number: int, section: str, township_range: str) -> list[_FieldEntry]:
    out: dict[tuple[str, str, str], _FieldEntry] = {}
    for block in re.finditer(
        r"Field Location Identification(?: Continued)?:\s*(.*?)(?=2026 Total Prod|Other:|Tenant/ Landlord|$)",
        group_text,
        re.I | re.S,
    ):
        crop = _crop_before(group_text, block.start())
        practice = _practice_before(group_text, block.start())
        unit_number = _unit_before(group_text, block.start())
        common_name = _common_name_after(group_text, block.end())
        for fm in FIELD_RE.finditer(block.group(1)):
            g = fm.groupdict()
            key = (g["farm"], g["tract"], g["field"])
            entry = _FieldEntry(
                nau_id=g["nau"],
                farm=g["farm"],
                tract=g["tract"],
                field=g["field"],
                acres=float(g["acres"]),
                crop=crop,
                practice=practice,
                unit_number=unit_number,
                common_name=common_name,
                page=page_number,
                section=section,
                township_range=township_range,
            )
            previous = out.get(key)
            if previous is None or (previous.common_name is None and common_name):
                out[key] = entry
    for fm in FIELD_RE.finditer(group_text):
        g = fm.groupdict()
        key = (g["farm"], g["tract"], g["field"])
        if key not in out:
            out[key] = _FieldEntry(
                nau_id=g["nau"],
                farm=g["farm"],
                tract=g["tract"],
                field=g["field"],
                acres=float(g["acres"]),
                crop=_crop_before(group_text, fm.start()),
                practice=_practice_before(group_text, fm.start()),
                unit_number=_unit_before(group_text, fm.start()),
                common_name=_common_name_after(group_text, fm.end()),
                page=page_number,
                section=section,
                township_range=township_range,
            )
    return list(out.values())


def _large_map_image(page) -> Image.Image | None:
    for item in page.images:
        image = item.image
        if image.width >= 1000 and image.height >= 1000:
            return image.convert("RGB")
    return None


def _groups(indices: np.ndarray) -> list[tuple[int, int]]:
    if not len(indices):
        return []
    groups: list[tuple[int, int]] = []
    start = prev = int(indices[0])
    for raw in indices[1:]:
        value = int(raw)
        if value > prev + 2:
            groups.append((start, prev))
            start = value
        prev = value
    groups.append((start, prev))
    return groups


def _section_frame(image: Image.Image) -> tuple[int, int, int, int]:
    arr = np.asarray(image, dtype=np.int16)
    red, green, blue = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    orange = (red > 165) & (green > 70) & (green < 225) & (blue < 95) & ((red - green) > 18)
    h, w = orange.shape
    col = orange.sum(axis=0)
    row = orange.sum(axis=1)
    x_groups = _groups(np.where(col > h * 0.28)[0])
    y_groups = _groups(np.where(row > w * 0.28)[0])
    x_centers = [round((a + b) / 2) for a, b in x_groups]
    y_centers = [round((a + b) / 2) for a, b in y_groups]
    left = min((x for x in x_centers if x < w * 0.18), default=round(w * 0.025))
    right = max((x for x in x_centers if x > w * 0.82), default=round(w * 0.975))
    top = min((y for y in y_centers if y < h * 0.18), default=round(h * 0.045))
    bottom = max((y for y in y_centers if y > h * 0.82), default=round(h * 0.955))
    if right - left < w * 0.65 or bottom - top < h * 0.65:
        return round(w * 0.025), round(h * 0.045), round(w * 0.975), round(h * 0.955)
    return left, top, right, bottom


def _overlay_mask(image: Image.Image, crop: str, frame: tuple[int, int, int, int], scale: float = 0.5):
    small = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.BILINEAR)
    hsv = np.asarray(small.convert("HSV"), dtype=np.uint8)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    if crop == "SOYBEANS":
        mask = (hue >= 120) & (hue <= 160) & (sat >= 55) & (val >= 70)
    else:
        mask = (hue >= 43) & (hue <= 62) & (sat >= 120) & (val >= 110)
    left, top, right, bottom = [round(v * scale) for v in frame]
    clipped = np.zeros(mask.shape, dtype=bool)
    clipped[max(0, top):min(mask.shape[0], bottom + 1), max(0, left):min(mask.shape[1], right + 1)] = mask[max(0, top):min(mask.shape[0], bottom + 1), max(0, left):min(mask.shape[1], right + 1)]
    return clipped, (left, top, right, bottom), scale


def _find(parent: list[int], value: int) -> int:
    while parent[value] != value:
        parent[value] = parent[parent[value]]
        value = parent[value]
    return value


def _components(mask: np.ndarray, min_pixels: int = 12) -> list[_RasterComponent]:
    runs: list[tuple[int, int, int]] = []
    parent: list[int] = []
    previous: list[int] = []
    for y in range(mask.shape[0]):
        row = mask[y]
        diff = np.diff(np.concatenate(([False], row, [False])).astype(np.int8))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0] - 1
        current: list[int] = []
        pstart = 0
        for x1_raw, x2_raw in zip(starts, ends):
            x1, x2 = int(x1_raw), int(x2_raw)
            idx = len(runs)
            runs.append((y, x1, x2))
            parent.append(idx)
            current.append(idx)
            while pstart < len(previous) and runs[previous[pstart]][2] < x1:
                pstart += 1
            j = pstart
            while j < len(previous) and runs[previous[j]][1] <= x2:
                other = previous[j]
                ra, rb = _find(parent, idx), _find(parent, other)
                if ra != rb:
                    parent[rb] = ra
                j += 1
        previous = current
    grouped: dict[int, list[tuple[int, int, int]]] = {}
    for i, run in enumerate(runs):
        grouped.setdefault(_find(parent, i), []).append(run)
    result = []
    for group in grouped.values():
        area = sum(x2 - x1 + 1 for _, x1, x2 in group)
        if area >= min_pixels:
            result.append(_RasterComponent(area=area, runs=group))
    return sorted(result, key=lambda c: c.area, reverse=True)


def _match_components(entries: list[_FieldEntry], components: list[_RasterComponent], frame_area: float):
    if not entries or not components:
        return []
    for comp in components:
        comp.estimated_acres = 640.0 * comp.area / max(frame_area, 1.0)
    fields = sorted(entries, key=lambda e: e.acres, reverse=True)
    comps = sorted(
        [c for c in components if c.estimated_acres >= max(0.04, min(e.acres for e in fields) * 0.15)],
        key=lambda c: c.estimated_acres,
        reverse=True,
    )
    if len(comps) < len(fields):
        comps = sorted(components, key=lambda c: c.estimated_acres, reverse=True)
    n, m = len(fields), len(comps)
    inf = 1e18
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    take = [[False] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = dp[i][j - 1]
            expected = max(fields[i - 1].acres, 0.01)
            observed = max(comps[j - 1].estimated_acres, 0.01)
            cost = abs(math.log(observed / expected))
            if dp[i - 1][j - 1] + cost < dp[i][j]:
                dp[i][j] = dp[i - 1][j - 1] + cost
                take[i][j] = True
    if dp[n][m] >= inf / 2:
        return []
    pairs = []
    i, j = n, m
    while i and j:
        if take[i][j]:
            field, comp = fields[i - 1], comps[j - 1]
            ratio = comp.estimated_acres / max(field.acres, 0.01)
            error = abs(math.log(max(ratio, 1e-6)))
            confidence = 0.93 if error <= 0.18 else 0.86 if error <= 0.35 else 0.72 if error <= 0.60 else 0.55
            pairs.append((field, comp, confidence, ratio))
            i -= 1
            j -= 1
        else:
            j -= 1
    return list(reversed(pairs))


def _component_geometry(comp: _RasterComponent, frame_small: tuple[int, int, int, int], section_geojson: dict[str, Any]) -> dict[str, Any] | None:
    left, top, right, bottom = frame_small
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    section = shape(section_geojson)
    min_lon, min_lat, max_lon, max_lat = section.bounds
    merged: list[tuple[int, int, int, int]] = []
    active: dict[tuple[int, int], tuple[int, int]] = {}
    for y, x1, x2 in sorted(comp.runs):
        key = (x1, x2)
        next_active: dict[tuple[int, int], tuple[int, int]] = {}
        for k, (ystart, yend) in active.items():
            if k == key and y == yend + 1:
                next_active[k] = (ystart, y)
            else:
                merged.append((k[0], ystart, k[1] + 1, yend + 1))
        if key not in next_active:
            next_active[key] = (y, y)
        active = next_active
    for k, (ystart, yend) in active.items():
        merged.append((k[0], ystart, k[1] + 1, yend + 1))
    pixel_geom = unary_union([box(x1, y1, x2, y2) for x1, y1, x2, y2 in merged])
    if pixel_geom.is_empty:
        return None
    def pixel_to_geo(x, y, z=None):
        lon = min_lon + ((x - left) / width) * (max_lon - min_lon)
        lat = max_lat - ((y - top) / height) * (max_lat - min_lat)
        return (lon, lat)
    geo = transform(pixel_to_geo, pixel_geom)
    try:
        geo = geo.intersection(section.buffer(0.00004))
    except Exception:
        pass
    if geo.is_empty:
        return None
    tolerance = max((max_lon - min_lon), (max_lat - min_lat)) / 1200.0
    geo = geo.simplify(tolerance, preserve_topology=True)
    if geo.geom_type not in {"Polygon", "MultiPolygon"}:
        geo = geo.buffer(0)
    if geo.is_empty or geo.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    return mapping(geo)


def _source_crop_year(text: str) -> int | None:
    matches = re.findall(r"\b(20\d{2})\s+Total\s+Prod", text, re.I)
    if matches:
        return max(int(x) for x in matches)
    years = [int(x) for x in re.findall(r"\b(20\d{2})\b", text)]
    plausible = [x for x in years if 2020 <= x <= 2100]
    return max(plausible) if plausible else None


def parse_nau_mapped_soi_pdf(path: Path) -> ParsedDocument:
    reader = PdfReader(str(path))
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    full_text = "\n".join(page_texts)
    unit_memberships = _unit_memberships(full_text)
    source_crop_year = _source_crop_year(full_text)
    out = ParsedDocument(document_type="SOI", producer_name=_producer_name(full_text), raw_preview=full_text[:6000])
    out.farm_name = out.producer_name
    out.policy_number = None
    map_pages: list[tuple[int, Image.Image]] = []
    for idx, page in enumerate(reader.pages):
        image = _large_map_image(page)
        if image is not None and idx != 0:
            map_pages.append((idx, image))
    if not map_pages:
        out.warnings.append("NAU mapped SOI was recognized, but no field-detail map images were found.")
        return out
    parsed_fields: dict[tuple[str, str, str], ParsedField] = {}
    geometry_matches = 0
    from soil_service import resolve_kansas_plss
    for map_idx, (page_idx, image) in enumerate(map_pages):
        page_text = page_texts[page_idx]
        legal = LEGAL_RE.search(page_text)
        if not legal:
            out.warnings.append(f"Page {page_idx + 1}: map found, but legal section/township-range could not be read.")
            continue
        section = legal.group("section")
        township_range = legal.group("tr").upper()
        next_page_idx = map_pages[map_idx + 1][0] if map_idx + 1 < len(map_pages) else len(page_texts)
        group_text = "\n".join(page_texts[page_idx:next_page_idx])
        entries = _field_entries(group_text, page_idx + 1, section, township_range)
        if not entries:
            out.warnings.append(f"Page {page_idx + 1}: no Field Location Identification records were found.")
            continue
        try:
            section_loc = resolve_kansas_plss(township_range, int(section))
            section_geojson = section_loc["boundary_geojson"]
        except Exception as exc:
            section_geojson = None
            out.warnings.append(f"Page {page_idx + 1}: PLSS section {section}-{township_range} could not be georeferenced: {exc}")
        frame = _section_frame(image)
        page_matches: dict[tuple[str, str, str], tuple[_RasterComponent, float, float, tuple[int, int, int, int]]] = {}
        if section_geojson:
            for crop in ("CORN", "SOYBEANS"):
                crop_entries = [e for e in entries if e.crop == crop]
                if not crop_entries:
                    continue
                mask, frame_small, _ = _overlay_mask(image, crop, frame)
                comps = _components(mask)
                frame_area = max((frame_small[2] - frame_small[0]) * (frame_small[3] - frame_small[1]), 1)
                for entry, comp, confidence, ratio in _match_components(crop_entries, comps, frame_area):
                    page_matches[(entry.farm, entry.tract, entry.field)] = (comp, confidence, ratio, frame_small)
        common_counts: dict[str, int] = {}
        for entry in entries:
            if entry.common_name:
                common_counts[entry.common_name] = common_counts.get(entry.common_name, 0) + 1
        for entry in entries:
            key = (entry.farm, entry.tract, entry.field)
            display_name = entry.common_name if entry.common_name and common_counts.get(entry.common_name, 0) == 1 else f"{entry.common_name} — Field {entry.field}" if entry.common_name else f"F{entry.farm} T{entry.tract} Field {entry.field}"
            metadata: dict[str, Any] = {
                "source": "NAU Mapped SOI",
                "parser_version": VERSION,
                "source_crop_year": source_crop_year,
                "source_page": entry.page,
                "source_field_location_id": entry.nau_id,
                "legal_section": entry.section,
                "township_range": entry.township_range,
                "insurance_data_scrubbed": True,
                "insurance_unit_memberships": unit_memberships.get(key, []),
                "insurance_unit_number": (unit_memberships.get(key, [{}])[0].get("unit_number") if unit_memberships.get(key) else entry.unit_number),
                "source_crop": (unit_memberships.get(key, [{}])[0].get("crop") if unit_memberships.get(key) else entry.crop),
                "source_practice": (unit_memberships.get(key, [{}])[0].get("practice") if unit_memberships.get(key) else entry.practice),
                "management_name": (unit_memberships.get(key, [{}])[0].get("management_name") if unit_memberships.get(key) else entry.common_name),
                "unit_identity_source": "NAU Total Unit Summary + Field Location Identification",
                "geometry_status": "reference_missing",
                "geometry_authoritative": False,
            }
            match = page_matches.get(key)
            if match and section_geojson:
                comp, confidence, ratio, frame_small = match
                boundary = _component_geometry(comp, frame_small, section_geojson)
                if boundary and 0.25 <= ratio <= 1.80:
                    metadata.update({
                        "reference_boundary_geojson": boundary,
                        "geometry_status": "mapped_soi_reference",
                        "geometry_authoritative": False,
                        "geometry_confidence": round(confidence, 3),
                        "geometry_acre_ratio": round(ratio, 3),
                        "reference_boundary_source": "NAU Mapped SOI raster + Kansas PLSS",
                    })
                    geometry_matches += 1
            existing = parsed_fields.get(key)
            if existing is None or (existing.metadata.get("geometry_status") == "reference_missing" and metadata.get("geometry_status") != "reference_missing"):
                parsed_fields[key] = ParsedField(
                    name=display_name,
                    acres=entry.acres,
                    county="Brown" if "013 - BROWN" in group_text.upper() else None,
                    state="KS" if "20-KS" in group_text.upper() else None,
                    farm_number=entry.farm,
                    tract_number=entry.tract,
                    field_number=entry.field,
                    crop=None,
                    practice=entry.practice,
                    irrigation=None,
                    metadata=metadata,
                )
            entity_key = f"{entry.farm}|{entry.tract}|{entry.field}"
            for field_name, value in (
                ("farm_number", entry.farm),
                ("tract_number", entry.tract),
                ("field_number", entry.field),
                ("acres", entry.acres),
                ("legal_section", entry.section),
                ("township_range", entry.township_range),
                ("source_field_location_id", entry.nau_id),
                ("insurance_unit_number", metadata.get("insurance_unit_number")),
                ("source_crop", metadata.get("source_crop")),
                ("source_practice", metadata.get("source_practice")),
            ):
                out.facts.append(SourceFact("mapped_soi_field", entity_key, field_name, value, source_locator=f"page:{entry.page}", confidence=1.0))
    out.fields = list(parsed_fields.values())
    if not out.fields:
        out.warnings.append("Mapped SOI parser did not produce physical fields.")
    elif geometry_matches < len(out.fields):
        out.warnings.append(f"Mapped SOI extracted {len(out.fields)} physical field identities; {geometry_matches} received raster-georeferenced reference shapes. Reference shapes are locators only and are not promoted to authoritative AcreFit boundaries.")
    else:
        out.warnings.append(f"Mapped SOI extracted {len(out.fields)} physical field identities and raster-georeferenced all reference shapes. AcreFit requires exact MBAR/GIS or confirmed manual geometry before soil and production recommendations use a boundary.")
    membership_fields = sum(1 for f in out.fields if (f.metadata or {}).get("insurance_unit_memberships"))
    out.warnings.append(f"Insurance financial/election data was intentionally excluded. Current unit membership was anchored from NAU Total Unit Summary for {membership_fields}/{len(out.fields)} physical fields and is retained only as an APH identity bridge.")
    return out
