"""FastAPI entry point for the upgrade tool.

Run locally (dev):
    uvicorn upgrade_api.main:app --reload --port 8000

This service is a thin shim around `upgrade_lib` — all business logic stays
in the existing library, so the Streamlit UI and this API stay in lock-step.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make project root importable so `upgrade_lib` resolves cleanly when the
# service is started via `uvicorn upgrade_api.main:app`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from upgrade_api.config import cors_origins  # noqa: E402
from upgrade_api.routers import (  # noqa: E402
    diff,
    health,
    jira,
    merges,
    projects,
    providers,
    scan,
    summary,
)


app = FastAPI(
    title="upgrade-api",
    description=(
        "Backend service for the GTM Docker-to-Docker upgrade tool. "
        "Wraps the `upgrade_lib` agents and providers for the Next.js UI."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(projects.router)
app.include_router(providers.router)
app.include_router(scan.router)
app.include_router(merges.router)
app.include_router(summary.router)
app.include_router(diff.router)
app.include_router(jira.router)
