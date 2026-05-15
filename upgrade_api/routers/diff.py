"""Diff Viewer + Review endpoints.

GET  /projects/{id}/diff/{key:path}        -> per-file aldi/system/merged contents
POST /projects/{id}/review/{key:path}      -> Claude structured review
"""

from __future__ import annotations

from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException

from upgrade_api.merge_util import primary_customer_bucket, read_artifact_files
from upgrade_api.paths import comparison_path, load_json, merge_report_path
from upgrade_api.scan_util import resolve_project_paths
from upgrade_api.state import load_projects
from upgrade_lib.claude_client import UpgradeClient


router = APIRouter(prefix="/projects/{project_id}", tags=["diff"])


def _project_or_404(project_id: str) -> dict:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return projects[project_id]


def _roots_for(project_id: str, project: dict) -> tuple[Path, Path, Path, str]:
    try:
        resolved = resolve_project_paths(project)
    except Exception as e:
        raise HTTPException(400, f"Failed to resolve paths: {e}")
    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])
    out_root = Path(
        resolved.get("merge_output_dir") or project.get("merge_output_dir", "")
    )
    default_bucket = primary_customer_bucket(source_root, project_id)
    return source_root, target_root, out_root, default_bucket


def _entry_for(project_id: str, key: str) -> dict:
    comp = load_json(comparison_path(project_id), {})
    merges = load_json(merge_report_path(project_id), {})
    if key in merges:
        return merges[key]
    if key in comp:
        return comp[key]
    raise HTTPException(404, f"Artifact '{key}' not found")


@router.get("/diff/{key:path}")
def get_diff(project_id: str, key: str) -> dict:
    project = _project_or_404(project_id)
    entry = _entry_for(project_id, key)
    source_root, target_root, out_root, default_bucket = _roots_for(project_id, project)
    rel = entry.get("rel_path", key)
    bucket = entry.get("bucket") or default_bucket

    aldi = read_artifact_files(source_root / bucket / rel)
    system = read_artifact_files(target_root / rel)
    merged = read_artifact_files(out_root / bucket / rel)

    files = sorted(set(aldi) | set(system) | set(merged))
    return {
        "key": key,
        "bucket": bucket,
        "rel_path": rel,
        "files": files,
        "aldi": aldi,
        "system": system,
        "merged": merged,
        "has_merge": bool(merged),
    }


@router.post("/review/{key:path}")
async def review(project_id: str, key: str) -> dict:
    project = _project_or_404(project_id)
    merges = load_json(merge_report_path(project_id), {})
    if key not in merges:
        raise HTTPException(404, f"Artifact '{key}' has not been merged yet")
    entry = merges[key]

    source_root, target_root, out_root, default_bucket = _roots_for(project_id, project)
    rel = entry.get("rel_path", key)
    bucket = entry.get("bucket") or default_bucket

    aldi = read_artifact_files(source_root / bucket / rel)
    system = read_artifact_files(target_root / rel)
    merged = read_artifact_files(out_root / bucket / rel)

    client = UpgradeClient()
    result = await anyio.to_thread.run_sync(
        lambda: client.review_merge_structured(merged, aldi, system, customer=bucket)
    )
    return result
