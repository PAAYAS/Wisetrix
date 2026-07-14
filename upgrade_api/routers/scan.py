"""Scan & Compare endpoints.

GET  /projects/{id}/artifacts        -> list source artifacts (deterministic scan)
GET  /projects/{id}/comparison       -> load cached comparison results
POST /projects/{id}/compare/stream   -> run compare with SSE progress events
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import anyio
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

# Mirror the cache root constants from the provider modules so we can do
# pre-checks (is the git repo already cloned? is the version already extracted?)
# without importing provider internals.
_GIT_CACHE_ROOT = Path.home() / ".wisetrix" / "git_cache"
_ART_CACHE_ROOT = Path.home() / ".wisetrix" / "artifactory_cache"


def _git_clone_exists(git_url: str) -> bool:
    """True if a shallow clone for this URL already lives in the git cache."""
    h = hashlib.sha256(git_url.encode()).hexdigest()[:12]
    d = _GIT_CACHE_ROOT / h
    return d.exists() and (d / ".git").exists()


def _art_version_cached(version: str) -> bool:
    """True if the Artifactory JAR for this version was already extracted."""
    if not version:
        return False
    cache_dir = _ART_CACHE_ROOT / version
    if not cache_dir.exists():
        return False
    # Fast path: sidecar file written by ArtifactoryProvider after first extract
    sidecar = cache_dir / ".wisetrix_system_path"
    if sidecar.exists():
        try:
            p = Path(sidecar.read_text(encoding="utf-8").strip())
            return p.is_dir()
        except Exception:
            pass
    # Slow path: look for any SYSTEM directory
    for p in cache_dir.rglob("SYSTEM"):
        if p.is_dir():
            return True
    return False

from upgrade_api.paths import (
    comparison_path,
    load_json,
    resolved_paths_path,
    risk_path,
    save_json,
)
from upgrade_api.scan_util import resolve_project_paths, scan_source
from upgrade_api.state import load_projects
from upgrade_lib.compare import apply_business_rules, compare_artifact_local
from upgrade_lib.quality.risk_scorer import RiskScorer
from upgrade_lib.weblogic.compare import forced_result


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
            lambda: scan_source(source_root, project, project_id=project_id)
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


async def _artifacts_stream(project_id: str) -> AsyncIterator[dict]:
    """
    SSE stream for the initial artifact load.

    Emits progress events so the UI can show what's happening when the cache is
    cold (first-time git clone + Artifactory download). Each event has:

      phase  { phase: str, note: str }  — current step label + human description
      error  { message: str }           — fatal, stream ends
      done   { ArtifactListResponse }   — success, stream ends
    """

    def _phase(phase: str, note: str = "") -> dict:
        return {"event": "phase", "data": json.dumps({"phase": phase, "note": note})}

    project = _get_project(project_id)
    logger.info("artifacts_stream: project=%s", project_id)

    # ── Fast path: disk-cached resolved paths ────────────────────────────────
    cached = load_json(resolved_paths_path(project_id), None)
    if _resolved_paths_are_usable(cached):
        logger.debug("artifacts_stream: disk cache hit for %s", project_id)
        yield _phase("cached", "Using cached paths — skipping git / Artifactory.")
        resolved = cached
    else:
        # ── Announce what is about to happen ─────────────────────────────────
        source_type = project.get("source_type", "local")
        target_type = project.get("target_type", "local")

        if source_type == "git":
            git_url = project.get("git_url", "")
            if _git_clone_exists(git_url):
                yield _phase("git_fetch", "Fetching latest commits from git…")
            else:
                yield _phase(
                    "git_clone",
                    "Cloning git repository for the first time. "
                    "Using a shallow clone (--depth=1) — usually 30–90 s…",
                )

        if target_type == "artifactory":
            version = project.get("target_version", "")
            if _art_version_cached(version):
                yield _phase(
                    "artifactory_cached",
                    f"Artifactory v{version} already extracted locally.",
                )
            else:
                yield _phase(
                    "artifactory_download",
                    f"Downloading Artifactory JAR for v{version} "
                    "(first time — usually 1–3 min)…",
                )

        # ── Baseline Artifactory (separate JAR, separate cache entry) ─────────
        baseline_type    = project.get("baseline_type", "")
        baseline_url     = project.get("baseline_url", "")
        baseline_version = project.get("baseline_version", "")
        if baseline_type == "artifactory" and baseline_url and baseline_version:
            if _art_version_cached(baseline_version):
                yield _phase(
                    "artifactory_baseline_cached",
                    f"Baseline Artifactory v{baseline_version} already extracted locally.",
                )
            else:
                yield _phase(
                    "artifactory_baseline_download",
                    f"Downloading baseline Artifactory JAR for v{baseline_version} "
                    "(first time — usually 1–3 min)…",
                )

        # ── Run resolve in a worker thread, streaming progress back ──────────
        # The download can take 1-3 min. We use a queue so the Artifactory
        # provider can push download/extract progress events back to this SSE
        # generator while the worker thread is running.
        loop = asyncio.get_running_loop()
        progress_queue: asyncio.Queue = asyncio.Queue()
        _RESOLVE_DONE = object()

        def _progress_cb(phase: str, data: dict) -> None:
            loop.call_soon_threadsafe(
                progress_queue.put_nowait, {"phase": phase, **data}
            )

        async def _run_resolve() -> dict:
            try:
                return await anyio.to_thread.run_sync(
                    lambda: resolve_project_paths(
                        project, skip_pull=True, progress_cb=_progress_cb
                    )
                )
            finally:
                loop.call_soon_threadsafe(
                    progress_queue.put_nowait, _RESOLVE_DONE
                )

        resolve_task = asyncio.create_task(_run_resolve())

        # Drain progress events until the resolve task signals done
        while True:
            item = await progress_queue.get()
            if item is _RESOLVE_DONE:
                break
            yield {"event": "download_progress", "data": json.dumps(item)}

        try:
            resolved = await resolve_task
        except Exception as e:
            logger.error(
                "artifacts_stream: resolve failed for %s: %s",
                project_id, e, exc_info=True,
            )
            yield {"event": "error", "data": json.dumps({"message": str(e)})}
            return

        save_json(
            resolved_paths_path(project_id),
            {k: v for k, v in resolved.items() if not k.startswith("_")},
        )

    # ── Scan artifact tree ────────────────────────────────────────────────────
    source_root = Path(resolved.get("source_root", ""))
    if not resolved.get("source_root") or not source_root.exists():
        msg = f"Source root not found or not configured: {resolved.get('source_root')!r}"
        logger.error("artifacts_stream: %s for %s", msg, project_id)
        yield {"event": "error", "data": json.dumps({"message": msg})}
        return

    yield _phase("scan", "Scanning artifact tree…")
    try:
        artifacts = await anyio.to_thread.run_sync(
            lambda: scan_source(source_root, project, project_id=project_id)
        )
    except Exception as e:
        logger.error(
            "artifacts_stream: scan failed for %s: %s", project_id, e, exc_info=True
        )
        yield {"event": "error", "data": json.dumps({"message": f"Scan failed: {e}"})}
        return

    buckets = sorted({a["bucket"] for a in artifacts})
    logger.info(
        "artifacts_stream: done — %d artifacts, %d buckets for %s",
        len(artifacts), len(buckets), project_id,
    )

    yield {
        "event": "done",
        "data": json.dumps({
            "project_id": project_id,
            "source_root": resolved["source_root"],
            "target_system": resolved.get("target_system", ""),
            "baseline_system": resolved.get("baseline_system", ""),
            "git_metadata": resolved.get("_git_metadata"),
            "buckets": buckets,
            "artifacts": artifacts,
            "count": len(artifacts),
        }),
    }


@router.get("/artifacts/stream")
async def get_artifacts_stream(project_id: str):
    """
    SSE stream for initial artifact loading.

    Emits phase-progress events during git clone / Artifactory download so the
    UI can show what's happening instead of an unexplained spinner.
    Use EventSource on the client (bypasses the Next.js dev-proxy timeout).
    """
    return EventSourceResponse(_artifacts_stream(project_id))


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
    baseline_root = (
        Path(resolved["baseline_system"]) if resolved.get("baseline_system") else None
    )

    if not resolved["source_root"] or not source_root.exists():
        yield {
            "event": "error",
            "data": json.dumps(
                {"message": f"Source root not found: {resolved['source_root']!r}"}
            ),
        }
        return

    yield {"event": "phase", "data": json.dumps({"phase": "scan"})}
    artifacts = scan_source(source_root, project, project_id=project_id)
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

        # ── Source directory ─────────────────────────────────────────────────
        # WebLogic descriptors carry an explicit source_abs (the real customer
        # dir, whose on-disk path differs from bucket/rel after $->/ + redirects).
        # Docker descriptors omit it → fall back to the standard layout.
        if art.get("source_abs"):
            customer_dir = Path(art["source_abs"])
        else:
            customer_dir = source_root / bucket / rel

        # ── SYSTEM/baseline lookup path ──────────────────────────────────────
        # WebLogic descriptors carry an explicit, already-normalized system_rel.
        # Docker env_specific strips the {ENV}/{CUSTOMER}/ prefix; normal Docker
        # artifacts use rel as-is.
        if art.get("system_rel"):
            system_rel = art["system_rel"]
        elif bucket.startswith("__env_specific"):
            parts = Path(rel).parts
            system_rel = "/".join(parts[2:]) if len(parts) >= 3 else rel
        else:
            system_rel = rel

        sys_dir = target_root / system_rel

        meta = {
            "bucket": bucket,
            "category": art["category"],
            "name": art["name"],
            "rel_path": rel,
            "system_rel_path": system_rel,   # stripped path for SYSTEM/baseline lookup
            "source_rel": key,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        # Carry WebLogic bridge fields through to the merge stage (absent for
        # Docker artifacts, so Docker merge behaviour is unchanged).
        for _f in ("source_abs", "output_rel", "copy_as_is", "forced_decision"):
            if art.get(_f) is not None:
                meta[_f] = art[_f]

        try:
            forced = art.get("forced_decision")
            if forced:
                # WebLogic routing fixes this artifact's decision (rules 4/5/6):
                # copy-as-is Retain or Remove — no 3-way compare.
                result = forced_result(
                    forced, customer_dir, copy_as_is=bool(art.get("copy_as_is"))
                )
            else:
                # Pass system_rel so BASE redirect and category detection work on
                # the real artifact path, not the env_specific / WebLogic prefix.
                result = compare_artifact_local(
                    customer_dir, sys_dir, system_rel,
                    target_root=target_root, baseline_root=baseline_root,
                )
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

    # ── Rule 12 (WebLogic): WEB-INF/lib source-JAR reconciliation ────────────
    # Per-.java-file decision (commit → linked TA/PDSUPPORT → fixed-in-window →
    # Remove). Lives outside plugins/IMPLEMENTATION, so it needs the git clone
    # root. Streams a "Comparing <jar>" progress event per jar. Best-effort.
    if (project.get("upgrade_mode") or "docker") == "weblogic":
        repo_root = (resolved.get("_git_metadata") or {}).get("clone_dir")
        if repo_root:
            loop = asyncio.get_running_loop()
            jar_queue: asyncio.Queue = asyncio.Queue()
            _JAR_DONE = object()

            def _jar_cb(phase: str, data: dict) -> None:
                loop.call_soon_threadsafe(
                    jar_queue.put_nowait, {"phase": phase, **data}
                )

            async def _run_jars() -> dict:
                try:
                    from upgrade_api.weblogic_jars import reconcile_web_inf_lib

                    return await anyio.to_thread.run_sync(
                        lambda: reconcile_web_inf_lib(repo_root, project, _jar_cb)
                    )
                finally:
                    loop.call_soon_threadsafe(jar_queue.put_nowait, _JAR_DONE)

            jar_task = asyncio.create_task(_run_jars())
            while True:
                item = await jar_queue.get()
                if item is _JAR_DONE:
                    break
                yield {"event": "phase", "data": json.dumps(item)}
            try:
                jar_results = await jar_task
                comp_results.update(jar_results)
                logger.info(
                    "compare_stream: rule12 added %d WEB-INF/lib jar(s) for %s",
                    len(jar_results), project_id,
                )
            except Exception as e:
                logger.error(
                    "compare_stream: rule12 failed for %s: %s",
                    project_id, e, exc_info=True,
                )

    yield {"event": "phase", "data": json.dumps({"phase": "rollup"})}
    comp_results = apply_business_rules(comp_results, target_root=target_root)

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
