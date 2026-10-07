from __future__ import annotations

from typing import Any

ENGINE_VERSION = "agronomy-rules-1.0.0"

# SeedIQ uses these as neutral agronomy references. The summaries below are
# intentionally short principles, not copied extension text. Rules fire only
# when SeedIQ has supporting field/product evidence.
SOURCES = [
    {
        "id": "umn-corn-hybrid-selection",
        "organization": "University of Minnesota Extension",
        "title": "Selecting corn hybrids for grain production",
        "url": "https://extension.umn.edu/agriculture/crop-production/corn/selecting-corn-hybrids-for-grain-production",
        "principles": ["maturity adaptation", "multi-environment consistency", "emergence", "root/stalk strength", "drought and disease tolerance", "maturity diversification"],
    },
    {
        "id": "isu-soy-variety-selection",
        "organization": "Iowa State University Extension",
        "title": "Soybean Variety Selection",
        "url": "https://crops.extension.iastate.edu/encyclopedia/soybean-variety-selection",
        "principles": ["yield consistency", "maturity", "disease history", "standability", "seed economics", "soil/drainage matching"],
    },
    {
        "id": "isu-cultivar-selection",
        "organization": "Iowa State University Extension",
        "title": "Choosing a Corn Hybrid or Soybean Variety",
        "url": "https://crops.extension.iastate.edu/cropnews/2019/11/choosing-corn-hybrid-or-soybean-variety",
        "principles": ["multi-year multi-location evidence", "environment matching", "drydown", "seed-cost tradeoffs"],
    },
    {
        "id": "ksu-corn-handbook",
        "organization": "Kansas State Research and Extension",
        "title": "Corn Production Handbook",
        "url": "https://bookstore.ksre.ksu.edu/pubs/corn-production-handbook_C560.pdf",
        "principles": ["maturity by environment", "yield stability", "lodging", "drought tolerance", "population by hybrid/environment", "multiple information sources"],
    },
    {
        "id": "ksu-soy-maturity-2026",
        "organization": "Kansas State Research and Extension",
        "title": "Soybean Planting Date and Maturity Group Selection for Kansas",
        "url": "https://eupdate.agronomy.ksu.edu/article/soybean-planting-date-and-maturity-group-selection-for-kansas-688-3",
        "principles": ["Kansas maturity adaptation", "early planting emergence", "SCN/SDS resistance"],
    },
    {
        "id": "ksu-performance-tests",
        "organization": "Kansas State Research and Extension",
        "title": "Kansas Performance Tests",
        "url": "https://bookstore.ksre.ksu.edu/item/2024-kansas-performance-tests-with-corn-soybean-and-sunflower-varieties_SRP1187",
        "principles": ["unbiased regional performance", "environmental stability", "multi-location evaluation"],
    },
    {
        "id": "unl-corn-seeding-rate",
        "organization": "University of Nebraska-Lincoln CropWatch",
        "title": "Nebraska On-Farm Research Corn Seeding Rate Studies",
        "url": "https://cropwatch.unl.edu/corn-seeding-rate-2015-0/",
        "principles": ["population economics", "irrigated vs rainfed population", "hybrid-specific population response", "on-farm validation"],
    },
    {
        "id": "unl-dryland-corn",
        "organization": "University of Nebraska-Lincoln CropWatch",
        "title": "Impact of Hybrid Selection, Planting Date and Seeding Rates on Dryland Corn",
        "url": "https://cropwatch.unl.edu/2019/impact-hybrid-selection-planting-date-seeding-rates-dryland-corn/",
        "principles": ["dryland population", "planting date", "hybrid by environment interaction", "economic optimum"],
    },
    {
        "id": "unl-soy-disease",
        "organization": "University of Nebraska-Lincoln CropWatch",
        "title": "Selecting a Disease-Resistant Soybean Variety",
        "url": "https://cropwatch.unl.edu/selecting-disease-resistant-soybean-variety/",
        "principles": ["field disease history", "SCN resistance", "whole-package variety selection"],
    },
    {
        "id": "umn-soy-idc",
        "organization": "University of Minnesota Extension",
        "title": "Managing iron deficiency chlorosis in soybean",
        "url": "https://extension.umn.edu/agriculture/crop-production/nutrient-management-for-minnesota-crops/managing-iron-deficiency-chlorosis-in-soybean",
        "principles": ["IDC history", "soil-position risk", "IDC-tolerant variety selection"],
    },
    {
        "id": "umn-soy-white-mold",
        "organization": "University of Minnesota Extension",
        "title": "Sclerotinia stem rot (white mold) on soybean",
        "url": "https://extension.umn.edu/agriculture/crop-production/soybean/sclerotinia-stem-rot-white-mold-on-soybean",
        "principles": ["white mold variety tolerance", "population and row-spacing risk", "rotation"],
    },
    {
        "id": "umn-scn",
        "organization": "University of Minnesota Extension",
        "title": "Soybean cyst nematode",
        "url": "https://extension.umn.edu/agriculture/crop-production/soybean/soybean-cyst-nematode-scn",
        "principles": ["SCN risk", "stress interaction", "rotation and resistant genetics"],
    },
    {
        "id": "mu-soy-selection",
        "organization": "University of Missouri Extension",
        "title": "Soybean Variety Selection",
        "url": "https://extension.missouri.edu/publications/g4412",
        "principles": ["maturity", "standability", "pest resistance", "yield stability", "environment-specific performance"],
    },
    {
        "id": "uw-grain-selection",
        "organization": "University of Wisconsin Extension",
        "title": "Considerations for Selecting Annual Grain Hybrids and Varieties",
        "url": "https://cropsandsoils.extension.wisc.edu/articles/considerations-for-selecting-annual-grain-hybrids-and-varieties/",
        "principles": ["replicated multi-site performance", "profitability", "genetic yield potential", "weather interaction"],
    },
    {
        "id": "purdue-late-corn",
        "organization": "Purdue Extension",
        "title": "Late Planted Corn Hybrid Decisions & Growing Degree Day Compression",
        "url": "https://ag.purdue.edu/news/department/agry/kernel-news/2026/04/late-planted-corn-hybrid-decisions-gdd.html",
        "principles": ["planting-date maturity adjustment", "GDD-based maturity risk"],
    },
    {
        "id": "penn-state-corn-selection",
        "organization": "Penn State Extension",
        "title": "Considerations for Selecting Corn Hybrids in Pennsylvania",
        "url": "https://extension.psu.edu/considerations-for-selecting-corn-hybrids-in-pennsylvania",
        "principles": ["profitability", "maturity adaptation", "disease-specific resistance", "standability", "multi-trial performance", "drydown"],
    },
    {
        "id": "sdsu-seed-selection",
        "organization": "South Dakota State University Extension",
        "title": "Using Data for Better Seed Selection",
        "url": "https://extension.sdstate.edu/using-data-better-seed-selection",
        "principles": ["multi-location consistency", "field disease history", "emergence and vigor", "lodging", "drydown", "trait necessity"],
    },
    {
        "id": "sdsu-corn-population",
        "organization": "South Dakota State University Extension",
        "title": "Corn Planting Populations: A Deeper Dive",
        "url": "https://extension.sdstate.edu/corn-planting-populations-deeper-dive",
        "principles": ["rainfall and geography", "hybrid-specific population", "seed cost", "soil productivity", "yield potential", "economic optimum"],
    },
    {
        "id": "illinois-corn-management",
        "organization": "University of Illinois Extension",
        "title": "Illinois Corn Management",
        "url": "https://extension.illinois.edu/sites/default/files/2025-03/illinois-corn-management.pdf",
        "principles": ["maturity", "yield potential", "standability", "disease and pest resistance", "multi-location consistency"],
    },
    {
        "id": "msu-seed-selection",
        "organization": "Michigan State University Extension",
        "title": "Seed Selection: Beyond Yield and Disease Resistance",
        "url": "https://www.canr.msu.edu/farm_management/uploads/files/Seed%20Selection%20Beyond%20Yield%20and%20Disease%20Resistance%20%28Corn%20Edition%29.pdf",
        "principles": ["profitability", "adaptation to soil and management", "yield potential", "disease resistance", "local experience"],
    },
    {
        "id": "science-for-success-soy",
        "organization": "Science for Success / U.S. Extension soybean specialists",
        "title": "Keys to Success: Choosing the Right Soybean Variety",
        "url": "https://www.canr.msu.edu/agronomy/Extension/Science%20for%20Success-%20VarietySelection.pdf",
        "principles": ["regional maturity adaptation", "genetic diversification", "field-specific stress avoidance", "profitability"],
    }
]


def _rating(chars: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        v = chars.get(key.upper())
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _strength_from_low_rating(value: float | None) -> float:
    """0..1 where lower company rating is stronger. Unknown stays neutral."""
    if value is None:
        return 0.5
    v = max(1.0, min(9.0, float(value)))
    return (9.0 - v) / 8.0


def _rule(rule_id: str, label: str, delta: float, evidence: str, source_ids: list[str], severity: str = "weighted") -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "label": label,
        "delta": round(float(delta), 1),
        "evidence": evidence,
        "source_ids": source_ids,
        "severity": severity,
    }


def evaluate_agronomy(
    *,
    crop: str,
    irrigation: str | None,
    awc: float | None,
    slope: float | None,
    drainage: dict[str, Any] | None,
    tillage: str | None,
    row_spacing: str | None,
    planting_window: str | None,
    yield_goal: float | None,
    production_context: dict[str, Any] | None,
    product: dict[str, Any],
    product_meta: dict[str, Any],
    chars: dict[str, str],
    tags: set[str],
    maturity_window: dict[str, float] | None,
) -> dict[str, Any]:
    crop = (crop or "").upper()
    if crop == "SOYBEAN":
        crop = "SOYBEANS"
    irr = (irrigation or "").upper()
    till = (tillage or "").upper()
    rows = (row_spacing or "NORMAL").upper()
    plant = (planting_window or "NORMAL").upper()
    context = production_context or {}
    fired: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Neutral starting point. This score is blended with the existing
    # field/product fit model instead of simply adding more points to a 99 cap.
    score = 72.0

    maturity = product.get("relative_maturity")
    if maturity is not None and maturity_window:
        target = float(maturity_window["target"])
        span = max(0.5, (float(maturity_window["max"]) - float(maturity_window["min"])) / 2.0)
        distance = abs(float(maturity) - target)
        closeness = max(0.0, 1.0 - distance / span)
        delta = 7.0 * closeness
        score += delta
        fired.append(_rule(
            "maturity-adaptation",
            "Adapted maturity",
            delta,
            f"RM {float(maturity):g} is {distance:.1f} from the local target of {target:g}.",
            ["umn-corn-hybrid-selection", "ksu-corn-handbook", "ksu-soy-maturity-2026", "purdue-late-corn"],
        ))

    drainage_text = " ".join(str(k).lower() for k in (drainage or {}).keys())
    wet = any(x in drainage_text for x in ("poor", "somewhat poor", "very poor"))
    well = any(x in drainage_text for x in ("well drained", "moderately well drained", "excessively drained"))

    if crop == "CORN":
        drought = _rating(chars, "DROUGHT_TOLERANCE_USCB", "DROUGHT TOLERANCE")
        root = _rating(chars, "ROOT_STRENGTH_USCB", "ROOT STRENGTH")
        stalk = _rating(chars, "STALK_STRENGTH_USCB", "STALK STRENGTH")
        emergence = _rating(chars, "EMERGENCE_USCB", "EMERGENCE")

        if irr == "NIRR":
            water_limited = awc is not None and float(awc) < 22
            if water_limited or float(context.get("hot_dry_sensitivity") or 0) >= 0.08:
                d = 7.0 * _strength_from_low_rating(drought)
                r = 3.0 * _strength_from_low_rating(root)
                if "drought" in tags or "stress" in tags:
                    d += 2.0
                delta = min(11.0, d + r)
                score += delta
                fired.append(_rule(
                    "corn-dryland-stress-package",
                    "Dryland stress package",
                    delta,
                    f"NIRR field with {'lower water-holding capacity' if water_limited else 'documented hot/dry APH downside'}; drought and root strength receive extra weight.",
                    ["ksu-corn-handbook", "unl-dryland-corn", "umn-corn-hybrid-selection"],
                ))
        elif irr == "IRR":
            delta = 0.0
            if "irrigated" in tags:
                delta += 3.0
            if "high_yield" in tags or "high_management" in tags:
                delta += 3.0
            delta += 2.0 * _strength_from_low_rating(stalk)
            if delta:
                score += delta
                fired.append(_rule(
                    "corn-irrigated-offense",
                    "Irrigated yield environment",
                    delta,
                    "Irrigation supports more offensive yield positioning while retaining standability.",
                    ["ksu-corn-handbook", "unl-corn-seeding-rate", "umn-corn-hybrid-selection"],
                ))

        if (slope is not None and float(slope) >= 4.0) or wet:
            delta = 5.0 * _strength_from_low_rating(root)
            score += delta
            fired.append(_rule(
                "corn-root-harvestability",
                "Root strength under field pressure",
                delta,
                "Slope/drainage conditions increase the value of root strength and harvestability.",
                ["umn-corn-hybrid-selection", "ksu-corn-handbook"],
            ))

        if plant == "EARLY":
            delta = 5.0 * _strength_from_low_rating(emergence)
            score += delta
            fired.append(_rule(
                "corn-early-emergence",
                "Early-planting emergence",
                delta,
                "Early planting increases the value of strong emergence under cool/wet seedbed conditions.",
                ["umn-corn-hybrid-selection"],
            ))

        pop_min = product.get("population_min")
        pop_target = product.get("population_target")
        pop_max = product.get("population_max")
        if pop_target:
            fired.append(_rule(
                "hybrid-population-response",
                "Hybrid-specific population response",
                2.0,
                f"Product catalog target population is {int(pop_target):,}; SeedIQ should stay within the product response curve when field economics allow.",
                ["unl-corn-seeding-rate", "ksu-corn-handbook"],
            ))
            score += 2.0
        if pop_min and pop_max and int(pop_min) > int(pop_max):
            warnings.append("Product population range is internally inconsistent; population rule ignored.")

    elif crop == "SOYBEANS":
        stand = _rating(chars, "STANDABILITY_USCB", "STANDABILITY")
        emerge = _rating(chars, "EMERGENCE_USCB", "EMERGENCE")
        prr = _rating(chars, "PRR_TOLERANCE_USCB", "PRR FIELD TOLERANCE")
        prr_gene = chars.get("PRR_GENE_USCB") or chars.get("PRR GENE")

        if wet:
            delta = 7.0 * _strength_from_low_rating(prr)
            gene_bonus = 3.0 if prr_gene and str(prr_gene).lower() not in {"susc", "susceptible", "-", "none"} else 0.0
            delta += gene_bonus
            score += delta
            fired.append(_rule(
                "soy-wet-soil-prr",
                "Wet-soil Phytophthora protection",
                delta,
                "Poorer drainage raises the value of Phytophthora field tolerance" + (f" and {prr_gene} resistance." if gene_bonus else "."),
                ["isu-soy-variety-selection", "unl-soy-disease"],
            ))
        elif well:
            score += 1.0

        if plant == "EARLY":
            delta = 4.0 * _strength_from_low_rating(emerge)
            score += delta
            fired.append(_rule(
                "soy-early-emergence",
                "Early soybean emergence",
                delta,
                "Early planting raises the importance of vigor/emergence and disease protection.",
                ["ksu-soy-maturity-2026"],
            ))

        high_lodging_pressure = bool((yield_goal is not None and float(yield_goal) >= 60) or rows in {"15_IN", "20_IN", "TWIN_ROW"})
        if high_lodging_pressure:
            delta = 4.0 * _strength_from_low_rating(stand)
            score += delta
            fired.append(_rule(
                "soy-standability",
                "Soybean standability",
                delta,
                "Higher-yield or narrower-row environments increase the value of standability.",
                ["isu-soy-variety-selection", "mu-soy-selection"],
            ))

    stability = context.get("stability_score")
    years = int(context.get("year_count") or 0)
    if years >= 3 and stability is not None:
        if float(stability) < 65:
            defensive = any(x in tags for x in ("stress", "drought", "broad_acre", "root_strength"))
            delta = 4.0 if defensive else -2.0
            score += delta
            fired.append(_rule(
                "aph-variable-environment",
                "Variable APH environment",
                delta,
                f"{years}-year APH stability is {float(stability):.0f}/100; variable fields favor broadly adapted or defensive genetics.",
                ["isu-cultivar-selection", "ksu-corn-handbook", "uw-grain-selection"],
            ))
        elif float(stability) >= 80 and ("high_yield" in tags or "high_management" in tags):
            score += 3.0
            fired.append(_rule(
                "aph-stable-offense",
                "Stable APH supports offense",
                3.0,
                f"{years}-year APH stability is {float(stability):.0f}/100; a stable environment can support more top-end yield emphasis.",
                ["isu-cultivar-selection", "ksu-corn-handbook", "uw-grain-selection"],
            ))

    if till == "NO_TILL":
        nt = _rating(chars, "NO_TILL_ADAPTABILITY_USCB", "NO-TILL ADAPTABILITY")
        if nt is not None:
            delta = 4.0 * _strength_from_low_rating(nt)
            score += delta
            fired.append(_rule(
                "no-till-adaptation",
                "No-till adaptation",
                delta,
                "The farm is no-till, so product no-till adaptation receives explicit weight.",
                ["isu-soy-variety-selection", "isu-cultivar-selection"],
            ))

    # Do not pretend unavailable disease history exists. These warnings make
    # missing high-value inputs visible to the dealer and future product roadmap.
    field_meta = product_meta.get("_field_metadata") or {}
    for disease_key, label, source_ids in [
        ("scn_history", "SCN history", ["unl-soy-disease", "umn-scn"]),
        ("idc_history", "IDC history", ["umn-soy-idc"]),
        ("white_mold_history", "white mold history", ["umn-soy-white-mold"]),
    ]:
        if crop == "SOYBEANS" and field_meta.get(disease_key) is None:
            continue
        if crop == "SOYBEANS" and field_meta.get(disease_key):
            warnings.append(f"{label} is flagged on this field; a dedicated resistance/tolerance gate should be applied when that product trait is available.")

    score = round(max(45.0, min(99.0, score)), 1)
    fired.sort(key=lambda x: abs(float(x["delta"])), reverse=True)

    source_index = {s["id"]: s for s in SOURCES}
    source_ids = []
    for r in fired:
        for sid in r["source_ids"]:
            if sid not in source_ids:
                source_ids.append(sid)

    return {
        "engine_version": ENGINE_VERSION,
        "agronomy_score": score,
        "rules_fired": fired,
        "top_reasons": [r["evidence"] for r in fired[:4]],
        "warnings": warnings,
        "sources": [source_index[sid] for sid in source_ids if sid in source_index],
    }


def rules_catalog() -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "source_count": len(SOURCES),
        "sources": SOURCES,
        "design": {
            "hard_gates": ["crop", "location/maturity eligibility"],
            "weighted_domains": [
                "water environment",
                "soil water-holding capacity",
                "drainage",
                "root/stalk strength",
                "emergence",
                "farm management",
                "APH stability",
                "weather-linked APH stress response",
                "hybrid-specific population response",
                "disease history when available",
            ],
            "principle": "Neutral agronomy defines field needs; product data describes the genetic package; farm evidence determines how much each need matters.",
        },
    }
