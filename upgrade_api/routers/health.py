"""Health & metadata endpoints — used by the Next.js UI to verify the API is up."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "upgrade-api",
        "time": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
    }


@router.get("/")
def root() -> dict:
    return {
        "service": "upgrade-api",
        "docs": "/docs",
        "health": "/health",
    }
