"""WebLogic merge / output stage.

Docker-to-Docker only writes *merged* artifacts to the output (Retain artifacts
stay in place in the customer's already-Docker-format repo). WebLogic-to-Docker
must instead assemble a complete Docker-format delivery tree from a differently
laid-out WebLogic source, so every kept artifact is written to the output:

    decision Remove              -> skipped (dropped from the delivery)
    decision Retain / copy-as-is -> customer source copied verbatim
    decision Merge               -> 3-way LLM merge (delegated to perform_merge)

All artifacts land under ``<merge_output_dir>/<OUTPUT_APP_ROOT_PREFIX>/<output_rel>``
(e.g. ``.../app_root/repos/DOOSAN/datasets/REPORT/<name>``).

Lives in upgrade_api (not upgrade_lib) because it orchestrates the API-level
merge pipeline (perform_merge); upgrade_lib must not depend on upgrade_api.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from upgrade_api.merge_util import perform_merge
from upgrade_lib.weblogic.layout import OUTPUT_APP_ROOT_PREFIX

_log = logging.getLogger(__name__)


def output_app_root(out_root: Path) -> Path:
    """The Docker ``app_root`` base inside the project's merge output dir."""
    return Path(out_root) / OUTPUT_APP_ROOT_PREFIX


def _copy_artifact(
    key: str,
    entry: dict,
    out_app_root: Path,
    emit: Callable[[str, dict], None],
) -> dict[str, Any]:
    """Copy a Retain / copy-as-is artifact verbatim into the delivery tree."""
    src = Path(entry.get("source_abs", ""))
    out_rel = entry.get("output_rel") or (
        f"{entry.get('bucket', '')}/{entry.get('rel_path', '')}"
    )
    out_dir = out_app_root / out_rel

    emit("copy_start", key=key)
    copied: list[str] = []
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            copied.append(rel.as_posix())
    elif src.is_file():
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out_dir / src.name)
        copied.append(src.name)

    emit("copied", key=key, files=len(copied), out_dir=str(out_dir))

    decision = entry.get("forced_decision") or entry.get("decision") or "Retain"
    return {
        "bucket": entry.get("bucket", ""),
        "rel_path": entry.get("rel_path", key),
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "files": copied,
        "explanation": (
            f"Copied as-is ({decision}) — WebLogic source carried into the "
            "Docker delivery."
        ),
        "base_redirect": None,
        "db_warning": None,
        "diff_generated": False,
        "diff_carried_forward": False,
        "diff_error": None,
        "out_dir": str(out_dir),
        "quality_result": None,
        "dropped_additions": [],
        "deterministic_files": [],
        "copied_as_is": True,
        "decision": decision,
    }


def perform_weblogic_merge(
    key: str,
    entry: dict,
    source_root: Path,
    target_root: Path,
    baseline_root: Path,
    out_root: Path,
    client: Any,
    quality_gate: Any | None,
    default_bucket: str,
    skip_diff: bool = True,
    progress_cb: Callable[[str, dict], None] | None = None,
) -> dict[str, Any]:
    """Produce the delivery entry for one WebLogic artifact.

    ``out_root`` is the project's merge_output_dir; the Docker ``app_root`` base
    is derived from it. Signature mirrors ``perform_merge`` so the merge router
    can dispatch on upgrade mode with no other changes.
    """
    def emit(phase: str, **extra: Any) -> None:
        if progress_cb is not None:
            try:
                progress_cb(phase, extra)
            except Exception:
                pass

    out_app_root = output_app_root(out_root)
    decision = entry.get("decision")
    forced = entry.get("forced_decision")

    # Remove → dropped from the delivery entirely.
    if decision == "Remove" or forced == "Remove":
        emit("removed", key=key)
        return {
            "bucket": entry.get("bucket", ""),
            "rel_path": entry.get("rel_path", key),
            "merged_at": datetime.now(timezone.utc).isoformat(),
            "files": [],
            "explanation": "Removed — dropped from the Docker delivery (WebLogic rule).",
            "out_dir": "",
            "quality_result": None,
            "diff_generated": False,
            "decision": "Remove",
            "removed": True,
        }

    # Retain / copy-as-is → verbatim copy into the delivery tree.
    if forced == "Retain" or entry.get("copy_as_is") or decision == "Retain":
        return _copy_artifact(key, entry, out_app_root, emit)

    # Merge → shared 3-way merge, writing to the app_root layout via output_rel.
    return perform_merge(
        key,
        entry,
        source_root,
        target_root,
        baseline_root,
        out_app_root,
        client,
        quality_gate,
        default_bucket,
        skip_diff,
        progress_cb,
    )
