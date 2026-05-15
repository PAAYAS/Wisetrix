"""
Deterministic local comparison — ported verbatim from v1's
upgrade-mcp-server/server.py.

No Claude / SDK calls. Pure Python.

Used by app.py during the Scan & Compare phase so the UI stays fast.
Claude is only called for actual MERGE work (where semantics matter).
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------- #
# Configuration (unchanged from v1)
# --------------------------------------------------------------------------- #

INCLUDE_EXTS = {".json", ".xml", ".java", ".js", ".jsp"}
EXCLUDE_NAMES = {"component.info", "security.txt"}
RETAIN_CATEGORIES = {"custom_privilages"}


# --------------------------------------------------------------------------- #
# File comparison helpers (UNCHANGED from v1)
# --------------------------------------------------------------------------- #

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _compare_json(src: str, tgt: str) -> bool:
    """Semantic JSON compare — key order irrelevant."""
    try:
        return json.loads(src) == json.loads(tgt)
    except (json.JSONDecodeError, Exception):
        return src.strip() == tgt.strip()


def _compare_xml(src: str, tgt: str) -> bool:
    """Semantic XML compare — whitespace-only diffs ignored."""
    try:
        def _norm(elem):
            return {
                "tag": elem.tag,
                "attrib": dict(sorted(elem.attrib.items())),
                "text": (elem.text or "").strip(),
                "tail": (elem.tail or "").strip(),
                "children": [_norm(c) for c in elem],
            }
        return _norm(ET.fromstring(src)) == _norm(ET.fromstring(tgt))
    except Exception:
        return (
            [l.strip() for l in src.splitlines() if l.strip()]
            == [l.strip() for l in tgt.splitlines() if l.strip()]
        )


def _is_comment_only_change(aldi_lines: list, sys_lines: list) -> bool:
    """
    True if the only difference between aldi_lines and sys_lines is comment
    markers on one side — meaning an intentional enable/disable decision.

    Covers per-line, same line count:
      1. ALDI:  // code     SYSTEM:  code
      2. ALDI:  code        SYSTEM:  // code
      3. ALDI:  /* code     SYSTEM:  code
      4. ALDI:  code        SYSTEM:  /* code
      5. */ vs blank/whitespace (either side)
    """
    if len(aldi_lines) != len(sys_lines):
        return False
    for a_line, s_line in zip(aldi_lines, sys_lines):
        a = a_line.strip()
        s = s_line.strip()
        if a == s:
            continue
        if a.startswith("//") and a[2:].strip() == s:
            continue
        if s.startswith("//") and s[2:].strip() == a:
            continue
        if a.startswith("/*") and a[2:].strip() == s:
            continue
        if s.startswith("/*") and s[2:].strip() == a:
            continue
        if a.startswith("*/") and a[2:].strip() == "" and s == "":
            continue
        if s.startswith("*/") and s[2:].strip() == "" and a == "":
            continue
        return False
    return True


def _compare_code(src: str, tgt: str) -> bool:
    """Exact line-by-line code compare (normalised line endings).
    Comment-only differences are treated as identical — either side commenting
    out lines the other keeps active is an intentional customisation."""
    def n(s: str) -> str:
        return s.replace("\r\n", "\n").replace("\r", "\n").strip()
    if n(src) == n(tgt):
        return True
    return _is_comment_only_change(n(src).splitlines(), n(tgt).splitlines())


def _compare_files(src_path: str, tgt_path: str) -> str:
    """Returns 'identical' | 'different' | 'error:<msg>'."""
    try:
        src, tgt = _read(src_path), _read(tgt_path)
        ext = Path(src_path).suffix.lower()
        if ext == ".json":
            same = _compare_json(src, tgt)
        elif ext == ".xml":
            same = _compare_xml(src, tgt)
        else:
            same = _compare_code(src, tgt)
        return "identical" if same else "different"
    except Exception as exc:
        return f"error:{exc}"


# --------------------------------------------------------------------------- #
# Decision rollup (UNCHANGED from v1)
# --------------------------------------------------------------------------- #

def _is_retain_category(rel_path: str) -> bool:
    """True if any path segment matches a RETAIN_CATEGORIES entry."""
    parts = Path(rel_path).parts
    return any(p in RETAIN_CATEGORIES for p in parts)


def _artifact_decision(file_decisions: dict) -> str:
    """
    Roll up per-file decisions to an artifact-level decision.
    Mirrors v1 exactly.
    """
    d = set(file_decisions.values())
    if "Merge" in d:
        return "Merge"
    if "Retain" in d and "No Changes" in d:
        return "Retain"
    if d == {"Retain"}:
        return "Retain"
    if d == {"No Changes"}:
        return "Remove"
    return "Retain"


# --------------------------------------------------------------------------- #
# Public API — drop-in for the UI's compare step
# --------------------------------------------------------------------------- #

def compare_artifact_local(
    src_artifact: Path,
    tgt_artifact: Path,
    rel_path: str,
) -> dict:
    """
    Local, deterministic compare. Same shape as the Claude-based result so
    the UI doesn't need to branch:

    {
        "decision":      "Merge" | "Retain" | "Remove",
        "file_decisions": { filename: "Merge"|"Retain"|"No Changes" },
        "file_details":   [{file, decision, target_exists, [diff_summary]}],
        "target_exists":  bool,
        "target_path":    str,
        "file_count":     int,
        "analysis":       "<short string explaining how the rollup happened>",
        "engine":         "local",
    }
    """
    src_artifact = Path(src_artifact)
    tgt_artifact = Path(tgt_artifact)

    target_exists = tgt_artifact.is_dir()
    file_decisions: dict[str, str] = {}
    file_details: list[dict] = []

    # ── Retain-only category: skip comparison, all files = Retain ──────────
    if _is_retain_category(rel_path):
        if src_artifact.is_dir():
            for f in src_artifact.iterdir():
                if not f.is_file():
                    continue
                if f.name in EXCLUDE_NAMES or f.suffix.lower() not in INCLUDE_EXTS:
                    continue
                file_decisions[f.name] = "Retain"
                file_details.append({
                    "file": f.name,
                    "decision": "Retain",
                    "target_exists": False,
                })
        return {
            "decision": "Retain",
            "file_decisions": file_decisions,
            "file_details": file_details,
            "target_exists": target_exists,
            "target_path": str(tgt_artifact),
            "file_count": len(file_decisions),
            "analysis": "Retain-only category (custom_privilages) — source kept as-is.",
            "engine": "local",
        }

    # ── Standard compare ───────────────────────────────────────────────────
    if src_artifact.is_dir():
        for f in src_artifact.iterdir():
            if not f.is_file():
                continue
            if f.name in EXCLUDE_NAMES or f.suffix.lower() not in INCLUDE_EXTS:
                continue
            tgt_file = tgt_artifact / f.name
            tgt_exists = target_exists and tgt_file.exists()
            if not tgt_exists:
                decision = "Retain"
                detail = {
                    "file": f.name,
                    "decision": decision,
                    "target_exists": False,
                }
            else:
                cmp = _compare_files(str(f), str(tgt_file))
                if cmp == "identical":
                    decision = "No Changes"
                elif cmp == "different":
                    decision = "Merge"
                else:
                    decision = "Merge"  # error → require human/AI review
                detail = {
                    "file": f.name,
                    "decision": decision,
                    "target_exists": True,
                    "compare_result": cmp,
                }
            file_decisions[f.name] = decision
            file_details.append(detail)

    artifact_decision = _artifact_decision(file_decisions)

    # Brief human-readable analysis
    counts = {}
    for v in file_decisions.values():
        counts[v] = counts.get(v, 0) + 1
    parts = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    analysis = (
        f"{len(file_decisions)} file(s) compared · {parts or 'no files'}"
        if file_decisions
        else "No comparable files found in source."
    )
    if not target_exists:
        analysis += "  Target artifact does not exist — source-only."

    return {
        "decision": artifact_decision,
        "file_decisions": file_decisions,
        "file_details": file_details,
        "target_exists": target_exists,
        "target_path": str(tgt_artifact),
        "file_count": len(file_decisions),
        "analysis": analysis,
        "engine": "local",
    }


# --------------------------------------------------------------------------- #
# Post-pass business rules (UNCHANGED from v1 — apply AFTER all artifacts
# have been individually compared).
#
#  Rule 1: windowdefs/{name} superseded by adhoc_windowdefs/{name} → Remove
#  Rule 2: datasets/SEARCH/ADHOC_SEARCH_{name} superseded by
#          datasets/REPORT/{name} → Remove
# --------------------------------------------------------------------------- #

def apply_business_rules(results: dict) -> dict:
    """
    Mutates and returns `results` (mapping of source_rel → result dict).

    Each result is expected to carry: bucket, rel_path, decision (and the
    other fields produced by compare_artifact_local()). Business rules
    are scoped per bucket — comparison is only against artifacts in the
    same bucket (ALDI customizations don't auto-remove SYSTEM bucket and
    vice-versa).
    """
    # ── Rule 1: windowdefs superseded by adhoc_windowdefs ───────────────
    adhoc_names: dict[str, set] = {}
    for r in results.values():
        rel = r.get("rel_path", "")
        bucket = r.get("bucket", "")
        parts = rel.split("/")
        if parts and parts[0] == "adhoc_windowdefs":
            adhoc_names.setdefault(bucket, set()).add(parts[-1])

    for r in results.values():
        rel = r.get("rel_path", "")
        bucket = r.get("bucket", "")
        parts = rel.split("/")
        if parts and parts[0] == "windowdefs":
            name = parts[-1]
            if name in adhoc_names.get(bucket, set()):
                r["decision"] = "Remove"
                r["decision_note"] = (
                    "Auto-removed: adhoc_windowdefs counterpart exists in source"
                )

    # ── Rule 2: datasets/SEARCH superseded by datasets/REPORT ───────────
    report_names: dict[str, set] = {}
    for r in results.values():
        rel = r.get("rel_path", "")
        bucket = r.get("bucket", "")
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "datasets" and parts[1] == "REPORT":
            report_names.setdefault(bucket, set()).add(parts[-1])

    for r in results.values():
        rel = r.get("rel_path", "")
        bucket = r.get("bucket", "")
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "datasets" and parts[1] == "SEARCH":
            search_name = parts[-1]
            if search_name.startswith("ADHOC_SEARCH_"):
                base = search_name[len("ADHOC_SEARCH_"):]
                if base in report_names.get(bucket, set()):
                    r["decision"] = "Remove"
                    r["decision_note"] = (
                        f"Auto-removed: datasets/REPORT/{base} counterpart exists in source"
                    )

    return results
