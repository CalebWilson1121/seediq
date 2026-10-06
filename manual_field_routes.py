from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

from database import backend_name, connect, json_dumps, row_to_dict
from soil_service import enrich_field, set_exact_boundary

router = APIRouter()


class ManualFieldRequest(BaseModel):
    name: str
    boundary_geojson: dict
    acres: float | None = None
    irrigation: str | None = None
    enrich_soils: bool = True
    base_boundary_geojson: dict | None = None
    cut_polygons: list[dict] | None = None
    split_parent_field_id: int | None = None


class FieldBoundaryUpdateRequest(BaseModel):
    name: str | None = None
    boundary_geojson: dict
    acres: float | None = None
    irrigation: str | None = None
    enrich_soils: bool = True
    base_boundary_geojson: dict | None = None
    cut_polygons: list[dict] | None = None
    split_parent_field_id: int | None = None


def _boundary_edit_metadata(base_boundary_geojson, cut_polygons, split_parent_field_id=None):
    meta = {
        "boundary_source": "manual_draw",
        "management_field": True,
        "boundary_edit": {
            "base_boundary_geojson": base_boundary_geojson,
            "cut_polygons": cut_polygons or [],
        },
    }
    if split_parent_field_id is not None:
        meta["split_parent_field_id"] = int(split_parent_field_id)
        meta["split_created"] = True
    return meta


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
        with connect() as conn:
            conn.execute(
                "UPDATE fields SET metadata_json=?::jsonb WHERE id=?",
                (json_dumps(_boundary_edit_metadata(req.base_boundary_geojson or req.boundary_geojson, req.cut_polygons, req.split_parent_field_id)), field_id),
            )
        try:
            location = set_exact_boundary(field_id, req.boundary_geojson)
            soil = enrich_field(field_id, force=True) if req.enrich_soils else None
            if req.acres is not None:
                with connect() as conn:
                    conn.execute(
                        "UPDATE fields SET acres=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (float(req.acres), field_id),
                    )
            elif soil and soil.get("total_area_acres"):
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

        # Geometry-calculated acres are authoritative for hand edits/cuts.
        # SSURGO intersection acreage is descriptive soil coverage, not the
        # canonical management-field acreage.
        final_acres = float(req.acres) if req.acres is not None else float(field.get("acres") or 0)

        existing_meta = {}
        try:
            import json
            existing_meta = field.get("metadata_json") if isinstance(field.get("metadata_json"), dict) else json.loads(field.get("metadata_json") or "{}")
        except Exception:
            existing_meta = {}
        existing_meta["boundary_edit"] = {
            "base_boundary_geojson": req.base_boundary_geojson or req.boundary_geojson,
            "cut_polygons": req.cut_polygons or [],
        }
        existing_meta["boundary_source"] = "edited_boundary"
        with connect() as conn:
            conn.execute(
                "UPDATE fields SET name=?,acres=?,irrigation=?,metadata_json=?::jsonb,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, final_acres, irrigation, json_dumps(existing_meta), field_id),
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


def _json_obj(value):
    if isinstance(value, dict):
        return value
    try:
        import json
        return json.loads(value or "{}")
    except Exception:
        return {}


@router.delete("/api/fields/{field_id}")
def delete_split_field(field_id: int):
    try:
        with connect() as conn:
            child = conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()
            if not child:
                raise KeyError(f"Field {field_id} not found")
            meta = _json_obj(child.get("metadata_json"))
            parent_id = meta.get("split_parent_field_id")
            if parent_id is None:
                import re
                name = str(child.get("name") or "").strip()
                base_name = re.sub(r"\s+new(?:\s+\d+)?$", "", name, flags=re.I).strip()
                if base_name != name:
                    parent = conn.execute(
                        "SELECT * FROM fields WHERE farm_id=? AND lower(name)=lower(?) AND id<>? ORDER BY id LIMIT 1",
                        (child["farm_id"], base_name, field_id),
                    ).fetchone()
                    if parent:
                        parent_id = int(parent["id"])
            if parent_id is None:
                raise ValueError("Only split-created fields can be deleted here")

            parent = conn.execute("SELECT * FROM fields WHERE id=?", (int(parent_id),)).fetchone()
            child_loc = conn.execute("SELECT * FROM field_locations WHERE field_id=?", (field_id,)).fetchone()
            parent_loc = conn.execute("SELECT * FROM field_locations WHERE field_id=?", (int(parent_id),)).fetchone()
            if not parent or not child_loc or not parent_loc:
                raise ValueError("Split field or parent boundary is unavailable")

        from soil_service import _loads
        merged = unary_union([
            shape(_loads(parent_loc.get("boundary_geojson"), {})),
            shape(_loads(child_loc.get("boundary_geojson"), {})),
        ])
        if not merged.is_valid:
            merged = merged.buffer(0)
        if merged.geom_type not in {"Polygon", "MultiPolygon"} or merged.is_empty:
            raise ValueError("Could not restore split acreage to the parent field")

        merged_geo = mapping(merged)
        parent_location = set_exact_boundary(int(parent_id), merged_geo)
        parent_soil = enrich_field(int(parent_id), force=True)
        restored_acres = float(parent.get("acres") or 0) + float(child.get("acres") or 0)

        with connect() as conn:
            pmeta = _json_obj(parent.get("metadata_json"))
            pmeta["boundary_source"] = "split_undo"
            pmeta["boundary_edit"] = {"base_boundary_geojson": merged_geo, "cut_polygons": []}
            conn.execute(
                "UPDATE fields SET acres=?,metadata_json=?::jsonb,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (round(restored_acres, 2), json_dumps(pmeta), int(parent_id)),
            )
            conn.execute("DELETE FROM fields WHERE id=?", (field_id,))

        _refresh_prospect_acres(int(child["farm_id"]))
        return {
            "deleted_field_id": field_id,
            "restored_parent_field_id": int(parent_id),
            "restored_parent_acres": round(restored_acres, 2),
            "location": parent_location,
            "soil": parent_soil,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Split field could not be deleted: {str(exc)[:220]}") from exc
