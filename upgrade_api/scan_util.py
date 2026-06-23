"""Source resolution + artifact scanning helpers — the deterministic
scan/compare pipeline run by the FastAPI service."""

from __future__ import annotations

import json
import os
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from upgrade_lib.sources.artifactory_provider import ArtifactoryProvider
from upgrade_lib.sources.git_provider import GitProvider


SYSTEM_BUCKET_NAME = "SYSTEM"
ARTIFACT_INCLUDE_EXTS = {".json", ".xml", ".java", ".js", ".jsp"}
ARTIFACT_EXCLUDE_NAMES = {"component.info", "security.txt"}


def _looks_like_artifact_tree(root: Path) -> bool:
    if not root.is_dir():
        return False
    for _dp, _dn, files in os.walk(root):
        for f in files:
            if (
                Path(f).suffix.lower() in ARTIFACT_INCLUDE_EXTS
                and f not in ARTIFACT_EXCLUDE_NAMES
            ):
                return True
    return False


def detect_buckets(source_root: Path, fallback_bucket: str) -> list[tuple[str, Path]]:
    if not source_root.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(source_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if _looks_like_artifact_tree(child):
            found.append((child.name, child))
    if found:
        return found
    return [(fallback_bucket, source_root)]


def scan_artifacts(source_root: Path, project_id: str = "SOURCE") -> list[dict]:
    artifacts: list[dict] = []
    if not source_root.exists():
        return artifacts
    scan_roots = detect_buckets(source_root, fallback_bucket=project_id)
    seen_keys: set[str] = set()
    for bucket, bucket_root in scan_roots:
        for dirpath, _dirnames, filenames in os.walk(bucket_root):
            has_artifact_file = any(
                Path(f).suffix.lower() in ARTIFACT_INCLUDE_EXTS
                and f not in ARTIFACT_EXCLUDE_NAMES
                for f in filenames
            )
            if not has_artifact_file:
                continue
            rel = Path(dirpath).relative_to(bucket_root)
            parts = rel.parts
            if len(parts) < 2:
                continue
            rel_path = rel.as_posix()
            key = f"{bucket}::{rel_path}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            artifacts.append(
                {
                    "bucket": bucket,
                    "category": parts[0],
                    "subcategory": parts[1] if len(parts) > 2 else "",
                    "name": parts[-1],
                    "rel_path": rel_path,
                    "source_rel": f"{bucket}/{rel_path}",
                    "abs_path": dirpath,
                }
            )
    artifacts.sort(key=lambda a: (a["bucket"], a["rel_path"]))
    return artifacts


# In-memory TTL cache for resolved paths. Avoids re-running fetch/pull for
# every endpoint call. Invalidated automatically when the project config
# changes (the key embeds a hash of the relevant fields).
_RESOLVE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_RESOLVE_TTL_SECONDS = 300  # 5 minutes

_CACHE_KEYS = (
    "source_type",
    "git_url",
    "git_branch",
    "source_subpath",
    "source_root",
    "target_type",
    "artifactory_url",
    "target_version",
    "target_system",
    "baseline_type",
    "baseline_url",
    "baseline_version",
    "baseline_system",
    "merge_output_dir",
    # DB (seed-data) source
    "db_enabled",
    "db_source_type",
    "db_git_url",
    "db_git_branch",
    "db_source_subpath",
    "db_source_root",
    "db_output_dir",
)


def _project_cache_key(project: dict[str, Any]) -> str:
    payload = {k: project.get(k, "") for k in _CACHE_KEYS}
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def invalidate_resolve_cache(project: dict[str, Any] | None = None) -> None:
    """Drop cached resolutions. Pass a project to invalidate just that one."""
    with _RESOLVE_CACHE_LOCK:
        if project is None:
            _RESOLVE_CACHE.clear()
        else:
            _RESOLVE_CACHE.pop(_project_cache_key(project), None)


def resolve_db_source_root(
    project: dict[str, Any],
    *,
    skip_pull: bool = True,
) -> dict[str, Any]:
    """Resolve ONLY the DB seed-data source root (clones the *_db repo).

    Independent of the app source/target/baseline so the DB tab never triggers
    an Artifactory download. Returns {db_source_root, [error]}.
    """
    out: dict[str, Any] = {"db_source_root": ""}
    if not project.get("db_enabled"):
        return out
    db_type = project.get("db_source_type") or "git"
    try:
        if db_type == "git" and project.get("db_git_url"):
            from upgrade_lib.db.paths import (
                db_subpath_default,
                project_token_from_db_url,
            )

            subpath = project.get("db_source_subpath") or db_subpath_default(
                project_token_from_db_url(project["db_git_url"])
            )
            gp = GitProvider()
            db_res = gp.clone_subpath(
                project["db_git_url"],
                branch=project.get("db_git_branch") or "main",
                subpath=subpath,
                skip_pull=skip_pull,
            )
            out["db_source_root"] = db_res["source_root"]
        elif db_type == "local":
            out["db_source_root"] = project.get("db_source_root", "")
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def resolve_project_paths(
    project: dict[str, Any],
    *,
    use_cache: bool = True,
    skip_pull: bool = False,
    progress_cb: Callable[[str, dict], None] | None = None,
) -> dict[str, Any]:
    """Resolve a project config into local paths via the appropriate provider.
    Returns dict with 'source_root', 'target_system', 'baseline_system'.

    Results are cached in-memory for ~5 minutes per unique project config —
    pass `use_cache=False` to force a fresh fetch/pull.

    `skip_pull=True` tells the GitProvider to skip `git fetch`/`pull` when the
    repo is already cloned — used by endpoints (like merge) that operate on
    the state a previous scan already validated.
    """
    cache_key = _project_cache_key(project)
    now = time.monotonic()
    if use_cache:
        with _RESOLVE_CACHE_LOCK:
            hit = _RESOLVE_CACHE.get(cache_key)
            if hit and (now - hit[0]) < _RESOLVE_TTL_SECONDS:
                return dict(hit[1])  # defensive copy

    source_type = project.get("source_type", "local")
    target_type = project.get("target_type", "local")

    resolved: dict[str, Any] = {}

    if source_type == "git":
        gp = GitProvider()
        git_result = gp.resolve(project, skip_pull=skip_pull)
        resolved["source_root"] = str(git_result.source_root)
        resolved["_git_metadata"] = git_result.metadata
    else:
        resolved["source_root"] = project.get("source_root", "")

    if target_type == "artifactory":
        ap = ArtifactoryProvider()
        art_result = ap.resolve(
            {**project, "source_root": resolved["source_root"]},
            progress_cb=progress_cb,
        )
        resolved["target_system"] = str(art_result.target_system)
        resolved["baseline_system"] = (
            str(art_result.baseline_system) if art_result.baseline_system else ""
        )
        resolved["_art_metadata"] = art_result.metadata
    else:
        resolved["target_system"] = project.get("target_system", "")
        resolved["baseline_system"] = ""

    baseline_type = project.get("baseline_type") or (
        "artifactory"
        if project.get("baseline_url")
        else ("local" if project.get("baseline_system") else "none")
    )
    if not resolved.get("baseline_system"):
        if baseline_type == "artifactory" and project.get("baseline_url"):
            ap = ArtifactoryProvider()
            baseline_cfg = {
                "artifactory_url": project["baseline_url"],
                "target_version": project.get("baseline_version", ""),
            }
            try:
                bl_result = ap.resolve(baseline_cfg, progress_cb=progress_cb)
                resolved["baseline_system"] = str(bl_result.target_system)
            except Exception:
                resolved["baseline_system"] = ""
        elif baseline_type == "local":
            resolved["baseline_system"] = project.get("baseline_system", "")

    resolved["merge_output_dir"] = project.get(
        "merge_output_dir", ""
    )

    # ── DB (seed-data) source — optional, additive ──────────────────────────
    # Resolved independently of the app source/target/baseline. Never raises:
    # DB failures must not break the app scan/merge flow.
    db_out = resolve_db_source_root(project, skip_pull=skip_pull)
    resolved["db_source_root"] = db_out.get("db_source_root", "")
    if db_out.get("error"):
        resolved["_db_error"] = db_out["error"]

    with _RESOLVE_CACHE_LOCK:
        _RESOLVE_CACHE[cache_key] = (now, dict(resolved))
    return resolved
