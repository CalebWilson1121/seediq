from __future__ import annotations

from typing import Any

# Deterministic scoring stays outside the LLM.
WEIGHTS = {
    "drought": 0.25,
    "wet_soil": 0.15,
    "emergence": 0.10,
    "root": 0.15,
    "stalk": 0.10,
    "disease": 0.10,
    "yield_ceiling": 0.15,
}


def _compat(risk_or_need: float, trait: float) -> float:
    risk_or_need = max(0.0, min(100.0, risk_or_need))
    trait = max(0.0, min(10.0, trait)) * 10.0
    return 100.0 - abs(risk_or_need - trait)


def score_seed(field_profile: dict[str, float], seed: dict[str, Any]) -> dict[str, Any]:
    components = {
        "drought": _compat(field_profile.get("drought_risk", 50), seed.get("drought_score", 5)),
        "wet_soil": _compat(field_profile.get("wet_risk", 50), seed.get("wet_soil_score", 5)),
        "emergence": _compat(field_profile.get("emergence_need", 50), seed.get("emergence_score", 5)),
        "root": _compat(field_profile.get("root_need", 50), seed.get("root_score", 5)),
        "stalk": _compat(field_profile.get("stalk_need", 50), seed.get("stalk_score", 5)),
        "disease": _compat(field_profile.get("disease_risk", 50), seed.get("disease_score", 5)),
        "yield_ceiling": _compat(field_profile.get("yield_environment", 70), seed.get("yield_ceiling", 7)),
    }
    fit = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)
    return {"fit_score": round(fit, 1), "components": {k: round(v, 1) for k, v in components.items()}}


def rank_seeds(field_profile: dict[str, float], seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for s in seeds:
        ranked.append({**s, **score_seed(field_profile, s)})
    ranked.sort(key=lambda x: x["fit_score"], reverse=True)
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    return ranked
