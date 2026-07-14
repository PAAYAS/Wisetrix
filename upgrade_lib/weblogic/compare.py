"""WebLogic forced-decision results.

Some WebLogic artifacts bypass the 3-way compare entirely because their
decision is fixed by a routing rule:

    Retain + copy-as-is   app-extensions/src (rule 5),
                          _ENV_SPECIFIC/.../integration_def_config (rule 4)
    Remove                _plugindef (rule 6)

``forced_result`` produces a result dict in the exact shape
``compare_artifact_local`` returns, so downstream (risk scoring, rollup,
report) treats these artifacts uniformly.
"""

from __future__ import annotations

from pathlib import Path

from upgrade_lib.compare import EXCLUDE_NAMES, INCLUDE_EXTS
from upgrade_lib.weblogic.layout import IGNORE_SUFFIXES


def forced_result(
    decision: str,
    source_dir,
    *,
    copy_as_is: bool = False,
    note: str | None = None,
) -> dict:
    """Build a compare-result dict for an artifact whose decision is forced.

    When ``copy_as_is`` is True the whole source tree is enumerated (recursively)
    so the file list reflects everything that will be copied verbatim; otherwise
    only the top-level comparable files are listed (mirrors the retain/remove
    fast-paths in ``compare_artifact_local``).
    """
    source_dir = Path(source_dir)
    file_decisions: dict[str, str] = {}
    file_details: list[dict] = []

    if source_dir.is_dir():
        walker = source_dir.rglob("*") if copy_as_is else source_dir.iterdir()
        for f in sorted(walker):
            if not f.is_file():
                continue
            if f.name in EXCLUDE_NAMES:
                continue
            suffix = f.suffix.lower()
            if suffix in IGNORE_SUFFIXES or suffix not in INCLUDE_EXTS:
                continue
            key = f.relative_to(source_dir).as_posix() if copy_as_is else f.name
            file_decisions[key] = decision
            file_details.append(
                {"file": key, "decision": decision, "target_exists": False}
            )

    if note is None:
        note = f"Forced {decision}" + (" — copied as-is" if copy_as_is else "")

    return {
        "decision": decision,
        "file_decisions": file_decisions,
        "file_details": file_details,
        "target_exists": False,
        "target_path": "",
        "file_count": len(file_decisions),
        "analysis": note,
        "engine": "local-weblogic",
        "base_redirect": None,
        "no_customer_content_note": None,
        "baseline_unchanged_note": None,
    }
