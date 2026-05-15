"""Scan & Compare endpoints.

GET  /projects/{id}/artifacts        -> list source artifacts (deterministic scan)
GET  /projects/{id}/comparison       -> load cached comparison results
POST /projects/{id}/compare/stream   -> run compare with SSE progress events
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import anyio
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

from upgrade_api.paths import (
    comparison_path,
    load_json,
    resolved_paths_path,
    risk_path,
    save_json,
)
from upgrade_api.scan_util import resolve_project_paths, scan_artifacts
from upgrade_api.state import load_projects
from upgrade_lib.compare import apply_business_rules, compare_artifact_local
from upgrade_lib.quality.risk_scorer import RiskScorer


router = APIRouter(prefix="/projects/{project_id}", tags=["scan"])


def _get_project(project_id: str) -> dict:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return projects[project_id]


def _resolved_paths_are_usable(resolved: dict | None) -> bool:
    """True if a persisted resolved-paths blob still points at real dirs."""
    if not resolved:
        return False
    source_root = resolved.get("source_root")
    target_system = resolved.get("target_system")
    if not source_root or not target_system:
        return False
    if not Path(source_root).exists() or not Path(target_system).exists():
        return False
    baseline = resolved.get("baseline_system")
    if baseline and not Path(baseline).exists():
        return False
    return True


@router.get("/artifacts")
async def list_artifacts(project_id: str) -> dict:
    project = _get_project(project_id)
    logger.info("list_artifacts: project=%s", project_id)

    # Fast path: prior scan persisted resolved paths to disk. Use that to
    # avoid hitting git / artifactory on every page load — that's what was
    # causing the Next.js proxy to time out with ECONNRESET.
    cached = load_json(resolved_paths_path(project_id), None)
    if _resolved_paths_are_usable(cached):
        logger.debug("list_artifacts: using cached resolved paths for %s", project_id)
        resolved = cached
    else:
        logger.info("list_artifacts: resolving paths for %s (cache miss)", project_id)
        try:
            # Offload resolve to a thread so the request stays responsive
            # and so we don't block the event loop while git/artifactory work.
            resolved = await anyio.to_thread.run_sync(
                lambda: resolve_project_paths(project, skip_pull=True)
            )
        except Exception as e:
            logger.error("list_artifacts: path resolution failed for %s: %s", project_id, e, exc_info=True)
            raise HTTPException(status_code=400, detail=f"Failed to resolve paths: {e}")
        # Persist for next time, stripping internal metadata keys.
        save_json(
            resolved_paths_path(project_id),
            {k: v for k, v in resolved.items() if not k.startswith("_")},
        )

    source_root = Path(resolved["source_root"])
    if not resolved["source_root"] or not source_root.exists():
        logger.error(
            "list_artifacts: source root not found for %s: %r",
            project_id,
            resolved["source_root"],
        )
        raise HTTPException(
            status_code=400,
            detail=f"Source root not found or not configured: {resolved['source_root']!r}",
        )

    try:
        artifacts = await anyio.to_thread.run_sync(
            lambda: scan_artifacts(source_root, project_id=project_id)
        )
    except Exception as e:
        logger.error("list_artifacts: scan failed for %s: %s", project_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Artifact scan failed: {e}")

    buckets = sorted({a["bucket"] for a in artifacts})
    logger.info("list_artifacts: found %d artifacts in %d buckets for %s", len(artifacts), len(buckets), project_id)

    return {
        "project_id": project_id,
        "source_root": resolved["source_root"],
        "target_system": resolved.get("target_system", ""),
        "baseline_system": resolved.get("baseline_system", ""),
        "git_metadata": resolved.get("_git_metadata"),
        "buckets": buckets,
        "artifacts": artifacts,
        "count": len(artifacts),
    }


@router.get("/comparison")
def get_comparison(project_id: str) -> dict:
    _get_project(project_id)
    return load_json(comparison_path(project_id), {})


@router.get("/risk")
def get_risk(project_id: str) -> dict:
    _get_project(project_id)
    return load_json(risk_path(project_id), {})


async def _compare_stream(project_id: str) -> AsyncIterator[dict]:
    """Yield SSE events while running the deterministic compare pipeline."""
    project = _get_project(project_id)
    logger.info("compare_stream: starting for project=%s", project_id)

    yield {"event": "phase", "data": json.dumps({"phase": "resolve"})}
    try:
        # Run resolve in a worker thread so the SSE response stays alive
        # while git fetch / artifactory download happens. Without this the
        # event loop blocks and the browser sees nothing until resolve finishes.
        resolved = await anyio.to_thread.run_sync(
            lambda: resolve_project_paths(project)
        )
    except Exception as e:
        logger.error("compare_stream: resolve failed for %s: %s", project_id, e, exc_info=True)
        yield {"event": "error", "data": json.dumps({"message": str(e)})}
        return

    # Persist resolved paths so subsequent merges can skip resolve entirely.
    # Strip internal metadata (anything starting with `_`) — we only need the
    # actual on-disk paths.
    save_json(
        resolved_paths_path(project_id),
        {k: v for k, v in resolved.items() if not k.startswith("_")},
    )

    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])

    if not resolved["source_root"] or not source_root.exists():
        yield {
            "event": "error",
            "data": json.dumps(
                {"message": f"Source root not found: {resolved['source_root']!r}"}
            ),
        }
        return

    yield {"event": "phase", "data": json.dumps({"phase": "scan"})}
    artifacts = scan_artifacts(source_root, project_id=project_id)
    total = len(artifacts)
    yield {
        "event": "scan",
        "data": json.dumps(
            {
                "total": total,
                "buckets": sorted({a["bucket"] for a in artifacts}),
                "source_root": resolved["source_root"],
                "target_system": resolved.get("target_system", ""),
            }
        ),
    }

    comp_results = load_json(comparison_path(project_id), {})

    yield {"event": "phase", "data": json.dumps({"phase": "compare"})}
    for i, art in enumerate(artifacts, 1):
        key = art["source_rel"]
        rel = art["rel_path"]
        bucket = art["bucket"]
        aldi_dir = source_root / bucket / rel
        sys_dir = target_root / rel

        meta = {
            "bucket": bucket,
            "category": art["category"],
            "name": art["name"],
            "rel_path": rel,
            "source_rel": key,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = compare_artifact_local(aldi_dir, sys_dir, rel)
            comp_results[key] = {**meta, **result}
            decision = result.get("decision", "?")
        except Exception as e:
            logger.error("compare_stream: error comparing %s in %s: %s", key, project_id, e, exc_info=True)
            comp_results[key] = {**meta, "decision": "ERROR", "error": str(e)}
            decision = "ERROR"

        yield {
            "event": "progress",
            "data": json.dumps(
                {
                    "index": i,
                    "total": total,
                    "key": key,
                    "decision": decision,
                }
            ),
        }
        # cooperative yield so the SSE client can render incrementally
        await asyncio.sleep(0)

    yield {"event": "phase", "data": json.dumps({"phase": "rollup"})}
    comp_results = apply_business_rules(comp_results)

    yield {"event": "phase", "data": json.dumps({"phase": "risk"})}
    scorer = RiskScorer()
    risk_results: dict[str, dict] = {}
    for key, result in comp_results.items():
        ra = scorer.assess(result, {"category": result.get("category", "")})
        risk_results[key] = {
            "level": ra.level,
            "score": ra.score,
            "factors": ra.factors,
        }
        comp_results[key]["risk_level"] = ra.level
        comp_results[key]["risk_score"] = ra.score

    save_json(comparison_path(project_id), comp_results)
    save_json(risk_path(project_id), risk_results)

    counts: dict[str, int] = {}
    for r in comp_results.values():
        d = r.get("decision", "?")
        counts[d] = counts.get(d, 0) + 1
    risk_counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for r in risk_results.values():
        lvl = r.get("level", "LOW")
        risk_counts[lvl] = risk_counts.get(lvl, 0) + 1

    yield {
        "event": "done",
        "data": json.dumps(
            {
                "total": total,
                "decision_counts": counts,
                "risk_counts": risk_counts,
            }
        ),
    }


@router.post("/compare/stream")
async def compare_stream(project_id: str):
    """Streams compare progress over Server-Sent Events.

    Use this from the UI via `EventSource` (note: EventSource only supports GET,
    so we also expose the same endpoint as GET below)."""
    return EventSourceResponse(_compare_stream(project_id))


@router.get("/compare/stream")
async def compare_stream_get(project_id: str):
    """GET variant of the SSE compare stream so browsers can use EventSource."""
    return EventSourceResponse(_compare_stream(project_id))
