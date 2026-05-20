"""
LessonStore — persistent storage and similarity matching for learned lessons.

Storage layout:
    lessons/
        index.json          — lightweight index for fast scanning
        <uuid>.json         — full lesson payload, one file per lesson

Writes are atomic (write to .tmp then os.replace) and thread-safe
(threading.Lock around all index mutations).

No external dependencies — only stdlib + schema.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from upgrade_lib.mcp.schema import Lesson, LessonQuery, extract_keywords

_log = logging.getLogger(__name__)

_INDEX_FILE = "index.json"
_SCORE_THRESHOLD = 40  # minimum score to return a lesson
_MAX_RESULTS = 3       # cap results returned to the agent


class LessonStore:
    """
    Thread-safe, file-backed store for agent lessons.

    Usage:
        store = LessonStore(Path("lessons/"))
        lesson = store.create_lesson(agent_name="merge", ...)
        matches = store.find_lessons(LessonQuery(...))
    """

    def __init__(self, lessons_dir: Path) -> None:
        self._dir = lessons_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index_path = self._dir / _INDEX_FILE
        if not self._index_path.exists():
            self._write_index([])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store_lesson(self, lesson: Lesson) -> Lesson:
        """Persist a lesson. Assigns id and created_at if not set."""
        if not lesson.id:
            lesson.id = uuid.uuid4().hex
        if not lesson.created_at:
            lesson.created_at = datetime.now(timezone.utc).isoformat()
        if not lesson.keywords:
            lesson.keywords = extract_keywords(
                lesson.error_message,
                lesson.artifact_category,
                lesson.file_extensions,
            )

        lesson_path = self._dir / f"{lesson.id}.json"
        self._atomic_write(lesson_path, lesson.to_dict())

        with self._lock:
            index = self._read_index()
            # Replace existing entry with same id (idempotent upsert)
            index = [e for e in index if e.get("id") != lesson.id]
            index.append(lesson.index_entry())
            self._write_index(index)

        _log.info("[mcp] Stored lesson id=%s agent=%s error=%s", lesson.id, lesson.agent_name, lesson.error_type)
        return lesson

    def find_lessons(self, query: LessonQuery) -> list[Lesson]:
        """
        Return up to MAX_RESULTS lessons matching the query.

        Phase 1: hard filter on agent_name + error_type
        Phase 2: score remaining candidates; return those above threshold
        """
        query_keywords = set(extract_keywords(
            query.error_message,
            query.artifact_category,
            query.file_extensions,
        ))

        with self._lock:
            index = self._read_index()

        # Phase 1 — hard filter
        candidates = [
            e for e in index
            if e.get("agent_name") == query.agent_name
            and e.get("error_type") == query.error_type
        ]
        if not candidates:
            return []

        # Phase 2 — score
        scored: list[tuple[int, dict]] = []
        for entry in candidates:
            score = self._score(entry, query, query_keywords)
            if score >= _SCORE_THRESHOLD:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:_MAX_RESULTS]

        # Load full lessons
        results = []
        for _, entry in top:
            lesson = self._load_lesson(entry["id"])
            if lesson:
                results.append(lesson)
        return results

    def list_lessons(
        self,
        agent_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Return (index_entries, total_count) with optional agent filter."""
        with self._lock:
            index = self._read_index()
        if agent_name:
            index = [e for e in index if e.get("agent_name") == agent_name]
        total = len(index)
        return index[offset: offset + limit], total

    def delete_lesson(self, lesson_id: str) -> bool:
        """Delete a lesson by id. Returns True if deleted."""
        lesson_path = self._dir / f"{lesson_id}.json"
        with self._lock:
            index = self._read_index()
            new_index = [e for e in index if e.get("id") != lesson_id]
            if len(new_index) == len(index):
                return False  # not found
            self._write_index(new_index)
        if lesson_path.exists():
            lesson_path.unlink(missing_ok=True)
        _log.info("[mcp] Deleted lesson id=%s", lesson_id)
        return True

    def mark_used(self, lesson_id: str) -> None:
        """Increment usage_count and update last_used on a lesson."""
        lesson = self._load_lesson(lesson_id)
        if lesson is None:
            return
        lesson.usage_count += 1
        lesson.last_used = datetime.now(timezone.utc).isoformat()
        lesson_path = self._dir / f"{lesson_id}.json"
        self._atomic_write(lesson_path, lesson.to_dict())
        with self._lock:
            index = self._read_index()
            for entry in index:
                if entry.get("id") == lesson_id:
                    entry["usage_count"] = lesson.usage_count
                    break
            self._write_index(index)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score(self, entry: dict, query: LessonQuery, query_kw: set[str]) -> int:
        score = 0

        # Artifact category match (+40) — most discriminating signal
        if entry.get("artifact_category") and entry["artifact_category"] == query.artifact_category:
            score += 40

        # File extension overlap (+20)
        entry_exts = set(entry.get("file_extensions") or [])
        query_exts = set(query.file_extensions or [])
        if entry_exts and query_exts and entry_exts & query_exts:
            score += 20

        # Keyword overlap — Jaccard * 20 (+0 to +20)
        entry_kw = set(entry.get("keywords") or [])
        if entry_kw and query_kw:
            jaccard = len(entry_kw & query_kw) / len(entry_kw | query_kw)
            score += int(jaccard * 20)

        # Prefer "fixed" lessons over "failed" ones (informational only)
        if entry.get("outcome") == "fixed":
            score += 5

        return score

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _read_index(self) -> list[dict]:
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _write_index(self, index: list[dict]) -> None:
        self._atomic_write(self._index_path, index)

    def _load_lesson(self, lesson_id: str) -> Lesson | None:
        path = self._dir / f"{lesson_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Lesson.from_dict(data)
        except Exception as exc:
            _log.warning("[mcp] Could not load lesson %s: %s", lesson_id, exc)
            return None

    def _atomic_write(self, path: Path, data: Any) -> None:
        """Write JSON atomically via a temp file."""
        tmp = path.with_suffix(".tmp.json")
        try:
            tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
