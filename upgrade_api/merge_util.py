"""Merge execution helpers — ported from upgrade-frontend/app.py so the
FastAPI service can run the same Claude-based merge pipeline."""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)

# Matches BASE_*_NAME tags in JSON files (Rules 3 & 4)
_BASE_TAG_RE = re.compile(r"^BASE_.+_NAME$")

from upgrade_api.scan_util import detect_buckets


TEXT_EXTS = {
    ".json",
    ".xml",
    ".java",
    ".js",
    ".jsp",
    ".groovy",
    ".txt",
    ".properties",
    ".yaml",
    ".yml",
    ".md",
    ".html",
    ".css",
}

SYSTEM_BUCKET_NAME = "SYSTEM"

# Files Claude writes to ./merged/ for documentation purposes.
# These are stored separately under output/_analysis/ and excluded
# from the artifact output that goes to git check-in.
_ANALYSIS_FILENAMES = {
    "MERGE_REPORT.md",
    "MERGE_SUMMARY.txt",
    "VERIFICATION_CHECKLIST.txt",
}


def _split_analysis_files(
    merged_files: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Split merged_files into (artifact_files, analysis_files).

    analysis_files: MERGE_REPORT.md, MERGE_SUMMARY.txt, VERIFICATION_CHECKLIST.txt,
                    and any *_conflicts.txt files Claude generates.
    artifact_files: everything else — the actual merged output for git check-in.
    """
    artifact: dict[str, str] = {}
    analysis: dict[str, str] = {}
    for rel, content in merged_files.items():
        fname = Path(rel).name
        if fname in _ANALYSIS_FILENAMES or fname.endswith("_conflicts.txt"):
            analysis[rel] = content
        else:
            artifact[rel] = content
    return artifact, analysis


def _read_base_artifact_name(files: dict[str, str]) -> str | None:
    """Scan artifact file dict for a BASE_*_NAME tag in any JSON file.

    Returns the base artifact name (e.g. "GPM_INBOUND_V2") or None.

    Used when system_files is empty — the customer artifact extends a SYSTEM
    base artifact named by this tag rather than having its own SYSTEM counterpart.
    """
    for rel, content in files.items():
        if not rel.endswith(".json"):
            continue
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                continue
            for key, value in data.items():
                if (
                    _BASE_TAG_RE.match(key)
                    and isinstance(value, str)
                    and value.strip()
                ):
                    return value.strip()
        except Exception:
            continue
    return None


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTS


def _read_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except Exception:
            return None
    except Exception:
        return None


def read_artifact_files(artifact_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not artifact_dir.exists():
        return result
    for p in artifact_dir.rglob("*"):
        if p.is_file() and _is_text(p):
            content = _read_safe(p)
            if content is not None:
                rel = p.relative_to(artifact_dir).as_posix()
                result[rel] = content
    return result


def write_artifact_files(output_dir: Path, files: dict[str, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def zip_directory(src_dir: Path, arc_root: str | None = None) -> bytes:
    if not src_dir.exists():
        return b""
    root_name = arc_root if arc_root is not None else src_dir.name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in src_dir.rglob("*"):
            if path.is_file():
                rel = path.relative_to(src_dir)
                arcname = (
                    f"{root_name}/{rel.as_posix()}" if root_name else rel.as_posix()
                )
                zf.write(path, arcname=arcname)
    return buf.getvalue()


def primary_customer_bucket(source_root: Path, project_id: str) -> str:
    buckets = detect_buckets(source_root, fallback_bucket=project_id)
    customers = [n for n, _p in buckets if n != SYSTEM_BUCKET_NAME]
    return customers[0] if customers else project_id


def perform_merge(
    key: str,
    entry: dict,
    source_root: Path,
    target_root: Path,
    baseline_root: Path,
    out_root: Path,
    client: Any,
    quality_gate: Any | None,
    default_bucket: str,
    skip_diff: bool = False,
    progress_cb: Callable[[str, dict], None] | None = None,
) -> dict[str, Any]:
    """Run a single artifact merge via UpgradeClient and quality gate.

    `progress_cb` (optional) is called with (phase_name, extra_dict) at each
    pipeline step so SSE consumers can stream progress to the UI. The callback
    must be thread-safe — it is invoked from whatever thread runs this fn.
    """
    def emit(phase: str, **extra: Any) -> None:
        if progress_cb is not None:
            try:
                progress_cb(phase, extra)
            except Exception:
                pass

    rel = entry.get("rel_path", key)
    bucket = entry.get("bucket") or default_bucket

    emit("reading", key=key)
    customer_files = read_artifact_files(source_root / bucket / rel)
    system_files = read_artifact_files(target_root / rel)

    # ── Rules 3 & 4: BASE_*_NAME redirect ────────────────────────────────────
    # If the SYSTEM target has no counterpart for this artifact, check whether
    # the customer's JSON contains a BASE_*_NAME tag that names the real SYSTEM
    # base artifact to merge against.
    # e.g. PTX_GPM_INBOUND_V2 / BASE_INTEGRATION_DEF_NAME = GPM_INBOUND_V2
    #   → read system files from  target_root/integration_def/GPM_INBOUND_V2
    base_redirect: str | None = None
    if not system_files and customer_files:
        base_name = _read_base_artifact_name(customer_files)
        if base_name:
            rel_parts = Path(rel).parts
            category  = rel_parts[0] if rel_parts else ""
            if category:
                alt_system_dir = target_root / category / base_name
                if alt_system_dir.exists():
                    system_files  = read_artifact_files(alt_system_dir)
                    base_redirect = f"{category}/{base_name}"
                    _log.info(
                        "[merge] BASE redirect: %s → %s (key=%s)",
                        rel, base_redirect, key,
                    )

    baseline_files: dict[str, str] = {}
    if baseline_root and str(baseline_root) and baseline_root.exists():
        baseline_files = read_artifact_files(baseline_root / rel)
        # If the baseline also has no counterpart, try the same BASE redirect
        if not baseline_files and base_redirect:
            baseline_files = read_artifact_files(baseline_root / base_redirect)

    emit(
        "claude_merge_start",
        key=key,
        files=len(customer_files),
        has_baseline=bool(baseline_files),
    )
    merge_res = client.merge_artifact(
        customer_files, system_files, baseline_files, customer=bucket
    )
    artifact_files, analysis_files = _split_analysis_files(merge_res["merged_files"])
    emit("claude_merge_done", key=key, files=len(artifact_files))

    out_dir = out_root / bucket / rel
    write_artifact_files(out_dir, artifact_files)

    # Store Claude's analysis docs separately — not part of the git check-in artifact.
    if analysis_files:
        analysis_dir = out_root / "_analysis" / bucket / rel
        write_artifact_files(analysis_dir, analysis_files)

    emit("written", key=key, out_dir=str(out_dir))

    quality_result: dict | None = None
    if quality_gate is not None:
        emit("quality_start", key=key)
        qr = quality_gate.check(artifact_files)
        quality_result = qr.to_dict()
        emit("quality_done", key=key, verdict=qr.verdict)

    diff_info = None
    diff_error: str | None = None
    customer_has_diff = any(
        name.endswith("_diff.json") for name in customer_files
    )
    if not skip_diff and not customer_has_diff:
        emit("diff_skipped", key=key, reason="no _diff.json in customer artifact")
    if not skip_diff and customer_has_diff:
        for fname, content in artifact_files.items():
            if fname.endswith(".json") and not fname.endswith("_diff.json"):
                sys_content = system_files.get(fname)
                if sys_content:
                    try:
                        emit("diff_start", key=key, file=fname)
                        artifact_id = Path(rel).name
                        diff_info = client.generate_diff_json(
                            content,
                            sys_content,
                            artifact_id,
                            merged_name=fname,
                            system_name=fname,
                        )
                        diff_name = fname.replace(".json", "_diff.json")
                        (out_dir / diff_name).write_text(
                            diff_info["diff_json"], encoding="utf-8"
                        )
                        emit("diff_done", key=key, file=fname)
                    except Exception as e:
                        diff_error = str(e)
                        emit("diff_failed", key=key, file=fname, error=str(e))
                break

    explanation = merge_res.get("explanation", "")

    # ── Rule 6: business_process_policies DB warning ──────────────────────────
    # Any change here requires manually deleting the previous DB entry before
    # the upgrade is applied. Append this to the explanation so it's visible
    # in the UI merge result.
    rel_parts = Path(rel).parts
    db_warning: str | None = None
    if rel_parts and rel_parts[0] == "business_process_policies":
        db_warning = (
            "\n\n⚠ DB ACTION REQUIRED: business_process_policies artifacts "
            "require manual DB handling. Delete the previous entry in the "
            "database BEFORE applying the upgrade to this environment."
        )
        explanation = explanation + db_warning

    return {
        "bucket": bucket,
        "rel_path": rel,
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "files": list(artifact_files.keys()),
        "explanation": explanation,
        "base_redirect": base_redirect,
        "db_warning": db_warning,
        "diff_generated": diff_info is not None,
        "diff_error": diff_error,
        "out_dir": str(out_dir),
        "quality_result": quality_result,
    }
