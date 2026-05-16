"""JIRA tab endpoints — mirror the Streamlit JIRA tab.

GET  /projects/{id}/jira/status          -> connection + tracker state
GET  /projects/{id}/jira/issues          -> list linked epic + subtasks
POST /projects/{id}/jira/start           -> start_run (create/find epic)
POST /projects/{id}/jira/sync            -> refresh statuses of linked issues
POST /projects/{id}/jira/match           -> auto-match comparison artifacts to project issues
POST /projects/{id}/jira/finalize        -> finalize run (post final comment)
POST /projects/{id}/jira/test            -> test JIRA connection
"""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import logging

from upgrade_api.config import RUN_STATE_DIR
from upgrade_api.paths import (
    comparison_path,
    jira_sources_path,
    jira_tickets_path,
    load_json,
    merge_report_path,
    save_json,
)
from upgrade_api.state import load_projects
from upgrade_lib.jira.jira_client import JiraClient
from upgrade_lib.jira.jira_tracker import JiraTracker

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/projects/{project_id}/jira", tags=["jira"])


def _project_or_404(project_id: str) -> dict:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return projects[project_id]


def _project_key(project: dict, project_id: str) -> str:
    """Resolve the JIRA project key from project config.

    Priority: explicit `jira_project` field, else extract from `jira_project_url`,
    else fall back to the project id.
    """
    explicit = project.get("jira_project")
    if explicit:
        return str(explicit)
    url = project.get("jira_project_url") or ""
    if url:
        key = JiraClient.extract_project_key_from_url(url)
        if key:
            return key
    return project_id


def _tracker_for(project_id: str, project: dict) -> JiraTracker:
    return JiraTracker(
        project_key=_project_key(project, project_id),
        epic_key=project.get("jira_epic", "") or "",
        state_dir=RUN_STATE_DIR,
    )


class StartRunRequest(BaseModel):
    metadata: dict[str, Any] | None = None


class MatchRequest(BaseModel):
    artifact_keys: list[str] | None = None


@router.get("/status")
def status(project_id: str) -> dict:
    project = _project_or_404(project_id)
    tracker = _tracker_for(project_id, project)
    return {
        "enabled": tracker.enabled,
        "base_url": tracker.client.base_url,
        "project_key": tracker.project_key,
        "epic_key": tracker.epic_key,
        "subtasks": tracker.subtasks,
        "started_at": tracker.state.started_at,
        "finalized_at": tracker.state.finalized_at,
    }


@router.post("/test")
async def test(project_id: str) -> dict:
    _project_or_404(project_id)
    client = JiraClient()
    return await anyio.to_thread.run_sync(client.test_connection)


@router.get("/issues")
async def issues(project_id: str) -> dict:
    project = _project_or_404(project_id)
    tracker = _tracker_for(project_id, project)
    if not tracker.enabled:
        return {"enabled": False, "issues": []}
    fetched = await anyio.to_thread.run_sync(tracker.get_linked_issues)
    return {
        "enabled": True,
        "base_url": tracker.client.base_url,
        "epic_key": tracker.epic_key,
        "issues": [
            {
                "key": i.key,
                "summary": i.summary,
                "status": i.status,
                "issue_type": i.issue_type,
                "assignee": i.assignee,
                "url": i.url,
            }
            for i in fetched
        ],
    }


@router.post("/start")
async def start(project_id: str, req: StartRunRequest) -> dict:
    project = _project_or_404(project_id)
    tracker = _tracker_for(project_id, project)
    if not tracker.enabled:
        raise HTTPException(400, "JIRA is disabled — enable it in config.yaml")
    issue = await anyio.to_thread.run_sync(lambda: tracker.start_run(req.metadata or {}))
    if not issue:
        raise HTTPException(502, "Failed to start JIRA run — check server logs")
    return {
        "epic_key": issue.key,
        "summary": issue.summary,
        "status": issue.status,
        "url": issue.url,
    }


@router.post("/sync")
async def sync(project_id: str) -> dict:
    project = _project_or_404(project_id)
    tracker = _tracker_for(project_id, project)
    if not tracker.enabled:
        raise HTTPException(400, "JIRA is disabled — enable it in config.yaml")
    fetched = await anyio.to_thread.run_sync(tracker.get_linked_issues)
    return {
        "synced": len(fetched),
        "issues": [
            {
                "key": i.key,
                "summary": i.summary,
                "status": i.status,
                "issue_type": i.issue_type,
                "url": i.url,
            }
            for i in fetched
        ],
    }


@router.post("/match")
async def match(project_id: str, req: MatchRequest) -> dict:
    project = _project_or_404(project_id)
    project_key = _project_key(project, project_id)

    comp = load_json(comparison_path(project_id), {})
    merges = load_json(merge_report_path(project_id), {})

    keys = req.artifact_keys
    if not keys:
        keys = sorted(set(comp.keys()) | set(merges.keys()))

    if not keys:
        return {"matched": {}, "count": 0}

    client = JiraClient()
    if not client.enabled:
        raise HTTPException(400, "JIRA is disabled — enable it in config.yaml")

    # --- git signal: extract JIRA keys from commit history ---
    git_hits: dict[str, set[str]] = {}
    git_debug: dict[str, object] = {"source_type": project.get("source_type")}

    if project.get("source_type") == "git":
        try:
            from upgrade_lib.sources.git_provider import GitProvider
            gp = GitProvider()
            git_url = project.get("git_url", "")
            git_branch = project.get("git_branch", "main")
            subpath = project.get(
                "source_subpath",
                "client_delivery/src/main/resources/app_root/repos",
            )
            git_debug["git_url"] = git_url
            git_debug["git_branch"] = git_branch
            git_debug["subpath"] = subpath

            # Build full git paths and reverse map back to artifact keys
            art_paths: list[str] = []
            path_to_art_key: dict[str, str] = {}
            skipped_keys: list[str] = []
            source = comp if comp else merges
            for art_key in keys:
                entry = source.get(art_key, {})
                bucket = entry.get("bucket", "")
                rel = entry.get("rel_path", "")
                if bucket and rel:
                    # Use bucket/rel without the subpath prefix so it matches
                    # git diff paths regardless of whether the repo root is
                    # above or below the subpath level.
                    git_path = f"{bucket}/{rel}"
                    art_paths.append(git_path)
                    path_to_art_key[git_path] = art_key
                else:
                    skipped_keys.append(art_key)

            git_debug["art_paths_built"] = len(art_paths)
            git_debug["skipped_no_bucket_or_rel"] = len(skipped_keys)
            git_debug["sample_art_path"] = art_paths[0] if art_paths else None

            if art_paths:
                path_to_keys = await anyio.to_thread.run_sync(
                    lambda: gp.extract_jira_keys_for_paths(git_url, git_branch, art_paths)
                )
                git_debug["git_paths_matched"] = len(path_to_keys)
                git_debug["sample_git_result"] = {
                    k: list(v) for k, v in list(path_to_keys.items())[:3]
                }
                git_debug["scan_stats"] = getattr(gp, "_last_scan_debug", {})
                for full_path, ticket_keys in path_to_keys.items():
                    mapped = path_to_art_key.get(full_path)
                    if mapped and ticket_keys:
                        git_hits[mapped] = ticket_keys
            else:
                git_debug["git_paths_matched"] = 0

            git_debug["git_hits_count"] = len(git_hits)
            git_debug["error"] = None

        except Exception as e:
            git_debug["error"] = str(e)
            logger.warning("Git JIRA scan failed — proceeding with keyword-only match", exc_info=True)
    else:
        git_debug["skipped"] = "source_type is not git"

    matched, sources = await anyio.to_thread.run_sync(
        lambda: client.match_issues_to_artifacts(project_key, list(keys), git_hits=git_hits or None)
    )
    save_json(jira_tickets_path(project_id), matched)
    save_json(jira_sources_path(project_id), sources)
    matched_count = sum(1 for v in matched.values() if v and v != "Not Found")
    return {
        "matched": matched,
        "sources": sources,
        "count": matched_count,
        "total": len(matched),
        "git_enriched": len(git_hits),
        "git_debug": git_debug,
    }


@router.get("/match")
def match_cached(project_id: str) -> dict:
    _project_or_404(project_id)
    cached = load_json(jira_tickets_path(project_id), {})
    if not isinstance(cached, dict):
        cached = {}
    sources = load_json(jira_sources_path(project_id), {})
    if not isinstance(sources, dict):
        sources = {}
    return {
        "matched": cached,
        "sources": sources,
        "count": sum(1 for v in cached.values() if v and v != "Not Found"),
        "total": len(cached),
    }


@router.post("/finalize")
async def finalize(project_id: str) -> dict:
    project = _project_or_404(project_id)
    tracker = _tracker_for(project_id, project)
    if not tracker.enabled:
        raise HTTPException(400, "JIRA is disabled — enable it in config.yaml")
    ok = await anyio.to_thread.run_sync(lambda: tracker.finalize(None))
    return {"finalized": ok, "epic_key": tracker.epic_key}
