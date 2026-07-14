"""Merge queue endpoints.

GET  /projects/{id}/merges                       -> done merges
POST /projects/{id}/merges/{key}                 -> run a single artifact merge (Claude)
POST /projects/{id}/merges/stream                -> SSE Merge All
GET  /projects/{id}/merges/{key}/download        -> ZIP a single merged artifact
GET  /projects/{id}/merges/download              -> ZIP all merged output
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sse_starlette.sse import EventSourceResponse

from upgrade_api.merge_util import (
    perform_merge,
    primary_customer_bucket,
    zip_directory,
)
from upgrade_api.paths import (
    comparison_path,
    load_json,
    merge_report_path,
    resolved_paths_path,
    risk_path,
    save_json,
)
from upgrade_api.scan_util import resolve_project_paths
from upgrade_api.state import load_projects
from upgrade_lib.claude_client import UpgradeClient
from upgrade_lib.quality.quality_gate import QualityGate
from upgrade_api.weblogic_merge import perform_weblogic_merge


router = APIRouter(prefix="/projects/{project_id}/merges", tags=["merges"])


def _is_weblogic(project: dict) -> bool:
    return (project.get("upgrade_mode") or "docker") == "weblogic"


def _pending_decisions(project: dict) -> set[str]:
    """Which decisions produce delivery output for this mode.

    Docker: only Merge (Retain artifacts stay in the customer's Docker repo).
    WebLogic: Merge + Retain — a Docker delivery is assembled from a differently
    laid-out source, so Retain artifacts must be copied too. Blank-decision
    rule-12 files (report to Core) are intentionally excluded — they're left for
    Core to resolve, not auto-included.
    """
    return {"Merge", "Retain"} if _is_weblogic(project) else {"Merge"}


def _merge_fn(project: dict):
    """The per-artifact merge function for this project's upgrade mode."""
    return perform_weblogic_merge if _is_weblogic(project) else perform_merge


def _project_or_404(project_id: str) -> dict:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return projects[project_id]


def _resolve_or_400(project: dict) -> dict:
    try:
        return resolve_project_paths(project)
    except Exception as e:
        raise HTTPException(400, f"Failed to resolve paths: {e}")


def _resolved_paths_are_usable(resolved: dict | None) -> bool:
    """True if a persisted resolved-paths blob still points at real dirs.

    Both source_root and target_system must exist on disk. Baseline is
    optional, but if a path is recorded it must still be valid.
    """
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


def _zip_filename_header(name: str) -> dict:
    """Build a Content-Disposition header that handles spaces/non-ASCII."""
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


@router.get("")
def list_merges(project_id: str) -> dict:
    project = _project_or_404(project_id)
    merges = load_json(merge_report_path(project_id), {})
    comp = load_json(comparison_path(project_id), {})
    wanted = _pending_decisions(project)
    pending = {
        k: v
        for k, v in comp.items()
        if v.get("decision") in wanted and k not in merges
    }
    return {
        "pending": pending,
        "done": merges,
        "pending_count": len(pending),
        "done_count": len(merges),
    }


@router.post("/{key:path}")
async def merge_one(project_id: str, key: str) -> dict:
    project = _project_or_404(project_id)
    comp = load_json(comparison_path(project_id), {})
    if key not in comp:
        raise HTTPException(404, f"Artifact '{key}' not found in comparison results")
    entry = comp[key]

    resolved = _resolve_or_400(project)
    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])
    baseline_root = Path(resolved.get("baseline_system") or "")
    out_root = Path(
        resolved.get("merge_output_dir") or project.get("merge_output_dir", "")
    )
    default_bucket = primary_customer_bucket(source_root, project_id)

    client = UpgradeClient()
    qg = QualityGate()
    merge_fn = _merge_fn(project)

    # Claude work is blocking — run in thread so we don't stall the event loop.
    record = await anyio.to_thread.run_sync(
        lambda: merge_fn(
            key,
            entry,
            source_root,
            target_root,
            baseline_root,
            out_root,
            client,
            qg,
            default_bucket,
            False,  # skip_diff=False: regenerate _diff.json so downloads are complete
        )
    )

    merges = load_json(merge_report_path(project_id), {})
    merges[key] = record
    save_json(merge_report_path(project_id), merges)
    return record


async def _merge_stream(
    project_id: str,
    only_keys: list[str] | None = None,
    skip_diff: bool = True,
) -> AsyncIterator[dict]:
    """Stream merge progress.

    If `only_keys` is set, merge just those keys (re-runs even if already merged).
    Otherwise merge every pending artifact from the comparison.
    """
    project = _project_or_404(project_id)
    yield {"event": "phase", "data": json.dumps({"phase": "resolve"})}

    # Fast path: the most recent scan persisted resolved paths to
    # run_state/{id}.resolved.json. Reading that avoids touching git or
    # rglob-ing the extracted artifactory JAR — typically the slow steps.
    cache_file = resolved_paths_path(project_id)
    resolved = load_json(cache_file, None)
    cache_diagnostic: dict[str, Any] = {
        "cache_file": str(cache_file),
        "exists": cache_file.exists(),
    }
    if resolved:
        cache_diagnostic["source_root_ok"] = (
            bool(resolved.get("source_root"))
            and Path(resolved["source_root"]).exists()
        )
        cache_diagnostic["target_system_ok"] = (
            bool(resolved.get("target_system"))
            and Path(resolved["target_system"]).exists()
        )
        baseline = resolved.get("baseline_system")
        cache_diagnostic["baseline_ok"] = (
            not baseline or Path(baseline).exists()
        )

    if _resolved_paths_are_usable(resolved):
        yield {
            "event": "phase",
            "data": json.dumps({"phase": "resolve_cache_hit", **cache_diagnostic}),
        }
    else:
        yield {
            "event": "phase",
            "data": json.dumps(
                {"phase": "resolve_cache_miss", **cache_diagnostic}
            ),
        }
        # Fall back to a real resolve (first merge, or stale/missing cache).
        try:
            resolved = await anyio.to_thread.run_sync(
                lambda: resolve_project_paths(project, skip_pull=True)
            )
        except Exception as e:
            yield {
                "event": "error",
                "data": json.dumps({"message": f"Failed to resolve paths: {e}"}),
            }
            return
        save_json(
            cache_file,
            {k: v for k, v in resolved.items() if not k.startswith("_")},
        )

    yield {"event": "phase", "data": json.dumps({"phase": "resolve_done"})}

    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])
    baseline_root = Path(resolved.get("baseline_system") or "")
    out_root = Path(
        resolved.get("merge_output_dir") or project.get("merge_output_dir", "")
    )
    default_bucket = primary_customer_bucket(source_root, project_id)

    comp = load_json(comparison_path(project_id), {})
    merges = load_json(merge_report_path(project_id), {})
    risks = load_json(risk_path(project_id), {})

    if only_keys:
        missing = [k for k in only_keys if k not in comp]
        if missing:
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": f"Unknown artifact key(s): {', '.join(missing)}"}
                ),
            }
            return
        pending_items = [(k, comp[k]) for k in only_keys]
    else:
        wanted = _pending_decisions(project)
        pending_items = [
            (k, v)
            for k, v in comp.items()
            if v.get("decision") in wanted and k not in merges
        ]
    total = len(pending_items)
    yield {"event": "scan", "data": json.dumps({"total": total})}

    if total == 0:
        yield {
            "event": "done",
            "data": json.dumps(
                {"total": 0, "succeeded": 0, "failed": [], "merged": []}
            ),
        }
        return

    client = UpgradeClient()
    qg = QualityGate()
    merge_fn = _merge_fn(project)
    succeeded = 0
    failed: list[dict] = []
    merged_keys: list[str] = []

    loop = asyncio.get_running_loop()

    for i, (key, entry) in enumerate(pending_items, 1):
        risk_level = risks.get(key, {}).get("level", "—")
        yield {
            "event": "progress",
            "data": json.dumps(
                {
                    "index": i,
                    "total": total,
                    "key": key,
                    "risk": risk_level,
                    "status": "running",
                }
            ),
        }

        # Per-artifact progress queue. perform_merge runs in a worker thread
        # and uses call_soon_threadsafe to push phase events back here, so the
        # SSE stream stays alive while Claude is working.
        phase_queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        def _cb(phase: str, extra: dict, _k: str = key) -> None:
            payload = {"key": _k, "phase": phase, **extra}
            loop.call_soon_threadsafe(phase_queue.put_nowait, payload)

        async def _run(k: str = key, e: dict = entry) -> dict:
            try:
                rec = await anyio.to_thread.run_sync(
                    lambda: merge_fn(
                        k,
                        e,
                        source_root,
                        target_root,
                        baseline_root,
                        out_root,
                        client,
                        qg,
                        default_bucket,
                        skip_diff,
                        _cb,
                    )
                )
                return {"ok": True, "record": rec}
            except Exception as ex:  # noqa: BLE001
                return {"ok": False, "error": str(ex)}
            finally:
                loop.call_soon_threadsafe(phase_queue.put_nowait, _DONE)

        merge_task = asyncio.create_task(_run())

        # Drain phase events as they arrive; stop when _DONE sentinel comes in.
        while True:
            item = await phase_queue.get()
            if item is _DONE:
                break
            yield {"event": "phase", "data": json.dumps(item)}

        result = await merge_task

        if result["ok"]:
            record = result["record"]
            # Re-read from disk before saving to avoid overwriting results
            # from concurrent single-merge streams that ran in parallel.
            current_merges = load_json(merge_report_path(project_id), {})
            current_merges[key] = record
            save_json(merge_report_path(project_id), current_merges)
            succeeded += 1
            merged_keys.append(key)
            verdict = (record.get("quality_result") or {}).get("verdict", "—")
            yield {
                "event": "merged",
                "data": json.dumps(
                    {"key": key, "verdict": verdict, "out_dir": record["out_dir"]}
                ),
            }
        else:
            failed.append({"key": key, "error": result["error"]})
            yield {
                "event": "failed",
                "data": json.dumps({"key": key, "error": result["error"]}),
            }
        await asyncio.sleep(0)

    yield {
        "event": "done",
        "data": json.dumps(
            {
                "total": total,
                "succeeded": succeeded,
                "failed": failed,
                "merged": merged_keys,
            }
        ),
    }


@router.get("/stream")
async def merge_all_stream_get(project_id: str):
    return EventSourceResponse(_merge_stream(project_id))


@router.post("/stream")
async def merge_all_stream_post(project_id: str):
    return EventSourceResponse(_merge_stream(project_id))


@router.get("/{key:path}/stream")
async def merge_one_stream_get(project_id: str, key: str):
    return EventSourceResponse(
        _merge_stream(project_id, only_keys=[key], skip_diff=False)
    )


@router.post("/{key:path}/stream")
async def merge_one_stream_post(project_id: str, key: str):
    return EventSourceResponse(
        _merge_stream(project_id, only_keys=[key], skip_diff=False)
    )


@router.get("/download")
def download_all(project_id: str) -> Response:
    project = _project_or_404(project_id)
    resolved = _resolve_or_400(project)
    out_root = Path(
        resolved.get("merge_output_dir") or project.get("merge_output_dir", "")
    )
    if not out_root or not out_root.exists():
        raise HTTPException(404, "No merged output yet")
    data = zip_directory(out_root, arc_root=project_id)
    if not data:
        raise HTTPException(404, "Output directory is empty")
    return Response(
        content=data,
        media_type="application/zip",
        headers=_zip_filename_header(f"{project_id}-merged.zip"),
    )


@router.get("/{key:path}/download")
def download_one(project_id: str, key: str) -> Response:
    _project_or_404(project_id)
    merges = load_json(merge_report_path(project_id), {})
    if key not in merges:
        raise HTTPException(404, f"Artifact '{key}' has not been merged yet")
    art_dir = Path(merges[key].get("out_dir", ""))
    if not art_dir.exists():
        raise HTTPException(404, f"Output directory missing on disk: {art_dir}")
    data = zip_directory(art_dir, arc_root=art_dir.name)
    safe_name = key.replace("/", "_").replace("\\", "_")
    return Response(
        content=data,
        media_type="application/zip",
        headers=_zip_filename_header(f"{safe_name}.zip"),
    )
