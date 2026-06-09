"""
Deterministic local comparison — ported verbatim from v1's
upgrade-mcp-server/server.py.

No Claude / SDK calls. Pure Python.

Used by app.py during the Scan & Compare phase so the UI stays fast.
Claude is only called for actual MERGE work (where semantics matter).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

INCLUDE_EXTS = {".json", ".xml", ".java", ".js", ".jsp"}
EXCLUDE_NAMES = {"component.info", "security.txt"}

# Categories that are always Retain — no comparison against SYSTEM.
# custom_privilages: customer access config, never in SYSTEM.
# dgs:              Document Generation System artifacts — not migrated via this tool.
RETAIN_CATEGORIES = {"custom_privilages", "dgs"}

# Categories that require a manual DB action before the upgrade is applied.
# Artifacts in these categories are stored both as files AND as database records.
# The old DB entry must be deleted before the upgrade so there is no conflict.
DB_WARNING_CATEGORIES = {"bizpolicydefs"}

# Regex matching any BASE_*_NAME tag in a JSON artifact file.
# Used by Rules 3 & 4: when a customer artifact has no SYSTEM counterpart,
# read this tag to find the SYSTEM base artifact to compare/merge against.
# e.g. BASE_INTEGRATION_DEF_NAME, BASE_DATASET_NAME, BASE_REPORT_NAME …
_BASE_TAG_RE = re.compile(r"^BASE_.+_NAME$")


# --------------------------------------------------------------------------- #
# BASE_*_NAME redirect helper  (Rules 3 & 4)
# --------------------------------------------------------------------------- #

def _read_base_artifact_name(src_artifact: Path) -> str | None:
    """Scan JSON files in *src_artifact* for a BASE_*_NAME tag.

    Returns the tag value (e.g. "GPM_INBOUND_V2") or None.

    Used when the customer artifact has no counterpart in the SYSTEM target:
    instead of treating it as source-only (Retain), we redirect the comparison
    to the SYSTEM base artifact named by the tag.

    Example:
      PTX_GPM_INBOUND_V2/integration_def.json  →  {"BASE_INTEGRATION_DEF_NAME": "GPM_INBOUND_V2"}
      → compare against SYSTEM/integration_def/GPM_INBOUND_V2 instead
    """
    if not src_artifact.is_dir():
        return None
    for f in src_artifact.iterdir():
        if not f.is_file() or f.suffix.lower() != ".json":
            continue
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)
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


# --------------------------------------------------------------------------- #
# File comparison helpers (UNCHANGED from v1)
# --------------------------------------------------------------------------- #

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Smart JSON comparison helpers
# ---------------------------------------------------------------------------

# Fields that carry no functional meaning for comparison purposes.
# Sequence numbers change whenever rules are inserted/reordered by SYSTEM.
# Metadata fields (ORG_CODE, dates) are always different between SYSTEM and
# customer but are NOT customer customisations.
_COMPARISON_IGNORE_KEYS: frozenset[str] = frozenset({
    # Sequence / ordering numbers — irrelevant for identity comparison
    "EXEC_SEQ",
    "ROW_SEQ",
    "SET_VALIDATION_ID",
    # Administrative metadata — always differs between SYSTEM and customer
    "ORG_CODE",
    "CREATED_BY",
    "CREATED_DATE",
    "LAST_MODIFIED_BY",
    "LAST_MODIFIED_DATE",
    "COMPANY_CODE",
    "RULE_COMPANY_CODE",
})

# Keys that uniquely identify a record inside an array.
# When sorting arrays for stable comparison we prefer these in order.
_ARRAY_IDENTITY_KEYS: tuple[str, ...] = (
    "RULE_ID",
    "INSTANCE_ID",
    "FIELD_ID",
    "FIELD_NAME",
    "VALIDATION_ID",
    "INTERNAL_ID",
    "COLUMN_NAME",
    "MENU_ITEM_ID",
    "OBJECT_ID",
)


def _normalize_for_comparison(obj: object) -> object:
    """Recursively strip noise fields and sort arrays by identity key.

    This makes the comparison semantic rather than positional:
      - EXEC_SEQ / ROW_SEQ differences are ignored (sequence numbers only)
      - ORG_CODE / date metadata are ignored (always differ, never custom)
      - Arrays of records are sorted by their identity key (RULE_ID etc.)
        so that insertions/reorderings by SYSTEM don't produce false diffs
    """
    if isinstance(obj, dict):
        return {
            k: _normalize_for_comparison(v)
            for k, v in obj.items()
            if k not in _COMPARISON_IGNORE_KEYS
        }
    if isinstance(obj, list):
        normalised = [_normalize_for_comparison(item) for item in obj]
        # Sort arrays of dicts by their identity key so order doesn't matter
        if normalised and isinstance(normalised[0], dict):
            for id_key in _ARRAY_IDENTITY_KEYS:
                if any(id_key in item for item in normalised if isinstance(item, dict)):
                    try:
                        return sorted(
                            normalised,
                            key=lambda x: str(x.get(id_key, "")) if isinstance(x, dict) else str(x),
                        )
                    except TypeError:
                        break
        return normalised
    return obj


def _customer_has_unique_content(src: object, tgt: object) -> bool:
    """Return True if src (customer) has any content that tgt (SYSTEM) does not.

    Used to decide whether to show the "no customer-specific content" note.

    False means the customer artifact is a subset of SYSTEM — after merge
    the result would be identical to SYSTEM, so the artifact could be
    removed from customer git and taken directly from SYSTEM instead.

    Scenarios that return False (note fires):
      - SYSTEM added a new rule AGCO doesn't have, AGCO has nothing unique
    Scenarios that return True (note suppressed):
      - AGCO modified an existing rule value
      - AGCO added a new rule SYSTEM doesn't have
      - AGCO has any field with a different value (non-noise fields)
    """
    if isinstance(src, dict) and isinstance(tgt, dict):
        for k, v in src.items():
            if k not in tgt:
                return True   # AGCO has a key SYSTEM doesn't → unique
            if _customer_has_unique_content(v, tgt[k]):
                return True
        return False

    if isinstance(src, list) and isinstance(tgt, list):
        if src and isinstance(src[0], dict):
            # Find the identity key used for this array
            for id_key in _ARRAY_IDENTITY_KEYS:
                if any(id_key in item for item in src if isinstance(item, dict)):
                    tgt_by_id = {
                        item.get(id_key): item
                        for item in tgt
                        if isinstance(item, dict)
                    }
                    for src_item in src:
                        if not isinstance(src_item, dict):
                            continue
                        key_val = src_item.get(id_key)
                        if key_val not in tgt_by_id:
                            return True   # AGCO has a record SYSTEM doesn't
                        if _customer_has_unique_content(src_item, tgt_by_id[key_val]):
                            return True   # same record but AGCO modified it
                    return False   # all AGCO records exist unchanged in SYSTEM
        return src != tgt   # plain list — fall back to equality

    return src != tgt   # scalar: any difference is unique content


def _compare_json(src: str, tgt: str) -> bool:
    """Semantic JSON compare.

    Ignores:
      - Key ordering (dicts are unordered)
      - EXEC_SEQ / ROW_SEQ — sequence numbers that change on insertions
      - ORG_CODE, dates, CREATED_BY — metadata that always differs
      - Array element ordering — sorted by identity key (RULE_ID etc.)

    Result: two JSON files are considered IDENTICAL if their functional
    content is the same even if SYSTEM renumbered rules or metadata differs.
    This prevents false Merge decisions caused purely by EXEC_SEQ changes.
    """
    try:
        src_norm = _normalize_for_comparison(json.loads(src))
        tgt_norm = _normalize_for_comparison(json.loads(tgt))
        return src_norm == tgt_norm
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


def _is_comment_only_change(src_lines: list, sys_lines: list) -> bool:
    """
    True if the only difference between src_lines and sys_lines is comment
    markers on one side — meaning an intentional enable/disable decision.

    Covers per-line, same line count:
      1. source:  // code     SYSTEM:  code
      2. source:  code        SYSTEM:  // code
      3. source:  /* code     SYSTEM:  code
      4. source:  code        SYSTEM:  /* code
      5. */ vs blank/whitespace (either side)
    """
    if len(src_lines) != len(sys_lines):
        return False
    for a_line, s_line in zip(src_lines, sys_lines):
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
    return _is_comment_only_change(n(src).splitlines(), n(tgt).splitlines())  # src_lines, sys_lines


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
    target_root: Path | None = None,
) -> dict:
    """
    Local, deterministic compare. Same shape as the Claude-based result so
    the UI doesn't need to branch:

    {
        "decision":       "Merge" | "Retain" | "Remove",
        "file_decisions": { filename: "Merge"|"Retain"|"No Changes" },
        "file_details":   [{file, decision, target_exists, [diff_summary]}],
        "target_exists":  bool,
        "target_path":    str,
        "file_count":     int,
        "analysis":       "<short string explaining how the rollup happened>",
        "engine":         "local",
        "base_redirect":  str | None,   # set when Rules 3/4 redirect applies
    }

    target_root (optional) — the root of the SYSTEM artifact tree.
    Passing it enables:
      - Rules 3 & 4: BASE_*_NAME redirect for missing-target artifacts.
    """
    src_artifact = Path(src_artifact)
    tgt_artifact = Path(tgt_artifact)

    target_exists = tgt_artifact.is_dir()

    # ── Rules 3 & 4: BASE_*_NAME redirect ─────────────────────────────────────
    # When the customer artifact has no counterpart in SYSTEM (target_exists=False),
    # read the BASE_*_NAME tag from the artifact's JSON and re-point tgt_artifact
    # to the named SYSTEM base artifact.  This ensures the comparison/merge is done
    # against the correct upstream, not against nothing.
    # Example: PTX_GPM_INBOUND_V2 → BASE_INTEGRATION_DEF_NAME=GPM_INBOUND_V2
    #          → compare against SYSTEM/integration_def/GPM_INBOUND_V2
    base_redirect: str | None = None
    if not target_exists and target_root is not None:
        base_name = _read_base_artifact_name(src_artifact)
        if base_name:
            # category = ALL segments except the artifact name (last part).
            # e.g. "integration_def/PTX_GPM_INBOUND_V2"       → "integration_def"
            #      "datasets/REPORT_TEMPLATE/impl.trade.fta.*" → "datasets/REPORT_TEMPLATE"
            #      "datasets/SEARCH/Impl.*"                    → "datasets/SEARCH"
            parts = Path(rel_path).parts
            category = "/".join(parts[:-1]) if len(parts) >= 2 else ""
            if category:
                alt_tgt = Path(target_root) / category / base_name
                if alt_tgt.is_dir():
                    tgt_artifact  = alt_tgt
                    target_exists = True
                    base_redirect = base_name

    file_decisions: dict[str, str] = {}
    file_details: list[dict] = []
    # Tracks whether every JSON file that produced a Merge decision has
    # no customer-unique content (AGCO is a subset of SYSTEM).
    # Used to set the no_customer_content_note on the artifact.
    _merge_json_count = 0
    _all_merge_json_no_unique = True

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
        retain_label = Path(rel_path).parts[0] if Path(rel_path).parts else "retain-only"
        return {
            "decision": "Retain",
            "file_decisions": file_decisions,
            "file_details": file_details,
            "target_exists": target_exists,
            "target_path": str(tgt_artifact),
            "file_count": len(file_decisions),
            "analysis": f"Retain-only category ({retain_label}) — source kept as-is.",
            "engine": "local",
            "base_redirect": None,
            "no_customer_content_note": None,
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
                    # For JSON files, check whether AGCO has anything unique.
                    # If not, the file contributes to the no_customer_content note.
                    if f.suffix.lower() == ".json":
                        _merge_json_count += 1
                        try:
                            src_norm = _normalize_for_comparison(
                                json.loads(_read(str(f)))
                            )
                            tgt_norm = _normalize_for_comparison(
                                json.loads(_read(str(tgt_file)))
                            )
                            if _customer_has_unique_content(src_norm, tgt_norm):
                                _all_merge_json_no_unique = False
                        except Exception:
                            # Can't determine — assume unique to be safe
                            _all_merge_json_no_unique = False
                    else:
                        # Non-JSON merge (Java, XML etc.) — always treat as unique
                        _all_merge_json_no_unique = False
                else:
                    decision = "Merge"  # error → require human/AI review
                    _all_merge_json_no_unique = False
                detail = {
                    "file": f.name,
                    "decision": decision,
                    "target_exists": True,
                    "compare_result": cmp,
                }
            file_decisions[f.name] = decision
            file_details.append(detail)

    artifact_decision = _artifact_decision(file_decisions)

    # ── No-customer-content note ──────────────────────────────────────────────
    # If every JSON file that triggered Merge has no AGCO-unique content,
    # flag the artifact so the engineer can consider removing it from
    # customer git after upgrade (SYSTEM will provide it directly).
    # Not applied to BASE-redirect artifacts (different artifact name / purpose).
    no_customer_content_note: str | None = None
    if (
        artifact_decision == "Merge"
        and _merge_json_count > 0
        and _all_merge_json_no_unique
        and base_redirect is None
    ):
        no_customer_content_note = (
            "No customer-specific content detected — after merge this artifact "
            "will be identical to SYSTEM 26.2. Consider removing from customer "
            "git after upgrade completes."
        )

    # ── BASE redirect always forces Merge ─────────────────────────────────────
    # When a BASE_*_NAME tag redirected us to a different SYSTEM artifact, the
    # customer artifact is a CUSTOM EXTENSION of that SYSTEM base.  Even if
    # individual files appear identical, SYSTEM 26.2 may have changed the base
    # and Claude must evaluate and apply those changes.
    # We never auto-Remove or skip a BASE redirect artifact.
    if base_redirect is not None and target_exists:
        artifact_decision = "Merge"

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
    if base_redirect is not None and target_exists:
        analysis += f"  BASE redirect → {base_redirect} — always Merge."

    return {
        "decision": artifact_decision,
        "file_decisions": file_decisions,
        "file_details": file_details,
        "target_exists": target_exists,
        "target_path": str(tgt_artifact),
        "file_count": len(file_decisions),
        "analysis": analysis,
        "engine": "local",
        "base_redirect": base_redirect,
        "no_customer_content_note": no_customer_content_note,
    }


# --------------------------------------------------------------------------- #
# Post-pass business rules (UNCHANGED from v1 — apply AFTER all artifacts
# have been individually compared).
#
#  Rule 1: windowdefs/{name} superseded by adhoc_windowdefs/{name} → Remove
#  Rule 2: datasets/SEARCH/ADHOC_SEARCH_{name} superseded by
#          datasets/REPORT/{name} → Remove
# --------------------------------------------------------------------------- #

def apply_business_rules(
    results: dict,
    target_root: Path | None = None,
) -> dict:
    """
    Mutates and returns `results` (mapping of source_rel → result dict).

    Each result is expected to carry: bucket, rel_path, decision (and the
    other fields produced by compare_artifact_local()). Business rules
    are scoped per bucket — comparison is only against artifacts in the
    same bucket (ALDI customizations don't auto-remove SYSTEM bucket and
    vice-versa).

    target_root (optional) — pass to enable Rules 2 and 5 which need to
    inspect the SYSTEM target directory.
    """
    # ── Rule 1 (existing): windowdefs superseded by adhoc_windowdefs ────────
    # Build a set of adhoc_windowdefs artifact names per bucket.
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

    # ── Rule 2 (new): windowdefs → adhoc_windowdefs promotion ───────────────
    # If a customer has windowdefs/{name} but NOT adhoc_windowdefs/{name},
    # and the TARGET system has adhoc_windowdefs/{name}, remove the windowdefs
    # entry and flag it for merging customizations into adhoc_windowdefs.
    if target_root is not None:
        tgt = Path(target_root)
        for r in results.values():
            rel = r.get("rel_path", "")
            bucket = r.get("bucket", "")
            parts = rel.split("/")
            if (
                parts
                and parts[0] == "windowdefs"
                and r.get("decision") != "Remove"
            ):
                name = parts[-1]
                # Only apply if Rule 1 didn't already handle it
                # (i.e. no adhoc counterpart in the customer source)
                if name not in adhoc_names.get(bucket, set()):
                    adhoc_in_target = tgt / "adhoc_windowdefs" / name
                    if adhoc_in_target.is_dir():
                        r["decision"] = "Remove"
                        r["decision_note"] = (
                            f"windowdefs promoted: adhoc_windowdefs/{name} exists in "
                            f"target — merge {name} customizations into "
                            f"adhoc_windowdefs/{name} of the target version"
                        )

    # ── Existing rule: datasets/SEARCH superseded by datasets/REPORT ────────
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

    # ── Rule 5 (new): __env_specific bucket ─────────────────────────────────
    # rel_path for __env_specific artifacts is  {ENV}/{CUSTOMER}/{category}/…
    # scan.py strips the ENV/CUSTOMER prefix and looks up the SYSTEM counterpart.
    # Two cases:
    #   a) target_exists=False → no SYSTEM counterpart (e.g. integration_def_config)
    #      → always Retain: purely env-specific config, nothing to compare against
    #   b) target_exists=True  → SYSTEM has this artifact type (e.g. datasets/REPORT)
    #      → keep the real comparison result (may be Merge/Retain/Remove) so that
    #        structural changes in SYSTEM 26.2 are caught and surfaced to the engineer
    for r in results.values():
        bucket = r.get("bucket", "")
        if not bucket.startswith("__env_specific"):
            continue
        if not r.get("target_exists", False):
            # Case a: no SYSTEM counterpart — pure env config
            r["decision"] = "Retain"
            r["decision_note"] = (
                "Environment-specific artifact — no SYSTEM counterpart, "
                "retained as-is (e.g. integration_def_config transport settings)"
            )
        else:
            # Case b: SYSTEM has this artifact — keep comparison result,
            # but annotate so the engineer knows it is env-specific
            existing = r.get("decision_note", "")
            r["decision_note"] = (
                f"[env_specific] {existing}".strip() if existing
                else "[env_specific] Compared against SYSTEM base artifact — "
                     "preserve environment-specific values, adopt SYSTEM structural changes"
            )

    # ── Rule 6 (new): bizpolicydefs / bizruledefs / multilegresolver — DB warning
    # These artifact types are stored both as files AND as database records.
    # The previous DB entry must be deleted before the upgrade is applied,
    # otherwise the upgrade will conflict with the existing record.
    for r in results.values():
        category = r.get("category", "")
        if (
            category in DB_WARNING_CATEGORIES
            and r.get("decision") != "Remove"
        ):
            r["db_warning"] = (
                f"⚠ DB ACTION REQUIRED: {category} artifacts require manual "
                "DB handling. Delete the previous entry in the database "
                "BEFORE applying the upgrade to this environment."
            )

    return results
