"""Deterministic 3-way JSON merge — for large artifacts the LLM can't handle.

The LLM merge agent loads artifacts into context; a multi-MB JSON (e.g. a large
integration_def.json) exceeds the model's context, so it silently drops the file
or mangles it. This module merges such JSON deterministically.

Model (matches the GTM "_diff.json = customer's changes" idea):
    result = SYSTEM 26.2 base  +  (customer's deltas relative to baseline)

i.e. start from the new SYSTEM base and re-apply exactly what the customer
changed vs the old baseline. Concretely, per node (3-way: base / customer /
system):
  - customer unchanged from baseline      → take SYSTEM   (adopt the upgrade)
  - SYSTEM unchanged from baseline         → take customer (preserve customization)
  - both changed (dict)                    → recurse key-by-key
  - both changed (array of keyed records)  → merge by identity key, recursively
  - both changed (scalar / unkeyed)        → customer wins (customization stands)

With no baseline available it falls back to a 2-way overlay (SYSTEM base with
customer values layered on top), per merge_policy.md §5.

This is deterministic and size-independent. It is NOT a semantic code merge —
it only handles JSON (the LLM still does .java/.js/.jsp).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# JSON files at/above this size always use the deterministic merge — the LLM is
# unreliable on them. Smaller JSON keeps the (already-correct) LLM output.
LARGE_JSON_BYTES = 2_000_000

_MERGE_EXTS = {".json", ".xml", ".java", ".js", ".jsp"}

# Identity-key name hints, in priority order, for matching array records.
_KEY_HINTS = ("_ID", "ID", "_NAME", "NAME", "_KEY", "KEY", "CODE")


# --------------------------------------------------------------------------- #
# Equality (whitespace/ordering-insensitive enough for merge decisions)
# --------------------------------------------------------------------------- #

def _eq(a: Any, b: Any) -> bool:
    """Structural equality. Dicts compared by content, lists order-sensitively
    only as a fallback (keyed arrays are handled before this is reached)."""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return False
        return all(_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    return a == b


# --------------------------------------------------------------------------- #
# Array identity-key detection
# --------------------------------------------------------------------------- #

def _identity_key(*record_lists: list) -> str | None:
    """Find a scalar field that uniquely identifies records across the given
    arrays (so customer/system/baseline records can be matched). Returns the
    field name or None if no reliable single-key identity exists."""
    lists = [lst for lst in record_lists if lst]
    if not lists:
        return None
    for lst in lists:
        if not all(isinstance(r, dict) for r in lst):
            return None

    # candidate keys: present and scalar in EVERY record of EVERY list
    common: set[str] | None = None
    for lst in lists:
        for r in lst:
            keys = {k for k, v in r.items() if not isinstance(v, (dict, list))}
            common = keys if common is None else (common & keys)
    if not common:
        return None

    # must be unique within every list
    uniq = []
    for k in common:
        if all(len({str(r.get(k)) for r in lst}) == len(lst) for lst in lists):
            uniq.append(k)
    if not uniq:
        return None

    # prefer id/name/key-ish names, else the alphabetically-first
    for hint in _KEY_HINTS:
        for k in sorted(uniq):
            if hint in k.upper():
                return k
    return sorted(uniq)[0]


# --------------------------------------------------------------------------- #
# 3-way merge core
# --------------------------------------------------------------------------- #

def _merge(base: Any, cust: Any, syst: Any) -> Any:
    """3-way merge of one node.

    Any of `base`/`cust`/`syst` may be the sentinel _MISSING (key absent on
    that side). A return value of _MISSING means "omit this key" — the caller
    (dict builder) must skip it.
    """
    cust_m = cust is _MISSING
    syst_m = syst is _MISSING
    base_m = base is _MISSING

    # ── one side is missing the key ──────────────────────────────────────────
    if cust_m and syst_m:
        return _MISSING
    if cust_m:
        # customer doesn't have the key
        if base_m:
            return syst                      # new in SYSTEM → adopt
        if _eq(syst, base):
            return _MISSING                  # customer deleted it, SYSTEM unchanged → omit
        return syst                          # SYSTEM changed it; keep the upgrade
    if syst_m:
        # SYSTEM doesn't have the key
        if base_m:
            return cust                      # customer-new key → keep
        if _eq(cust, base):
            return _MISSING                  # SYSTEM deleted it, customer unchanged → adopt deletion
        return cust                          # customer changed it → keep customization

    # ── both present ─────────────────────────────────────────────────────────
    if not base_m and _eq(cust, base):
        return syst                          # customer unchanged vs base → adopt SYSTEM
    if not base_m and _eq(syst, base):
        return cust                          # SYSTEM unchanged vs base → keep customer

    # both changed (or no baseline)
    if isinstance(cust, dict) and isinstance(syst, dict):
        base_d = base if isinstance(base, dict) else {}
        out: dict[str, Any] = {}
        ordered = list(syst.keys()) + [k for k in cust if k not in syst]
        for k in ordered:
            res = _merge(base_d.get(k, _MISSING), cust.get(k, _MISSING), syst.get(k, _MISSING))
            if res is not _MISSING:
                out[k] = res
        return out

    if isinstance(cust, list) and isinstance(syst, list):
        base_l = base if isinstance(base, list) else []
        idk = _identity_key(cust, syst, base_l)
        if idk is not None:
            return _merge_keyed_list(base_l, cust, syst, idk)
        # unkeyed list (scalars or no identity): customer's list wins
        return cust

    # scalar (or type mismatch) conflict → customer wins
    return cust


class _Missing:
    def __repr__(self):  # pragma: no cover
        return "_MISSING"


_MISSING = _Missing()


def _merge_keyed_list(base_l: list, cust_l: list, syst_l: list, idk: str) -> list:
    """3-way merge of arrays of records keyed by `idk`.

    SYSTEM records first (in order), with the customer's per-record changes
    applied; customer-added records appended; customer-deleted records dropped.
    """
    def index(lst):
        return {str(r.get(idk)): r for r in lst if isinstance(r, dict)}

    base_i, cust_i, syst_i = index(base_l), index(cust_l), index(syst_l)
    out: list = []
    seen: set[str] = set()

    for r in syst_l:
        if not isinstance(r, dict):
            out.append(r)
            continue
        kv = str(r.get(idk))
        seen.add(kv)
        in_base = kv in base_i
        in_cust = kv in cust_i
        if in_cust:
            # present on both sides → merge the record 3-way
            out.append(_merge(base_i.get(kv, _MISSING), cust_i[kv], r))
        elif in_base and not in_cust:
            # customer deleted a baseline record → respect deletion (drop it)
            continue
        else:
            # SYSTEM-only record (new in upgrade, customer never had it) → keep
            out.append(r)

    # customer-added records (not in SYSTEM) — append in customer order
    for r in cust_l:
        if not isinstance(r, dict):
            continue
        kv = str(r.get(idk))
        if kv in seen:
            continue
        # only append if it's genuinely a customer addition (not a baseline
        # record SYSTEM removed — those stay removed)
        if kv in base_i and kv not in syst_i:
            continue
        out.append(r)
        seen.add(kv)

    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def merge_json_3way(customer: str, system: str, baseline: str | None) -> str:
    """Merge a single JSON file's content. Returns the merged JSON string."""
    cust = json.loads(customer)
    syst = json.loads(system)
    base = json.loads(baseline) if baseline else _MISSING
    merged = _merge(base, cust, syst)
    if merged is _MISSING:  # defensive — never serialize the sentinel
        merged = cust
    return json.dumps(merged, indent=3, ensure_ascii=False)


def deterministic_fill(
    artifact_files: dict[str, str],
    customer_files: dict[str, str],
    system_files: dict[str, str],
    baseline_files: dict[str, str] | None,
) -> tuple[dict[str, str], list[str]]:
    """Backstop the LLM merge: deterministically produce any mergeable file the
    LLM failed to output, and override large JSON files (which the LLM can't
    handle reliably).

    Returns (updated_artifact_files, list_of_filenames_filled_deterministically).
    """
    baseline_files = baseline_files or {}
    out = dict(artifact_files)
    filled: list[str] = []

    expected = {
        f for f in (set(customer_files) | set(system_files))
        if Path(f).suffix.lower() in _MERGE_EXTS
    }

    for fname in sorted(expected):
        ext = Path(fname).suffix.lower()
        cust = customer_files.get(fname)
        syst = system_files.get(fname)
        base = baseline_files.get(fname)

        is_json = ext == ".json"
        big_json = is_json and (
            len(cust or "") >= LARGE_JSON_BYTES or len(syst or "") >= LARGE_JSON_BYTES
        )
        missing = fname not in out

        if not (missing or big_json):
            continue

        try:
            if is_json and cust is not None and syst is not None:
                out[fname] = merge_json_3way(cust, syst, base)
                filled.append(fname)
            elif missing:
                # non-JSON the LLM dropped, or only-one-side file: prefer the
                # customer's version (preserve customization); else SYSTEM's.
                out[fname] = cust if cust is not None else syst  # type: ignore[assignment]
                filled.append(fname)
        except Exception as exc:  # noqa: BLE001 — never break the merge
            _log.warning("[json_merge] deterministic fill failed for %s: %s", fname, exc)

    return out, filled
