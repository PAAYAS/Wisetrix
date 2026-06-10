"""Database (seed-data) endpoints.

Downstream of the app upgrade. For every `bizpolicydefs` artifact the app's
decision drives a DB action:

    Remove → advise removing the bppol workbook from the DB git check-in
    Retain → keep the workbook as-is
    Merge  → reconcile the merged bizpolicydef.json into a new bppol workbook

GET  /projects/{id}/db/status              → enablement + readiness flags
POST /projects/{id}/db/scan                → derive actions from app comparison
GET  /projects/{id}/db/scan                → last derived actions
GET  /projects/{id}/db/merges              → DB reconciliation records
POST /projects/{id}/db/merges/{key:path}   → reconcile one policy
POST /projects/{id}/db/merges              → reconcile all ready policies
GET  /projects/{id}/db/merges/{key:path}/download → download one new xlsx
GET  /projects/{id}/db/download            → zip all DB outputs

All work here is deterministic Python (openpyxl) — no Claude.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from upgrade_api.merge_util import zip_directory
from upgrade_api.paths import (
    comparison_path,
    db_comparison_path,
    db_merge_report_path,
    load_json,
    merge_report_path,
    save_json,
)
from upgrade_api.scan_util import resolve_db_source_root
from upgrade_api.state import load_projects
from upgrade_lib.db.db_scan import derive_db_actions
from upgrade_lib.db.paths import find_bppol_file
from upgrade_lib.db.xlsx_merge import merge_policy_into_workbook

router = APIRouter(prefix="/projects/{project_id}/db", tags=["database"])

MERGED_JSON_NAME = "bizpolicydef.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _project_or_404(project_id: str) -> dict:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return projects[project_id]


def _db_output_dir(project_id: str, project: dict) -> Path:
    return Path(project.get("db_output_dir") or f"./output/{project_id}/_db")


def _merge_output_dir(project_id: str, project: dict) -> Path:
    return Path(project.get("merge_output_dir") or f"./output/{project_id}")


def _zip_filename_header(name: str) -> dict:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


def _require_app_scan(project_id: str) -> dict:
    comp = load_json(comparison_path(project_id), {})
    if not comp:
        raise HTTPException(
            400,
            "No app comparison found. Run the app Scan & Compare first — the "
            "DB step is driven by the app's bizpolicydefs decisions.",
        )
    return comp


# --------------------------------------------------------------------------- #
# Status & scan
# --------------------------------------------------------------------------- #

@router.get("/status")
def db_status(project_id: str) -> dict:
    project = _project_or_404(project_id)
    comp = load_json(comparison_path(project_id), {})
    return {
        "enabled": bool(project.get("db_enabled")),
        "app_scanned": bool(comp),
        "has_actions": db_comparison_path(project_id).exists(),
    }


@router.post("/scan")
async def db_scan(project_id: str) -> dict:
    project = _project_or_404(project_id)
    if not project.get("db_enabled"):
        raise HTTPException(400, "Database handling is not enabled for this project.")
    comp = _require_app_scan(project_id)
    merge_report = load_json(merge_report_path(project_id), {})

    # Resolve the DB repo (clone if needed) — DB-only, never touches Artifactory.
    db_res = await anyio.to_thread.run_sync(
        lambda: resolve_db_source_root(project, skip_pull=True)
    )
    db_root = db_res.get("db_source_root", "")
    resolve_error = db_res.get("error")

    actions = derive_db_actions(comp, db_root, merge_report)
    payload = {
        "db_source_root": db_root,
        "resolve_error": resolve_error,
        "actions": actions,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(db_comparison_path(project_id), payload)
    return payload


@router.get("/scan")
def db_scan_get(project_id: str) -> dict:
    _project_or_404(project_id)
    return load_json(db_comparison_path(project_id), {"actions": []})


# --------------------------------------------------------------------------- #
# Merge (reconcile)
# --------------------------------------------------------------------------- #

def _reconcile_one(
    project_id: str,
    project: dict,
    key: str,
    comp: dict,
    db_root: str,
) -> dict[str, Any]:
    entry = comp.get(key)
    if not entry:
        raise HTTPException(404, f"Artifact '{key}' not found in comparison.")
    if entry.get("category") != "bizpolicydefs":
        raise HTTPException(400, f"'{key}' is not a bizpolicydefs artifact.")
    if entry.get("decision") != "Merge":
        raise HTTPException(
            400, f"'{key}' app decision is '{entry.get('decision')}', not Merge."
        )

    bucket = entry.get("bucket", "")
    rel = entry.get("rel_path", "")
    name = entry.get("name") or Path(rel).name

    merged_json = _merge_output_dir(project_id, project) / bucket / rel / MERGED_JSON_NAME
    if not merged_json.exists():
        raise HTTPException(
            400,
            f"App merged {MERGED_JSON_NAME} not found for '{key}'. "
            "Run the app Merge for this policy first.",
        )
    if not db_root:
        raise HTTPException(400, "DB source not resolved. Run DB scan first.")

    src_xlsx = find_bppol_file(db_root, bucket, name)
    if src_xlsx is None or not src_xlsx.exists():
        raise HTTPException(404, f"No bppol workbook found in DB repo for '{name}'.")

    # Preserve the source workbook's actual filename so the output can replace it.
    out_xlsx = _db_output_dir(project_id, project) / "bppol" / src_xlsx.name

    try:
        policy = json.loads(merged_json.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, f"Failed to read merged JSON: {exc}")

    result = merge_policy_into_workbook(policy, src_xlsx, out_xlsx)
    record = result.to_dict()
    record.update({
        "key": key,
        "bucket": bucket,
        "name": name,
        "rel_path": rel,
        "merged_at": datetime.now(timezone.utc).isoformat(),
    })
    return record


@router.post("/merges/{key:path}")
async def db_merge_one(project_id: str, key: str) -> dict:
    project = _project_or_404(project_id)
    comp = _require_app_scan(project_id)
    db_res = await anyio.to_thread.run_sync(
        lambda: resolve_db_source_root(project, skip_pull=True)
    )
    db_root = db_res.get("db_source_root", "")

    record = await anyio.to_thread.run_sync(
        lambda: _reconcile_one(project_id, project, key, comp, db_root)
    )
    records = load_json(db_merge_report_path(project_id), {})
    records[key] = record
    save_json(db_merge_report_path(project_id), records)
    return record


@router.post("/merges")
async def db_merge_all(project_id: str) -> dict:
    project = _project_or_404(project_id)
    comp = _require_app_scan(project_id)
    merge_report = load_json(merge_report_path(project_id), {})
    db_res = await anyio.to_thread.run_sync(
        lambda: resolve_db_source_root(project, skip_pull=True)
    )
    db_root = db_res.get("db_source_root", "")

    actions = derive_db_actions(comp, db_root, merge_report)
    ready = [a for a in actions if a["action_type"] == "merge" and a["ready"]]

    records = load_json(db_merge_report_path(project_id), {})
    done: list[str] = []
    failed: list[dict] = []
    for a in ready:
        try:
            rec = await anyio.to_thread.run_sync(
                lambda k=a["key"]: _reconcile_one(project_id, project, k, comp, db_root)
            )
            records[a["key"]] = rec
            done.append(a["key"])
        except HTTPException as exc:
            failed.append({"key": a["key"], "error": exc.detail})
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": a["key"], "error": str(exc)})

    save_json(db_merge_report_path(project_id), records)
    return {"merged": done, "failed": failed, "total": len(ready)}


@router.get("/merges")
def db_merges_list(project_id: str) -> dict:
    _project_or_404(project_id)
    return {"done": load_json(db_merge_report_path(project_id), {})}


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #

@router.get("/merges/{key:path}/download")
def db_download_one(project_id: str, key: str) -> Response:
    _project_or_404(project_id)
    records = load_json(db_merge_report_path(project_id), {})
    if key not in records:
        raise HTTPException(404, f"'{key}' has not been reconciled yet.")
    out_xlsx = Path(records[key].get("output_xlsx", ""))
    if not out_xlsx.exists():
        raise HTTPException(404, f"Output workbook missing on disk: {out_xlsx}")
    data = out_xlsx.read_bytes()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=_zip_filename_header(out_xlsx.name),
    )


@router.get("/download")
def db_download_all(project_id: str) -> Response:
    project = _project_or_404(project_id)
    out_root = _db_output_dir(project_id, project)
    if not out_root.exists():
        raise HTTPException(404, "No DB output yet.")
    data = zip_directory(out_root, arc_root=f"{project_id}_db")
    if not data:
        raise HTTPException(404, "DB output directory is empty.")
    return Response(
        content=data,
        media_type="application/zip",
        headers=_zip_filename_header(f"{project_id}-db.zip"),
    )
