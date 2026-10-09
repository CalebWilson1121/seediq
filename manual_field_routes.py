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


def _reference_boundary(field) -> dict | None:
    meta = _json_obj(field.get("metadata_json"))
    ref = meta.get("reference_boundary_geojson")
    if isinstance(ref, dict) and ref.get("type") == "Feature":
        ref = ref.get("geometry")
    if not isinstance(ref, dict):
        return None
    try:
        geom = shape(ref)
        if geom.geom_type not in {"Polygon", "MultiPolygon"} or geom.is_empty:
            return None
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.geom_type not in {"Polygon", "MultiPolygon"} or geom.is_empty or not geom.is_valid:
            return None
        return mapping(geom)
    except Exception:
        return None


def _reference_boundary_auto_ready(field) -> bool:
    meta = _json_obj(field.get("metadata_json"))
    if not _reference_boundary(field):
        return False
    confidence = float(meta.get("geometry_confidence") or 0)
    return bool(
        meta.get("geometry_auto_confirm_ready")
        and meta.get("geometry_validation_status") == "pass"
        and confidence >= 0.96
    )


def _has_exact_boundary(field_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT boundary_geojson FROM field_locations WHERE field_id=? "
            "ORDER BY updated_at DESC NULLS LAST,id DESC LIMIT 1",
            (field_id,),
        ).fetchone()
    return bool(row and row.get("boundary_geojson"))


def _confirm_reference_boundary(field) -> dict:
    field_id = int(field["id"])
    ref = _reference_boundary(field)
    if not ref:
        raise ValueError("No valid SOI reference boundary is available")
    location = set_exact_boundary(field_id, ref)
    meta = _json_obj(field.get("metadata_json"))
    meta["boundary_source"] = "confirmed_soi_reference"
    meta["boundary_confirmed"] = True
    meta["management_field"] = True
    with connect() as conn:
        conn.execute(
            "UPDATE fields SET metadata_json=?::jsonb,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json_dumps(meta), field_id),
        )
        conn.execute("DELETE FROM field_soils WHERE field_id=?", (field_id,))
    return {"field_id": field_id, "location": location}


@router.post("/api/fields/{field_id}/boundary/confirm-reference")
def confirm_reference_boundary(field_id: int):
    try:
        with connect() as conn:
            field = conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()
        if not field:
            raise KeyError(f"Field {field_id} not found")
        if _has_exact_boundary(field_id):
            return {"field_id": field_id, "confirmed": False, "already_mapped": True}
        result = _confirm_reference_boundary(field)
        return {**result, "confirmed": True, "already_mapped": False}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/farms/{farm_id}/boundaries/confirm-all")
def confirm_all_reference_boundaries(farm_id: int, include_review: bool = False):
    try:
        with connect() as conn:
            farm = conn.execute("SELECT id FROM farms WHERE id=?", (farm_id,)).fetchone()
            if not farm:
                raise KeyError(f"Farm {farm_id} not found")
            fields = conn.execute("SELECT * FROM fields WHERE farm_id=? ORDER BY id", (farm_id,)).fetchall()

        confirmed = []
        already_mapped = []
        skipped = []
        for field in fields:
            field_id = int(field["id"])
            if _has_exact_boundary(field_id):
                already_mapped.append(field_id)
                continue
            if not _reference_boundary(field):
                skipped.append({"field_id": field_id, "name": field.get("name"), "reason": "No valid SOI reference boundary"})
                continue
            if not include_review and not _reference_boundary_auto_ready(field):
                meta = _json_obj(field.get("metadata_json"))
                skipped.append({
                    "field_id": field_id,
                    "name": field.get("name"),
                    "reason": "Boundary needs review before auto-confirm",
                    "geometry_confidence": meta.get("geometry_confidence"),
                    "geometry_qa_issues": meta.get("geometry_qa_issues") or [],
                })
                continue
            try:
                _confirm_reference_boundary(field)
                confirmed.append(field_id)
            except Exception as exc:
                skipped.append({"field_id": field_id, "name": field.get("name"), "reason": str(exc)[:180]})

        return {
            "farm_id": farm_id,
            "confirmed_count": len(confirmed),
            "confirmed_field_ids": confirmed,
            "already_mapped_count": len(already_mapped),
            "already_mapped_field_ids": already_mapped,
            "skipped_count": len(skipped),
            "review_count": len([x for x in skipped if x.get("reason") == "Boundary needs review before auto-confirm"]),
            "include_review": bool(include_review),
            "skipped": skipped,
            "soils_need_refresh": bool(confirmed),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
        location = set_exact_boundary(field_id, req.boundary_geojson)
        if req.acres is not None:
            with connect() as conn:
                conn.execute(
                    "UPDATE fields SET acres=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (float(req.acres), field_id),
                )
        soil = None
        soil_warning = None
        if req.enrich_soils:
            try:
                soil = enrich_field(field_id, force=True)
            except Exception as exc:
                soil_warning = f"Field saved, but SSURGO needs retry: {str(exc)[:220]}"
                with connect() as conn:
                    conn.execute("DELETE FROM field_soils WHERE field_id=?", (field_id,))
        elif req.acres is None:
            soil_warning = "Field saved without soil enrichment."

        if req.acres is None and soil and soil.get("total_area_acres"):
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
            "soil_warning": soil_warning,
            "boundary_source": "manual_draw",
        }
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

        location = set_exact_boundary(field_id, req.boundary_geojson)

        # Geometry-calculated acres are authoritative for hand edits/cuts.
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
        if req.split_parent_field_id is not None:
            existing_meta["split_parent_field_id"] = int(req.split_parent_field_id)
            existing_meta["split_created"] = True

        with connect() as conn:
            conn.execute(
                "UPDATE fields SET name=?,acres=?,irrigation=?,metadata_json=?::jsonb,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, final_acres, irrigation, json_dumps(existing_meta), field_id),
            )
            # Never leave stale soil values tied to an old boundary.
            conn.execute("DELETE FROM field_soils WHERE field_id=?", (field_id,))
            updated = conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()

        soil = None
        soil_warning = None
        if req.enrich_soils:
            try:
                soil = enrich_field(field_id, force=True)
            except Exception as exc:
                soil_warning = f"Boundary saved. SSURGO needs retry: {str(exc)[:220]}"

        _refresh_prospect_acres(int(field["farm_id"]))
        return {
            "field": row_to_dict(updated),
            "location": location,
            "soil": soil,
            "soil_warning": soil_warning,
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
        restored_acres = float(parent.get("acres") or 0) + float(child.get("acres") or 0)
        parent_soil = None
        soil_warning = None
        with connect() as conn:
            conn.execute("DELETE FROM field_soils WHERE field_id=?", (int(parent_id),))
        try:
            parent_soil = enrich_field(int(parent_id), force=True)
        except Exception as exc:
            soil_warning = f"Split restored. SSURGO needs retry: {str(exc)[:220]}"

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
            "soil_warning": soil_warning,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Split field could not be deleted: {str(exc)[:220]}") from exc
