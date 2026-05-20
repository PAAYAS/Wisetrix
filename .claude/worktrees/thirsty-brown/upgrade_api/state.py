"""On-disk state helpers — read/write `projects.json` shared with Streamlit.

The schema is exactly what the Streamlit app writes today, so both UIs
remain interoperable.
"""

from __future__ import annotations

import json
from typing import Any

from upgrade_api.config import PROJECTS_FILE


def load_projects() -> dict[str, dict[str, Any]]:
    if not PROJECTS_FILE.exists():
        return {}
    try:
        with open(PROJECTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_projects(projects: dict[str, dict[str, Any]]) -> None:
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2, sort_keys=True)
