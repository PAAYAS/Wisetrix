"""Run-state file paths + JSON IO. Matches the Streamlit naming so both
UIs share the same on-disk artifacts under `run_state/`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from upgrade_api.config import RUN_STATE_DIR

# Characters that are illegal in Windows/Linux file names.
_UNSAFE_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_id(project_id: str) -> str:
    """Return a filesystem-safe version of a project ID.

    Spaces are kept (valid on all supported platforms); only truly illegal
    path characters are replaced with underscores.
    """
    return _UNSAFE_FILE_CHARS.sub("_", project_id)


def comparison_path(project_id: str) -> Path:
    return RUN_STATE_DIR / f"{_safe_id(project_id)}.comparison.json"


def merge_report_path(project_id: str) -> Path:
    return RUN_STATE_DIR / f"{_safe_id(project_id)}.merges.json"


def summary_path(project_id: str) -> Path:
    return RUN_STATE_DIR / f"{_safe_id(project_id)}.summary.json"


def risk_path(project_id: str) -> Path:
    return RUN_STATE_DIR / f"{_safe_id(project_id)}.risks.json"


def jira_tickets_path(project_id: str) -> Path:
    return RUN_STATE_DIR / f"{_safe_id(project_id)}.jira_tickets.json"


def jira_sources_path(project_id: str) -> Path:
    return RUN_STATE_DIR / f"{_safe_id(project_id)}.jira_sources.json"


def resolved_paths_path(project_id: str) -> Path:
    """Stores the most recent successfully-resolved source/target/baseline
    paths, so merge can skip the resolve providers entirely."""
    return RUN_STATE_DIR / f"{_safe_id(project_id)}.resolved.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
