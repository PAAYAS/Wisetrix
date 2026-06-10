"""Deterministic reconciliation of a merged bizpolicydef.json into a bppol xlsx.

This is pure Python (openpyxl). NO Claude. The semantic merge already happened
on the app side (the merged bizpolicydef.json is the source of truth). Here we
only project that JSON's record arrays into the matching data tabs of the
customer's exported seed-data workbook, appending rows that are missing.

Design goals (per the GTM expert's instructions):
  - "Keep the other tabs and values as is" → load the existing workbook and only
    APPEND rows to the relevant data tab(s); never rewrite other sheets.
  - Tab names vary per export (timestamp aliases like data.TBL_1758881963614.1)
    and per project, so the target tab is discovered dynamically by matching the
    JSON record's field names against each data tab's header row.
  - Surrogate keys:
      * alt_key_policy  → constant across rows → copied from existing rows.
      * alt_key_instance / alt_key_columns / alt_key_* that vary per row →
        generated as max(existing numeric values) + 1, incrementing per new row.
  - Additive only: existing rows are never modified or deleted.

Entry point: merge_policy_into_workbook().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Columns that identify a record within a tab (normalised, lower-case). When a
# data tab's headers intersect this set, those shared columns form the identity
# key used to decide whether a JSON record already exists as a row.
#
# IMPORTANT: sequence numbers (exec_seq, row_seq) are intentionally EXCLUDED.
# The app merge renumbers exec_seq when it inserts/reorders rules, so a rule
# present in both sides would get a *different* exec_seq and be wrongly treated
# as new — producing duplicate rows. Identity must be the stable business key
# (e.g. instance_id + rule_id). Sequence columns are still written verbatim as
# direct fields on genuinely-new rows.
_KEY_CANDIDATES: frozenset[str] = frozenset({
    "instance_id",
    "rule_id",
    "status_column",
    "name",
    "locale",
    "group_name",
    "field_name",
})

# Sequence columns that must never participate in identity matching.
_SEQUENCE_COLS: frozenset[str] = frozenset({"exec_seq", "row_seq"})

# Audit columns inherited from a sibling row (not present in JSON records).
_AUDIT_COLS: frozenset[str] = frozenset({
    "created_by",
    "created_date",
    "last_modified_by",
    "last_modified_date",
})

# Columns that are constant for all rows of a single policy. Always treated as
# constant even when a tab has only one existing row (so detection can't infer
# it from the data alone).
_CONSTANT_CANDIDATES: frozenset[str] = frozenset({
    "org_code",
    "policy_id",
    "alt_key_policy",
})

# Minimum header/field overlap for a tab to be considered a routing match.
_MIN_OVERLAP = 2


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

@dataclass
class TabResult:
    sheet: str
    json_key: str
    identity_cols: list[str]
    matched: int = 0          # JSON records already present as rows
    added: int = 0            # rows appended
    added_alt_keys: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class MergeResult:
    source_xlsx: str
    output_xlsx: str
    tabs: list[TabResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_added(self) -> int:
        return sum(t.added for t in self.tabs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_xlsx": self.source_xlsx,
            "output_xlsx": self.output_xlsx,
            "total_added": self.total_added,
            "tabs": [
                {
                    "sheet": t.sheet,
                    "json_key": t.json_key,
                    "identity_cols": t.identity_cols,
                    "matched": t.matched,
                    "added": t.added,
                    "added_alt_keys": t.added_alt_keys,
                }
                for t in self.tabs
            ],
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _norm(name: Any) -> str:
    """Normalise a column header or JSON key for matching."""
    return str(name).strip().lower() if name is not None else ""


def _norm_val(v: Any) -> str:
    """Normalise a cell/JSON value for equality comparison.

    Numbers compare by integer value when integral (45 == 45.0 == "45"); other
    values compare as trimmed strings.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    s = str(v).strip()
    # numeric-looking string → normalise too
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else str(f)
    except (ValueError, TypeError):
        return s


def _coerce_for_write(norm_col: str, value: Any) -> Any:
    """Coerce a JSON value to the cell type expected for a column."""
    if value is None:
        return None
    # exec_seq / *_seq are integers in the sheet
    if norm_col.endswith("_seq") and isinstance(value, (int, float)):
        f = float(value)
        return int(f) if f.is_integer() else f
    return value


def _header_map(ws) -> dict[str, int]:
    """Map normalised header name → 1-based column index from row 1."""
    out: dict[str, int] = {}
    for col_idx, cell in enumerate(ws[1], start=1):
        n = _norm(cell.value)
        if n:
            out[n] = col_idx
    return out


def _data_rows(ws) -> list[dict[str, Any]]:
    """Return existing data rows (below header) as {norm_col: value} dicts."""
    headers = _header_map(ws)
    inv = {idx: name for name, idx in headers.items()}
    rows: list[dict[str, Any]] = []
    for row in ws.iter_rows(min_row=2):
        values = {inv[c.column]: c.value for c in row if c.column in inv}
        if any(v is not None and str(v).strip() != "" for v in values.values()):
            rows.append(values)
    return rows


def _is_data_sheet(title: str) -> bool:
    return title.startswith("data.")


# --------------------------------------------------------------------------- #
# Routing: JSON record array → workbook data tab
# --------------------------------------------------------------------------- #

def _candidate_arrays(policy: dict) -> list[tuple[str, list[dict]]]:
    """Top-level JSON keys whose value is a non-empty list of dict records."""
    out = []
    for key, value in policy.items():
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
        ):
            out.append((key, value))
    return out


def _entry_field_names(records: list[dict]) -> set[str]:
    """Union of normalised field names across records (skip nested arrays)."""
    names: set[str] = set()
    for rec in records:
        for k, v in rec.items():
            # nested record arrays (dependency fields/rules) are handled within
            # their own tabs, not as columns here
            if isinstance(v, list):
                continue
            names.add(_norm(k))
    return names


def _route_array(field_names: set[str], data_sheets: list) -> tuple[Any, set[str]] | None:
    """Pick the best-matching data sheet for a set of record field names.

    Returns (worksheet, overlap_cols) or None if no sheet meets the threshold.
    """
    best = None
    best_overlap: set[str] = set()
    for ws in data_sheets:
        headers = set(_header_map(ws).keys())
        if not headers:
            continue
        overlap = field_names & headers
        # require at least one identity-candidate column in the overlap
        if len(overlap) >= _MIN_OVERLAP and (overlap & _KEY_CANDIDATES):
            if len(overlap) > len(best_overlap):
                best, best_overlap = ws, overlap
    return (best, best_overlap) if best is not None else None


# --------------------------------------------------------------------------- #
# Column classification for new-row construction
# --------------------------------------------------------------------------- #

def _constant_columns(rows: list[dict[str, Any]], headers: dict[str, int]) -> dict[str, Any]:
    """Columns whose non-empty value is identical across all existing rows.

    Returns {norm_col: constant_value}. Always includes _CONSTANT_CANDIDATES if
    a value can be found in any row.
    """
    consts: dict[str, Any] = {}
    for col in headers:
        seen = [r.get(col) for r in rows if r.get(col) not in (None, "")]
        distinct = {_norm_val(v) for v in seen}
        if col in _CONSTANT_CANDIDATES and seen:
            consts[col] = seen[0]
        elif len(rows) > 1 and len(distinct) == 1 and seen:
            consts[col] = seen[0]
    return consts


def _is_surrogate_varying(col: str, rows: list[dict[str, Any]], consts: dict[str, Any]) -> bool:
    """alt_key_* column that varies per row (a generated surrogate key)."""
    if not col.startswith("alt_key"):
        return False
    if col in consts:
        return False
    return True


def _next_surrogate(rows: list[dict[str, Any]], col: str) -> int:
    """max(existing numeric values in col) + 1; 1 if none present."""
    vals: list[int] = []
    for r in rows:
        v = r.get(col)
        if v is None or str(v).strip() == "":
            continue
        try:
            vals.append(int(float(v)))
        except (ValueError, TypeError):
            continue
    return (max(vals) + 1) if vals else 1


# --------------------------------------------------------------------------- #
# Reconcile one array into one tab
# --------------------------------------------------------------------------- #

def _reconcile_tab(ws, json_key: str, records: list[dict], overlap: set[str]) -> TabResult:
    headers = _header_map(ws)           # norm_col -> col index
    rows = _data_rows(ws)
    consts = _constant_columns(rows, headers)

    # direct-field columns: header cols that appear as record fields
    field_names = _entry_field_names(records)
    direct_cols = {c for c in headers if c in field_names}

    # identity columns: shared key candidates (fallback: direct cols minus
    # volatile sequence columns, which the merge renumbers).
    identity = sorted((overlap & _KEY_CANDIDATES)) or sorted(
        direct_cols - _SEQUENCE_COLS
    )

    result = TabResult(
        sheet=ws.title, json_key=json_key, identity_cols=list(identity)
    )

    # existing identity signatures
    def sig_from_row(r: dict[str, Any]) -> tuple:
        return tuple(_norm_val(r.get(c)) for c in identity)

    existing_sigs = {sig_from_row(r) for r in rows}

    # surrogate columns and their running next-value
    surrogate_cols = [
        c for c in headers if _is_surrogate_varying(c, rows, consts)
    ]
    next_surrogate = {c: _next_surrogate(rows, c) for c in surrogate_cols}

    template = rows[0] if rows else {}

    for rec in records:
        rec_norm = {_norm(k): v for k, v in rec.items() if not isinstance(v, list)}
        sig = tuple(_norm_val(rec_norm.get(c)) for c in identity)
        if sig in existing_sigs:
            result.matched += 1
            continue

        # Build the new row, column by column (1-based, full width)
        max_col = ws.max_column
        new_cells: list[Any] = [None] * max_col
        for col, idx in headers.items():
            if col in surrogate_cols:
                val = next_surrogate[col]
                next_surrogate[col] += 1
                result.added_alt_keys.setdefault(col, []).append(int(val))
            elif col in consts:
                val = consts[col]
            elif col in _AUDIT_COLS:
                val = template.get(col)
            elif col in direct_cols:
                val = _coerce_for_write(col, rec_norm.get(col))
            else:
                val = None
            new_cells[idx - 1] = val

        ws.append(new_cells)
        existing_sigs.add(sig)
        result.added += 1

    return result


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def merge_policy_into_workbook(
    merged_policy: dict,
    source_xlsx: Path | str,
    output_xlsx: Path | str,
) -> MergeResult:
    """Reconcile a merged bizpolicydef.json into a copy of the bppol workbook.

    Args:
        merged_policy: parsed merged bizpolicydef.json (the app's merged output).
        source_xlsx:   the customer's existing bppol.<ORG>.<NAME>.xlsx.
        output_xlsx:   where to write the new workbook (dirs created as needed).

    Returns a MergeResult describing what was appended per tab.

    The workbook is loaded and saved with openpyxl, so every other tab, cell,
    style, and value is preserved; only the matched data tab(s) gain rows.
    """
    source_xlsx = Path(source_xlsx)
    output_xlsx = Path(output_xlsx)

    wb = openpyxl.load_workbook(source_xlsx)
    result = MergeResult(source_xlsx=str(source_xlsx), output_xlsx=str(output_xlsx))

    data_sheets = [ws for ws in wb.worksheets if _is_data_sheet(ws.title)]
    if not data_sheets:
        result.warnings.append("No data.* tabs found in workbook.")

    arrays = _candidate_arrays(merged_policy)
    if not arrays:
        result.warnings.append("No record arrays found in merged policy JSON.")

    routed_sheets: set[str] = set()
    for json_key, records in arrays:
        field_names = _entry_field_names(records)
        routed = _route_array(field_names, data_sheets)
        if routed is None:
            result.warnings.append(
                f"No matching data tab for JSON array '{json_key}' "
                f"(fields: {sorted(field_names)[:6]}…) — skipped."
            )
            continue
        ws, overlap = routed
        if ws.title in routed_sheets:
            result.warnings.append(
                f"Tab '{ws.title}' matched more than one JSON array; "
                f"'{json_key}' skipped to avoid double-write."
            )
            continue
        routed_sheets.add(ws.title)
        result.tabs.append(_reconcile_tab(ws, json_key, records, overlap))

    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_xlsx)
    _log.info(
        "[db-merge] %s → %s : +%d rows across %d tab(s)",
        source_xlsx.name, output_xlsx.name, result.total_added, len(result.tabs),
    )
    return result
