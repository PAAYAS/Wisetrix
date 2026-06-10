"""Deterministic safety net for code merges: detect dropped additions.

The 3-way merge of code files is performed by Claude. To make sure a method or
a statement that EITHER side genuinely *added* (relative to the baseline common
ancestor) is never silently lost, this module compares the merged output
against baseline / customer / system and flags anything that should be present
but isn't.

It works at TWO granularities so it stays accurate without false positives:

  1. Whole-method: a method that is NEW on one side (present there, absent in
     baseline) and absent on the other side must appear in the merged output.
     - Catches a dropped new SYSTEM method (e.g. submitWorkToCMG) or a dropped
       new customer method.
     - Does NOT flag methods the customer intentionally *deleted* (those exist
       in the baseline, so they are not "new").

  2. Within a surviving method: for a method present in the merged output, any
     statement one side added relative to the baseline version of THAT method,
     and which the other side doesn't have, must appear in the merged method
     body.
     - Catches the afterEntitySave case: SYSTEM 26.2 added the
       `submitWorkToCMG(...)` call inside afterEntitySave; if the merged
       afterEntitySave drops it, it is flagged.
     - Skips methods the customer deleted (they aren't in the merged output),
       so SYSTEM's edits inside a deleted method never register.

For JSON/XML artifacts the same idea applies at structural granularity: a
leaf path (and value) that ONE side added relative to baseline, that the other
side did not contest, must be present in the merged output. Resequencing /
metadata noise is ignored (reuses the comparison normaliser), and a path
changed on BOTH sides (a real conflict the merge resolves) is never flagged.

This is a heuristic *detector*, not a merger: it raises visibility (WARN
findings) rather than silently passing. Requires the baseline to be present;
with no baseline it can't classify additions, so it no-ops.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

_log = logging.getLogger(__name__)

_CODE_EXTS = {".java", ".js", ".jsp", ".groovy"}
_JSON_EXTS = {".json"}
_XML_EXTS = {".xml"}

# Skip the guard on files larger than this (bytes) so a pathological artifact
# can never slow a merge. A note is emitted instead.
_MAX_BYTES = 5_000_000

# Cap findings per file so a wholly-dropped large record can't flood the report.
_MAX_FINDINGS_PER_FILE = 25

_TRIVIAL_RE = re.compile(r"^[\s{}();,\]\[]*$")

# A method declaration: an access modifier, a return type, a name, an arg list,
# optional `throws ...`, then the opening brace.
_METHOD_RE = re.compile(
    r"(?:public|private|protected)\s+[\w<>\[\],\s]+?\s+(\w+)\s*\([^;{]*\)\s*"
    r"(?:throws[\w\s,\.]+)?\{"
)


def _nokey(s: str) -> str:
    """Whitespace-insensitive key (collapse ALL whitespace)."""
    return re.sub(r"\s+", "", s)


def _match_brace(text: str, open_idx: int) -> int:
    """Index just past the `}` matching the `{` at open_idx.

    Skips braces inside double/single-quoted strings and // or /* */ comments.
    """
    depth = 0
    i = open_idx
    n = len(text)
    in_str = ""          # '"' or "'" when inside a string
    in_line = False      # // comment
    in_block = False     # /* */ comment
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_str:
            if c == "\\":
                i += 1
            elif c == in_str:
                in_str = ""
        elif c == "/" and nxt == "/":
            in_line = True
            i += 1
        elif c == "/" and nxt == "*":
            in_block = True
            i += 1
        elif c in ('"', "'"):
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _methods(text: str) -> dict[str, str]:
    """Map method name → body text (between its braces). Last definition wins."""
    out: dict[str, str] = {}
    for m in _METHOD_RE.finditer(text):
        name = m.group(1)
        open_idx = m.end() - 1  # position of the '{'
        end = _match_brace(text, open_idx)
        out[name] = text[open_idx + 1 : end - 1]
    return out


def _sig_keys(text: str) -> dict[str, str]:
    """Whitespace-insensitive key → display line, for substantive lines."""
    out: dict[str, str] = {}
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or _TRIVIAL_RE.match(line):
            continue
        key = _nokey(line)
        if len(key) < 8:
            continue
        out.setdefault(key, line)
    return out


def _check_code_file(fname, base, cust, syst, merged_text) -> list[dict]:
    """Method- and statement-level dropped-addition check for code files."""
    findings: list[dict] = []
    base_m = _methods(base)
    cust_m = _methods(cust)
    syst_m = _methods(syst)
    merged_m = _methods(merged_text)

    # 1) Whole-method additions that vanished
    for name in syst_m:
        if name not in base_m and name not in cust_m and name not in merged_m:
            findings.append({
                "file": fname, "side": "SYSTEM", "kind": "method", "line": name,
                "message": (
                    f"SYSTEM 26.2 added method '{name}()' but it is missing "
                    f"from the merged output (upgrade method dropped)."
                ),
            })
    for name in cust_m:
        if name not in base_m and name not in syst_m and name not in merged_m:
            findings.append({
                "file": fname, "side": "customer", "kind": "method", "line": name,
                "message": (
                    f"Customer added method '{name}()' but it is missing "
                    f"from the merged output (customization dropped)."
                ),
            })

    # 2) Statement additions inside a surviving method
    for name, merged_body in merged_m.items():
        base_body = base_m.get(name)
        if base_body is None:
            continue
        base_keys = set(_sig_keys(base_body))
        merged_blob = _nokey(merged_body)
        if name in syst_m:
            cust_keys = set(_sig_keys(cust_m.get(name, "")))
            for key, disp in _sig_keys(syst_m[name]).items():
                if key in base_keys or key in cust_keys:
                    continue
                if key not in merged_blob:
                    findings.append({
                        "file": fname, "side": "SYSTEM", "kind": "statement",
                        "line": disp,
                        "message": (
                            f"SYSTEM 26.2 added this line inside {name}() but it "
                            f"is missing from the merged output: {disp}"
                        ),
                    })
        if name in cust_m:
            syst_keys = set(_sig_keys(syst_m.get(name, "")))
            for key, disp in _sig_keys(cust_m[name]).items():
                if key in base_keys or key in syst_keys:
                    continue
                if key not in merged_blob:
                    findings.append({
                        "file": fname, "side": "customer", "kind": "statement",
                        "line": disp,
                        "message": (
                            f"Customer customization inside {name}() is missing "
                            f"from the merged output: {disp}"
                        ),
                    })
    return findings


# --------------------------------------------------------------------------- #
# JSON / XML structural flattening
# --------------------------------------------------------------------------- #

def _vrepr(v) -> str:
    try:
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(v)


def _flatten_json(obj, prefix: str, out: set[tuple[str, str]]) -> None:
    """Flatten normalised JSON into a set of (path, value-repr) leaves.

    Arrays of records are keyed by an identity field (RULE_ID/INSTANCE_ID/…) so
    a record's leaves get a stable path regardless of ordering; scalar arrays
    are flattened order-independently.
    """
    from upgrade_lib.compare import _ARRAY_IDENTITY_KEYS

    if isinstance(obj, dict):
        for k in obj:
            _flatten_json(obj[k], f"{prefix}/{k}", out)
    elif isinstance(obj, list):
        if obj and all(isinstance(e, dict) for e in obj):
            for i, elem in enumerate(obj):
                seg = str(i)
                for idk in _ARRAY_IDENTITY_KEYS:
                    if idk in elem and str(elem.get(idk)).strip():
                        seg = f"{idk}:{elem[idk]}"
                        break
                _flatten_json(elem, f"{prefix}/[{seg}]", out)
        else:
            for v in obj:
                out.add((f"{prefix}/[]", _vrepr(v)))
    else:
        out.add((prefix, _vrepr(obj)))


def _json_leaves(text: str) -> set[tuple[str, str]] | None:
    from upgrade_lib.compare import _normalize_for_comparison

    try:
        obj = _normalize_for_comparison(json.loads(text))
    except Exception:
        return None
    out: set[tuple[str, str]] = set()
    _flatten_json(obj, "", out)
    return out


_ARRAY_SEG_RE = re.compile(r"^(.*?/\[[^\]]+\])")


def _group_key(path: str) -> str:
    """Collapse a leaf path to its enclosing record so a wholly-dropped record
    reports once instead of once per field."""
    m = _ARRAY_SEG_RE.match(path)
    return m.group(1) if m else path


def _check_struct_file(fname, base, cust, syst, merged, leaf_fn, label) -> list[dict]:
    """Generic structural dropped-addition check for JSON/XML.

    A leaf one side added (vs baseline) that the other side did not contest, and
    whose path is entirely absent from the merged output, is flagged.
    """
    bl, cl, sl, ml = (leaf_fn(base), leaf_fn(cust), leaf_fn(syst), leaf_fn(merged))
    if bl is None or ml is None:
        return []  # unparseable — stay silent rather than guess
    cl = cl or set()
    sl = sl or set()

    merged_paths = {p for p, _ in ml}
    sys_added = (sl - bl) - cl
    cust_added = (cl - bl) - sl
    sys_paths = {p for p, _ in sys_added}
    cust_paths = {p for p, _ in cust_added}
    contested = sys_paths & cust_paths  # both sides changed same path → conflict

    findings: list[dict] = []
    seen_groups: set[tuple[str, str]] = set()

    def emit(side, path, vrepr):
        if path in contested or path in merged_paths:
            return
        g = (side, _group_key(path))
        if g in seen_groups:
            return
        seen_groups.add(g)
        val = vrepr if len(vrepr) <= 80 else vrepr[:77] + "…"
        who = "SYSTEM 26.2" if side == "SYSTEM" else "Customer"
        findings.append({
            "file": fname, "side": side, "kind": "json_path",
            "line": _group_key(path),
            "message": (
                f"{who} added {label} content at '{_group_key(path)}' but it is "
                f"missing from the merged output (value: {val})."
            ),
        })

    for p, v in sorted(sys_added):
        emit("SYSTEM", p, v)
    for p, v in sorted(cust_added):
        emit("customer", p, v)
    return findings


# --------------------------------------------------------------------------- #
# XML flattening
# --------------------------------------------------------------------------- #

def _flatten_xml(elem, prefix, idx, out: set[tuple[str, str]]) -> None:
    path = f"{prefix}/{elem.tag}[{idx}]"
    attribs = ";".join(f"{k}={v}" for k, v in sorted(elem.attrib.items()))
    text = (elem.text or "").strip()
    out.add((path, f"attrib[{attribs}] text[{text}]"))
    # group children by tag to give stable per-occurrence indices
    counts: dict[str, int] = {}
    for child in list(elem):
        i = counts.get(child.tag, 0)
        counts[child.tag] = i + 1
        _flatten_xml(child, path, i, out)


def _xml_leaves(text: str) -> set[tuple[str, str]] | None:
    try:
        root = ET.fromstring(text)
    except Exception:
        return None
    out: set[tuple[str, str]] = set()
    _flatten_xml(root, "", 0, out)
    return out


def detect_dropped_additions(
    customer_files: dict[str, str],
    system_files: dict[str, str],
    baseline_files: dict[str, str] | None,
    merged_files: dict[str, str],
) -> list[dict]:
    """Return findings for additions (method/statement for code; record/field
    for JSON/XML) that one side genuinely introduced but are missing from the
    merged output.

    Each finding: {file, side, kind, line, message}. No-ops without a baseline.
    Never raises — a per-file failure is logged and skipped.
    """
    if not baseline_files:
        return []

    findings: list[dict] = []
    for fname, merged_text in merged_files.items():
        ext = Path(fname).suffix.lower()
        base = baseline_files.get(fname)
        if base is None:
            continue
        cust = customer_files.get(fname, "")
        syst = system_files.get(fname, "")

        # Size cap — never let a huge artifact slow a merge.
        if max(len(base), len(cust), len(syst), len(merged_text)) > _MAX_BYTES:
            findings.append({
                "file": fname, "side": "—", "kind": "skipped", "line": fname,
                "message": f"customization guard skipped {fname}: file exceeds size cap.",
            })
            continue

        try:
            if ext in _CODE_EXTS:
                file_findings = _check_code_file(fname, base, cust, syst, merged_text)
            elif ext in _JSON_EXTS:
                file_findings = _check_struct_file(
                    fname, base, cust, syst, merged_text, _json_leaves, "JSON"
                )
            elif ext in _XML_EXTS:
                file_findings = _check_struct_file(
                    fname, base, cust, syst, merged_text, _xml_leaves, "XML"
                )
            else:
                continue
        except Exception as exc:  # noqa: BLE001 — guard must never break a merge
            _log.warning("[guard] check failed for %s: %s", fname, exc)
            continue

        if len(file_findings) > _MAX_FINDINGS_PER_FILE:
            extra = len(file_findings) - _MAX_FINDINGS_PER_FILE
            file_findings = file_findings[:_MAX_FINDINGS_PER_FILE]
            file_findings.append({
                "file": fname, "side": "—", "kind": "truncated", "line": fname,
                "message": f"…and {extra} more dropped-addition finding(s) in {fname}.",
            })
        findings.extend(file_findings)

    return findings
