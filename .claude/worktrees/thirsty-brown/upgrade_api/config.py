"""Centralized config & paths for the FastAPI service.

State files (projects.json, run_state/*.json, output/) are shared with the
Streamlit UI — both UIs read/write the same on-disk artifacts so you can
switch between them without any migration.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROJECTS_FILE = PROJECT_ROOT / "projects.json"
RUN_STATE_DIR = PROJECT_ROOT / "run_state"
OUTPUT_DIR = PROJECT_ROOT / "output"

RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# CORS — Next.js dev server (3000) by default; comma-separated env override.
def cors_origins() -> list[str]:
    raw = os.environ.get(
        "UPGRADE_API_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]
