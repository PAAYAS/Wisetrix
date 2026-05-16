"""Pydantic schemas shared across routers."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


SourceType = Literal["git", "local"]
TargetType = Literal["artifactory", "local"]
BaselineType = Literal["artifactory", "local", "none"]


class ProjectConfig(BaseModel):
    """One project's persisted configuration.

    Mirrors the dict that Streamlit writes to `projects.json` so the two
    UIs remain bit-for-bit interoperable.
    """

    source_type: SourceType = "git"
    target_type: TargetType = "artifactory"
    baseline_type: BaselineType = "none"

    # Source — git
    git_url: Optional[str] = None
    git_branch: Optional[str] = None
    source_subpath: Optional[str] = None
    # Source — local
    source_root: Optional[str] = None

    # Target — artifactory
    artifactory_url: Optional[str] = None
    target_version: Optional[str] = None
    # Target — local
    target_system: Optional[str] = None

    # Baseline — artifactory
    baseline_url: Optional[str] = None
    baseline_version: Optional[str] = None
    # Baseline — local
    baseline_system: Optional[str] = None

    # Misc
    merge_output_dir: Optional[str] = None
    jira_project_url: Optional[str] = None

    model_config = {"extra": "allow"}  # forward-compat: ignore unknown fields


class ProjectSummary(BaseModel):
    """List-view payload — adds `id` to the config."""

    id: str
    config: ProjectConfig


class ProjectCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_(). /-]+$")
    config: ProjectConfig


class ProjectUpdate(BaseModel):
    config: ProjectConfig


class TestConnectionRequest(BaseModel):
    url: str
    # git-only
    branch: Optional[str] = None


class TestConnectionResponse(BaseModel):
    success: bool
    message: str
    details: Optional[dict] = None
