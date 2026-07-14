"""Merge execution helpers — the Claude-based merge pipeline run by the
FastAPI service."""

from __future__ import annotations

import io
import json
import logging
import re
import time
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

    # For __env_specific artifacts the rel_path contains {ENV}/{CUSTOMER}/ prefix
    # which doesn't exist in SYSTEM.  scan.py stores the stripped path so we can
    # look up the real SYSTEM counterpart (e.g. datasets/REPORT/MY_REPORT).
    system_rel = entry.get("system_rel_path") or rel

    merge_start = time.monotonic()
    emit("reading", key=key)
    # WebLogic entries carry an explicit source_abs (the real customer dir,
    # whose path differs from bucket/rel after $->/ + redirects). Docker entries
    # omit it → fall back to the standard layout.
    customer_dir = (
        Path(entry["source_abs"]) if entry.get("source_abs")
        else source_root / bucket / rel
    )
    customer_files = read_artifact_files(customer_dir)
    system_files = read_artifact_files(target_root / system_rel)

    # ── Rules 3 & 4: BASE_*_NAME redirect ────────────────────────────────────
    # If the SYSTEM target has no counterpart for this artifact, check whether
    # the customer's JSON contains a BASE_*_NAME tag that names the real SYSTEM
    # base artifact to merge against.
    # e.g. PTX_GPM_INBOUND_V2 / BASE_INTEGRATION_DEF_NAME = GPM_INBOUND_V2
    #   → read system files from  target_root/integration_def/GPM_INBOUND_V2
    base_redirect: str | None = None
    if not system_files and customer_files:
        rel_parts = Path(system_rel).parts
        # category = ALL segments except the artifact name (last part).
        # e.g. "integration_def/PTX_GPM_INBOUND_V2"       → "integration_def"
        #      "datasets/REPORT_TEMPLATE/impl.trade.fta.*" → "datasets/REPORT_TEMPLATE"
        #      "datasets/SEARCH/Impl.*"                    → "datasets/SEARCH"
        category = "/".join(rel_parts[:-1]) if len(rel_parts) >= 2 else ""

        # Rules 3 & 4: read BASE_*_NAME tag from any JSON file in the artifact.
        # Covers all artifact types: integration_def, datasets/REPORT_TEMPLATE,
        # datasets/SEARCH, datasets/REPORT, windowdefs, etc.
        base_name = _read_base_artifact_name(customer_files)
        if base_name and category:
            alt_system_dir = target_root / category / base_name
            if alt_system_dir.exists():
                system_files  = read_artifact_files(alt_system_dir)
                base_redirect = f"{category}/{base_name}"
                _log.info("[merge] BASE tag redirect: %s → %s (key=%s)", system_rel, base_redirect, key)

    baseline_files: dict[str, str] = {}
    if baseline_root and str(baseline_root) and baseline_root.exists():
        baseline_files = read_artifact_files(baseline_root / system_rel)
        # If the baseline also has no counterpart, try the same BASE redirect
        if not baseline_files and base_redirect:
            baseline_files = read_artifact_files(baseline_root / base_redirect)

    # ── Route large JSON straight to deterministic — don't wait on the LLM ───
    # The LLM can't reliably merge a multi-MB JSON, so don't even send it: it
    # would just burn minutes and drop the file. Exclude large JSON from the
    # LLM inputs; deterministic_fill() (below) merges them instantly. The LLM
    # still handles code + small JSON.
    from upgrade_lib.json_merge import LARGE_JSON_BYTES

    def _is_large_json(fname: str) -> bool:
        if not fname.endswith(".json") or fname.endswith("_diff.json"):
            return False
        return (
            len(customer_files.get(fname, "")) >= LARGE_JSON_BYTES
            or len(system_files.get(fname, "")) >= LARGE_JSON_BYTES
        )

    large_json = {
        f for f in (set(customer_files) | set(system_files)) if _is_large_json(f)
    }
    # _diff.json sidecars are NEVER merged by the LLM. They are handled
    # deterministically by the diff pipeline below (carried forward for large
    # artifacts, or regenerated from the merged base). Sending them to the LLM
    # is pure waste — they can be hundreds of KB (dominating merge runtime), the
    # output is discarded by the diff pipeline anyway, and the extra context
    # pressure makes the LLM more likely to truncate the real code files it is
    # merging alongside (e.g. dropping imports + helper methods from a .java).
    diff_json = {
        f for f in (set(customer_files) | set(system_files))
        if f.endswith("_diff.json")
    }
    exclude_from_llm = large_json | diff_json
    llm_customer = {f: c for f, c in customer_files.items() if f not in exclude_from_llm}
    llm_system = {f: c for f, c in system_files.items() if f not in exclude_from_llm}
    llm_baseline = {
        f: c for f, c in (baseline_files or {}).items() if f not in exclude_from_llm
    } or None
    if large_json:
        emit("large_json_deterministic", key=key, files=sorted(large_json))
        _log.info("[merge] %s: %d large JSON routed to deterministic (skipped LLM): %s",
                  key, len(large_json), sorted(large_json))
    if diff_json:
        emit("diff_json_excluded_from_llm", key=key, files=sorted(diff_json))
        _log.info("[merge] %s: %d _diff.json excluded from LLM (handled by diff pipeline): %s",
                  key, len(diff_json), sorted(diff_json))

    emit(
        "claude_merge_start",
        key=key,
        files=len(llm_customer),
        has_baseline=bool(llm_baseline),
    )
    merge_res = client.merge_artifact(
        llm_customer, llm_system, llm_baseline, customer=bucket
    )
    artifact_files, analysis_files = _split_analysis_files(merge_res["merged_files"])
    emit("claude_merge_done", key=key, files=len(artifact_files))

    # ── Deterministic backstop for large / dropped JSON ──────────────────────
    # The LLM can't hold a multi-MB JSON in context, so it silently drops or
    # mangles large files (e.g. a big integration_def.json). Deterministically
    # produce any mergeable file the LLM failed to output, and override large
    # JSON files. Small files keep the LLM output.
    try:
        from upgrade_lib.json_merge import deterministic_fill

        artifact_files, filled = deterministic_fill(
            artifact_files, customer_files, system_files, baseline_files
        )
        if filled:
            emit("deterministic_merge", key=key, files=filled)
            _log.info("[merge] deterministic fill for %s: %s", key, filled)
    except Exception as exc:  # noqa: BLE001 — backstop must never break a merge
        _log.warning("[merge] deterministic fill failed for %s: %s", key, exc)
        filled = []

    # WebLogic entries carry an explicit output_rel (app_root-relative path in
    # the Docker delivery layout). Docker entries omit it → out_root/bucket/rel.
    out_dir = (
        out_root / entry["output_rel"] if entry.get("output_rel")
        else out_root / bucket / rel
    )
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

    # ── Deterministic safety net: detect dropped pure-additions ─────────────
    # Catches the case where the LLM merge silently lost a line that one side
    # genuinely added (e.g. SYSTEM's new submitWorkToCMG call, or a customer
    # customization). Non-blocking, but surfaces as WARN findings so the
    # engineer sees it instead of shipping a wrong merge.
    dropped: list[dict] = []
    try:
        from upgrade_lib.quality.customization_guard import detect_dropped_additions

        dropped = detect_dropped_additions(
            customer_files, system_files, baseline_files, artifact_files
        )
    except Exception as exc:  # noqa: BLE001 — never let the guard break a merge
        _log.warning("[merge] customization guard failed for %s: %s", key, exc)
    if dropped:
        emit("customization_warning", key=key, count=len(dropped))
        if quality_result is None:
            quality_result = {"verdict": "WARN", "findings": [], "blocking": False}
        findings = quality_result.setdefault("findings", [])
        for d in dropped:
            findings.append({
                "severity": "WARNING",
                "category": "dropped_addition",
                "file": d["file"],
                "line": None,
                "message": d["message"],
            })
        if quality_result.get("verdict") == "PASS":
            quality_result["verdict"] = "WARN"

    diff_info = None
    diff_error: str | None = None
    diff_carried_forward = False
    customer_has_diff = any(
        name.endswith("_diff.json") for name in customer_files
    )
    if not skip_diff and not customer_has_diff:
        emit("diff_skipped", key=key, reason="no _diff.json in customer artifact")
    if not skip_diff and customer_has_diff:
        for fname, content in artifact_files.items():
            if fname.endswith(".json") and not fname.endswith("_diff.json"):
                sys_content = system_files.get(fname)
                diff_name = fname.replace(".json", "_diff.json")
                # ── Large JSON: carry the customer's existing _diff.json forward ──
                # The _diff.json captures the customer's customizations, which
                # persist across the upgrade (only the base version changes, and
                # the runtime re-applies the diff on the new base). An LLM can't
                # reliably recompute a multi-MB recursive diff, so for large
                # artifacts we deterministically carry the customer's diff
                # forward instead of regenerating it.
                if len(content) >= LARGE_JSON_BYTES and diff_name in customer_files:
                    (out_dir / diff_name).write_text(
                        customer_files[diff_name], encoding="utf-8"
                    )
                    diff_info = {"diff_json": customer_files[diff_name]}
                    diff_carried_forward = True
                    emit("diff_carried_forward", key=key, file=diff_name)
                    _log.info("[merge] %s: carried customer %s forward (large artifact)",
                              key, diff_name)
                elif len(content) >= LARGE_JSON_BYTES:
                    # large but no existing customer diff to carry — skip rather
                    # than attempt an unreliable LLM diff of a multi-MB file
                    emit("diff_skipped", key=key, file=fname, reason="large artifact, no customer _diff.json")
                elif sys_content:
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
                        (out_dir / diff_name).write_text(
                            diff_info["diff_json"], encoding="utf-8"
                        )
                        emit("diff_done", key=key, file=fname)
                    except Exception as e:
                        diff_error = str(e)
                        emit("diff_failed", key=key, file=fname, error=str(e))
                break

    merge_duration_seconds = round(time.monotonic() - merge_start, 1)
    explanation = merge_res.get("explanation", "")

    # ── Rule 6: bizpolicydefs / bizruledefs / multilegresolver — DB warning ──
    # These artifact types are stored both as files AND as database records.
    # The old DB entry must be deleted before upgrade to avoid conflicts.
    rel_parts = Path(rel).parts
    db_warning: str | None = None
    artifact_category = rel_parts[0] if rel_parts else ""
    if artifact_category in {"bizpolicydefs"}:
        db_warning = (
            f"\n\n⚠ DB ACTION REQUIRED: {artifact_category} artifacts "
            "require manual DB handling. Delete the previous entry in the "
            "database BEFORE applying the upgrade to this environment."
        )
        explanation = explanation + db_warning

    return {
        "bucket": bucket,
        "rel_path": rel,
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "merge_duration_seconds": merge_duration_seconds,
        # True when the artifact had no customer-specific content at compare time.
        # Future git automation can use this flag to prompt the engineer before
        # skipping check-in (take from SYSTEM directly instead).
        "no_customer_content": bool(entry.get("no_customer_content_note")),
        "files": list(artifact_files.keys()),
        "explanation": explanation,
        "base_redirect": base_redirect,
        "db_warning": db_warning,
        "diff_generated": diff_info is not None,
        "diff_carried_forward": diff_carried_forward,
        "diff_error": diff_error,
        "out_dir": str(out_dir),
        "quality_result": quality_result,
        "dropped_additions": dropped,
        "deterministic_files": filled,
    }
