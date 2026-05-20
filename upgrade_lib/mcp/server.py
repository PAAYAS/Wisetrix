"""
MCP Learning Server — FastAPI router mounted at /mcp in the main API.

Exposes four tools:
  POST /mcp/store_lesson   — save a new lesson
  POST /mcp/find_lesson    — find matching lessons for an error
  GET  /mcp/list_lessons   — browse all stored lessons
  DELETE /mcp/lessons/{id} — remove a specific lesson
  GET  /mcp/health         — liveness check

The LessonStore singleton is created once per process and shared across requests.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from upgrade_lib.mcp.lesson_store import LessonStore
from upgrade_lib.mcp.schema import Lesson, LessonQuery


# ---------------------------------------------------------------------------
# Pydantic request/response models (FastAPI validation layer)
# ---------------------------------------------------------------------------

class StoreLessonRequest(BaseModel):
    agent_name: str
    error_type: str
    error_message: str
    file_extensions: list[str] = []
    artifact_category: str = ""
    prompt_size_band: str = "small"
    has_baseline: bool = False
    retry_attempt: int = 1
    project_id: str = ""
    outcome: str                    # "fixed" | "failed"
    fix_description: str
    prompt_injection: str


class FindLessonRequest(BaseModel):
    agent_name: str
    error_type: str
    error_message: str = ""
    artifact_category: str = ""
    file_extensions: list[str] = []
    has_baseline: bool = False
    prompt_size_band: str = "small"


# ---------------------------------------------------------------------------
# Router factory — called once from upgrade_api/main.py
# ---------------------------------------------------------------------------

def get_mcp_router(lessons_dir: Path | None = None) -> APIRouter:
    """Create and return the MCP learning router with an embedded LessonStore."""
    from upgrade_api.config import PROJECT_ROOT
    _lessons_dir = lessons_dir or (PROJECT_ROOT / "lessons")
    store = LessonStore(_lessons_dir)
    router = APIRouter()

    @router.get("/health")
    def mcp_health():
        _, total = store.list_lessons(limit=0)
        return {
            "status": "ok",
            "service": "mcp-learning-server",
            "lessons_stored": total,
            "lessons_dir": str(_lessons_dir),
        }

    @router.post("/store_lesson")
    def store_lesson(req: StoreLessonRequest):
        """Store a new lesson from an agent run (error or retry success)."""
        lesson = Lesson(
            id="",
            created_at="",
            agent_name=req.agent_name,
            error_type=req.error_type,
            error_message=req.error_message[:500],
            file_extensions=req.file_extensions,
            artifact_category=req.artifact_category,
            prompt_size_band=req.prompt_size_band,
            has_baseline=req.has_baseline,
            retry_attempt=req.retry_attempt,
            project_id=req.project_id,
            outcome=req.outcome,
            fix_description=req.fix_description,
            prompt_injection=req.prompt_injection[:800],
        )
        saved = store.store_lesson(lesson)
        return {"id": saved.id, "stored": True}

    @router.post("/find_lesson")
    def find_lesson(req: FindLessonRequest):
        """Find lessons matching an error context. Returns up to 3 matches."""
        query = LessonQuery(
            agent_name=req.agent_name,
            error_type=req.error_type,
            error_message=req.error_message,
            artifact_category=req.artifact_category,
            file_extensions=req.file_extensions,
            has_baseline=req.has_baseline,
            prompt_size_band=req.prompt_size_band,
        )
        lessons = store.find_lessons(query)
        for lesson in lessons:
            store.mark_used(lesson.id)
        return {
            "lessons": [l.to_dict() for l in lessons],
            "count": len(lessons),
        }

    @router.get("/list_lessons")
    def list_lessons(
        agent: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        """List stored lessons with optional agent filter."""
        entries, total = store.list_lessons(agent_name=agent, limit=limit, offset=offset)
        return {"lessons": entries, "total": total, "limit": limit, "offset": offset}

    @router.delete("/lessons/{lesson_id}")
    def delete_lesson(lesson_id: str):
        """Remove a lesson by id."""
        deleted = store.delete_lesson(lesson_id)
        if not deleted:
            raise HTTPException(404, f"Lesson '{lesson_id}' not found")
        return {"deleted": True, "id": lesson_id}

    return router
