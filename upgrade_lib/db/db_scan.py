"""Derive DB actions from the app comparison results.

The DB side does NOT run its own comparison. For every `bizpolicydefs`
artifact in the app comparison, the app's decision dictates the DB action:

    Remove  → tell the user to remove the bppol xlsx from the DB git check-in
    Retain  → keep the DB xlsx as-is (no change)
    Merge   → reconcile the merged bizpolicydef.json into a new bppol xlsx

This module only *derives* the action list; the actual xlsx reconciliation is
performed by upgrade_lib.db.xlsx_merge when a Merge action is executed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from upgrade_lib.db.paths import find_bppol_file

# The app category whose data also lives in the DB seed-data repo.
DB_CATEGORY = "bizpolicydefs"


def _action_for_decision(decision: str, workbook_name: str | None) -> tuple[str, str]:
    """Map an app decision (+ whether the workbook exists) to a coherent
    (action_type, human message)."""
    if decision == "Remove":
        if workbook_name:
            return "remove", (
                f"Remove '{workbook_name}' from the DB git check-in — the "
                f"artifact is taken from the 26.2 core."
            )
        return "remove_absent", (
            "No DB workbook present for this policy — nothing to remove."
        )
    if decision == "Retain":
        if workbook_name:
            return "retain", "Keep the DB workbook as-is — no change needed."
        return "retain", (
            "Decision is Retain but no DB workbook was found for this policy."
        )
    if decision == "Merge":
        if workbook_name:
            return "merge", "Reconcile the merged policy into a new workbook."
        return "merge", (
            "Merge decision, but no DB workbook found to reconcile into."
        )
    return "skip", f"No DB action for decision '{decision}'."


def derive_db_actions(
    comparison: dict[str, Any],
    db_source_root: Path | str | None,
    merge_report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the DB action list from the app comparison.

    Args:
        comparison:     parsed {id}.comparison.json (app decisions).
        db_source_root: resolved .../data dir of the DB repo (may be None/empty
                        if the repo isn't resolved yet — bppol existence is then
                        reported as unknown).
        merge_report:   parsed {id}.merges.json so we can tell whether a
                        Merge-decision policy has been merged on the app side.

    Returns a list of action records (one per bizpolicydefs artifact).
    """
    merge_report = merge_report or {}
    root = Path(db_source_root) if db_source_root else None

    actions: list[dict[str, Any]] = []
    for key, entry in comparison.items():
        if entry.get("category") != DB_CATEGORY:
            continue

        org = entry.get("bucket") or ""
        name = entry.get("name") or Path(entry.get("rel_path", key)).name
        decision = entry.get("decision", "")

        matched = find_bppol_file(root, org, name) if root else None
        workbook_name = matched.name if matched else None
        bppol_exists = matched is not None

        action_type, message = _action_for_decision(decision, workbook_name)

        # For Merge: the DB reconcile needs the app's merged bizpolicydef.json.
        app_merged = key in merge_report
        ready = action_type != "merge" or (app_merged and bppol_exists)

        actions.append({
            "key": key,
            "bucket": org,
            "name": name,
            "rel_path": entry.get("rel_path", ""),
            "app_decision": decision,
            "action_type": action_type,
            "message": message,
            "workbook_name": workbook_name or "",
            "bppol_path": str(matched) if matched else "",
            "bppol_exists": bppol_exists,
            "app_merged": app_merged,
            "ready": ready,
            "status": "pending" if action_type == "merge" else "n/a",
        })

    return actions
