from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import soil_service
from database import connect


def enrich_prospect_parallel(prospect_id: int, force: bool = False) -> dict[str, Any]:
    """Enrich a prospect's fields without exceeding the serverless request window.

    The original implementation enriched fields sequentially. On a real 81-field
    farm, USDA SDA calls were succeeding but the Vercel request timed out at 60s.
    This implementation runs a small, bounded worker pool. Existing soil rows are
    reused when force=False, so interrupted runs resume safely.
    """
    with connect() as conn:
        prospect = conn.execute(
            "SELECT * FROM prospects WHERE id=?", (prospect_id,)
        ).fetchone()
        if not prospect:
            raise KeyError(f"Prospect {prospect_id} not found")
        fields = conn.execute(
            "SELECT id FROM fields WHERE farm_id=? ORDER BY id",
            (prospect["farm_id"],),
        ).fetchall()

    field_ids = [int(row["id"]) for row in fields]
    if not field_ids:
        return {"prospect_id": prospect_id, "fields": [], "ready": 0, "errors": 0}

    def run(field_id: int) -> dict[str, Any]:
        try:
            soil = soil_service.enrich_field(field_id, force=force)
            return {"field_id": field_id, "status": "ready", "soil": soil}
        except Exception as exc:
            return {"field_id": field_id, "status": "error", "error": str(exc)}

    # Keep concurrency moderate so we speed up large farms without hammering
    # USDA Soil Data Access or exhausting database connections.
    workers = min(6, len(field_ids))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, field_id): field_id for field_id in field_ids}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda row: row["field_id"])
    ready = sum(1 for row in results if row["status"] == "ready")
    errors = len(results) - ready
    return {
        "prospect_id": prospect_id,
        "fields": results,
        "ready": ready,
        "errors": errors,
        "total": len(results),
    }


soil_service.enrich_prospect = enrich_prospect_parallel
