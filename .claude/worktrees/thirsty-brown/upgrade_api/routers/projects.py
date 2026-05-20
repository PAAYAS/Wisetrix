"""Project CRUD endpoints — full parity with the Streamlit Setup tab."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from upgrade_api.schemas import (
    ProjectConfig,
    ProjectCreate,
    ProjectSummary,
    ProjectUpdate,
)
from upgrade_api.scan_util import invalidate_resolve_cache
from upgrade_api.state import load_projects, save_projects


router = APIRouter(prefix="/projects", tags=["projects"])


def _normalize(project_id: str, cfg: ProjectConfig) -> dict:
    """Apply server-side defaults that Streamlit also applies on save."""
    data = cfg.model_dump(exclude_none=True)
    # merge_output_dir is auto-managed by the backend.
    data["merge_output_dir"] = data.get("merge_output_dir") or f"./output/{project_id}"
    return data


@router.get("", response_model=list[ProjectSummary])
def list_projects() -> list[ProjectSummary]:
    projects = load_projects()
    return [
        ProjectSummary(id=pid, config=ProjectConfig.model_validate(cfg))
        for pid, cfg in sorted(projects.items())
    ]


@router.get("/{project_id}", response_model=ProjectSummary)
def get_project(project_id: str) -> ProjectSummary:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return ProjectSummary(
        id=project_id,
        config=ProjectConfig.model_validate(projects[project_id]),
    )


@router.post("", response_model=ProjectSummary, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate) -> ProjectSummary:
    projects = load_projects()
    if payload.id in projects:
        raise HTTPException(
            status_code=409,
            detail=f"Project '{payload.id}' already exists",
        )
    projects[payload.id] = _normalize(payload.id, payload.config)
    save_projects(projects)
    return ProjectSummary(
        id=payload.id,
        config=ProjectConfig.model_validate(projects[payload.id]),
    )


@router.put("/{project_id}", response_model=ProjectSummary)
def update_project(project_id: str, payload: ProjectUpdate) -> ProjectSummary:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    invalidate_resolve_cache(projects[project_id])  # drop stale cache for old config
    projects[project_id] = _normalize(project_id, payload.config)
    save_projects(projects)
    return ProjectSummary(
        id=project_id,
        config=ProjectConfig.model_validate(projects[project_id]),
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str) -> None:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    invalidate_resolve_cache(projects[project_id])
    del projects[project_id]
    save_projects(projects)
