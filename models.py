from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SourceFact:
    entity_type: str
    entity_key: str
    field_name: str
    value: Any
    unit: Optional[str] = None
    source_locator: Optional[str] = None
    confidence: float = 1.0


@dataclass
class ParsedField:
    name: str
    acres: Optional[float] = None
    county: Optional[str] = None
    state: Optional[str] = None
    farm_number: Optional[str] = None
    tract_number: Optional[str] = None
    field_number: Optional[str] = None
    crop: Optional[str] = None
    practice: Optional[str] = None
    irrigation: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CropRecord:
    field_key: str
    crop_year: Optional[int] = None
    crop: Optional[str] = None
    practice: Optional[str] = None
    planted_acres: Optional[float] = None
    production: Optional[float] = None
    yield_value: Optional[float] = None
    approved_yield: Optional[float] = None
    coverage_level: Optional[float] = None
    unit_structure: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    document_type: str
    producer_name: Optional[str] = None
    farm_name: Optional[str] = None
    policy_number: Optional[str] = None
    fields: list[ParsedField] = field(default_factory=list)
    crop_records: list[CropRecord] = field(default_factory=list)
    facts: list[SourceFact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_preview: Optional[str] = None
