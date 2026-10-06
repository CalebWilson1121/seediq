from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from shapely.geometry import shape

from database import backend_name, connect, json_dumps, row_to_dict
from soil_service import enrich_field, set_exact_boundary

router = APIRouter()


class ManualFieldRequest(BaseModel):
    name: str
    boundary_geojson: dict
    acres: float | None = None
    irrigation: str | None = None
    enrich_soils: bool = True


class FieldBoundaryUpdateRequest(BaseModel):
    name: str | None = None
    boundary_geojson: dict
    acres: float | None = None
    irrigation: str | None = None
    enrich_soils: bool = True


def _insert_field(farm_id: int, name: str, acres: float | None, irrigation: str | None) -> int:
    field_key = f"manual:{farm_id}:{uuid.uuid4().hex}"
    metadata = {
        "boundary_source": "manual_draw",
        "management_field": True,
    }
    with connect() as conn:
        farm = conn.execute("SELECT id,state,county FROM farms WHERE id=?", (farm_id,)).fetchone()
        if not farm:
            raise KeyError(f"Farm {farm_id} not found")
        sql = "INSERT INTO fields(farm_id,field_key,name,acres,state,county,irrigation,metadata_json) VALUES(?,?,?,?,?,?,?,?)"
        params = (farm_id, field_key, name, acres, farm.get("state"), farm.get("county"), irrigation, json_dumps(metadata))
        if backend_name() == "supabase-postgres":
            row = conn.execute(sql + " RETURNING id", params).fetchone()
            return int(row["id"])
        cur = conn.execute(sql, params)
        return int(cur.lastrowid)


def _refresh_prospect_acres(farm_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE prospects SET total_acres=(SELECT COALESCE(SUM(acres),0) FROM fields WHERE farm_id=?), updated_at=CURRENT_TIMESTAMP WHERE farm_id=?",
            (farm_id, farm_id),
        )


@router.post("/api/farms/{farm_id}/fields/manual")
def create_manual_field(farm_id: int, req: ManualFieldRequest):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Field name is required")

    try:
        geom = shape(req.boundary_geojson)
        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Field boundary must be a Polygon or MultiPolygon")
        if geom.is_empty or not geom.is_valid:
            raise ValueError("Field boundary is empty or invalid")
        if req.acres is not None and req.acres <= 0:
            raise ValueError("Calculated acres must be greater than zero")

        irrigation = (req.irrigation or "").upper().strip() or None
        if irrigation not in {None, "NIRR", "IRR"}:
            raise ValueError("Irrigation must be NIRR or IRR")
        field_id = _insert_field(farm_id, name, req.acres, irrigation)
        try:
            location = set_exact_boundary(field_id, req.boundary_geojson)
            soil = enrich_field(field_id, force=True) if req.enrich_soils else None
            if soil and soil.get("total_area_acres"):
                with connect() as conn:
                    conn.execute(
                        "UPDATE fields SET acres=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (float(soil["total_area_acres"]), field_id),
                    )
            _refresh_prospect_acres(farm_id)
            with connect() as conn:
                field = conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()
            return {
                "field": row_to_dict(field),
                "location": location,
                "soil": soil,
                "boundary_source": "manual_draw",
            }
        except Exception:
            with connect() as conn:
                conn.execute("DELETE FROM fields WHERE id=?", (field_id,))
            raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Drawn field could not be saved: {str(exc)[:220]}") from exc


@router.put("/api/fields/{field_id}/boundary")
def update_field_boundary(field_id: int, req: FieldBoundaryUpdateRequest):
    try:
        with connect() as conn:
            field = conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()
            if not field:
                raise KeyError(f"Field {field_id} not found")
            old_location = conn.execute("SELECT * FROM field_locations WHERE field_id=?", (field_id,)).fetchone()

        geom = shape(req.boundary_geojson)
        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Field boundary must be a Polygon or MultiPolygon")
        if geom.is_empty or not geom.is_valid:
            raise ValueError("Field boundary is empty or invalid")
        if req.acres is not None and req.acres <= 0:
            raise ValueError("Calculated acres must be greater than zero")

        irrigation = (req.irrigation or field.get("irrigation") or "").upper().strip() or None
        if irrigation not in {None, "NIRR", "IRR"}:
            raise ValueError("Irrigation must be NIRR or IRR")
        name = (req.name or field.get("name") or "").strip()
        if not name:
            raise ValueError("Field name is required")

        try:
            location = set_exact_boundary(field_id, req.boundary_geojson)
            soil = enrich_field(field_id, force=True) if req.enrich_soils else None
        except Exception:
            # Never leave a saved field pointing at a failed edit. Restore its
            # previous boundary if USDA enrichment rejects the new geometry.
            if old_location and old_location.get("boundary_geojson"):
                from soil_service import _loads
                set_exact_boundary(field_id, _loads(old_location.get("boundary_geojson"), {}))
            raise

        final_acres = float(req.acres) if req.acres is not None else float(field.get("acres") or 0)
        if soil and soil.get("total_area_acres"):
            final_acres = float(soil["total_area_acres"])

        with connect() as conn:
            conn.execute(
                "UPDATE fields SET name=?,acres=?,irrigation=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, final_acres, irrigation, field_id),
            )
            updated = conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()

        _refresh_prospect_acres(int(field["farm_id"]))
        return {
            "field": row_to_dict(updated),
            "location": location,
            "soil": soil,
            "boundary_source": "edited_boundary",
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Field edit could not be saved: {str(exc)[:220]}") from exc
