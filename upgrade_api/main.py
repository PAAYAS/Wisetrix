"""FastAPI entry point for the upgrade tool.

Run locally (dev):
    uvicorn upgrade_api.main:app --reload --port 8000

This service is a thin shim around `upgrade_lib` — all business logic stays
in the existing library, so the Streamlit UI and this API stay in lock-step.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# Make project root importable so `upgrade_lib` resolves cleanly when the
# service is started via `uvicorn upgrade_api.main:app`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Configure logging before importing anything that uses the root logger.
from upgrade_api.logging_config import setup_logging  # noqa: E402
setup_logging()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from upgrade_api.config import cors_origins  # noqa: E402
from upgrade_api.routers import (  # noqa: E402
    db,
    diff,
    health,
    jira,
    merges,
    projects,
    providers,
    scan,
    summary,
)
from upgrade_lib.mcp.server import get_mcp_router  # noqa: E402

logger = logging.getLogger("upgrade_api.access")


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


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request + response. Unhandled exceptions are logged as ERROR."""
    start = time.perf_counter()
    logger.info("→ %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        logger.info(
            "← %s %s %d (%.2fs)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response
    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.error(
            "✗ %s %s UNHANDLED EXCEPTION (%.2fs): %s",
            request.method,
            request.url.path,
            elapsed,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal server error: {exc}"},
        )


# ---------------------------------------------------------------------------
# Frontend log relay — the Next.js UI posts client-side errors here so they
# end up in the same wisetrix.log file as backend errors.
# ---------------------------------------------------------------------------
_fe_logger = logging.getLogger("upgrade_api.frontend")


@app.post("/log", include_in_schema=False)
async def frontend_log(request: Request):
    """Accept structured log records from the Next.js frontend."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)

    level = str(body.get("level", "error")).lower()
    message = body.get("message", "(no message)")
    context = body.get("context", {})

    log_fn = {
        "debug": _fe_logger.debug,
        "info": _fe_logger.info,
        "warn": _fe_logger.warning,
        "warning": _fe_logger.warning,
        "error": _fe_logger.error,
    }.get(level, _fe_logger.error)

    log_fn("[FE] %s | context=%s", message, context)
    return JSONResponse({"ok": True})


app.include_router(health.router)
app.include_router(projects.router)
app.include_router(providers.router)
app.include_router(scan.router)
app.include_router(merges.router)
app.include_router(summary.router)
app.include_router(diff.router)
app.include_router(jira.router)
app.include_router(db.router)
app.include_router(get_mcp_router(), prefix="/mcp", tags=["learning"])
