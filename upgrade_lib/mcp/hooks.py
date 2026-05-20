"""
LearningMixin — hooks injected into BaseAgent._call() for the MCP learning server.

Two behaviours:
  1. BEFORE a Claude call: query the MCP server for matching lessons and
     prepend any prompt_injection text to the system prompt.

  2. AFTER a Claude call:
     - On success after retry (attempt > 1): store a "fixed" lesson
     - On final failure (all retries exhausted): store a "failed" lesson

The MCP server is contacted over HTTP on localhost. If it is unreachable,
the mixin logs a warning and continues — the primary workflow is never blocked.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import requests

from upgrade_lib.mcp.schema import LessonQuery, prompt_size_band

_log = logging.getLogger(__name__)

# Timeout for MCP HTTP calls — must be short so a dead server doesn't slow merges
_MCP_TIMEOUT = 0.8   # seconds

_MCP_BASE_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")

# Instruction header prepended when injecting lessons into the system prompt
_INJECTION_HEADER = "\n\n## Learning System — Known Issues\n"
_INJECTION_FOOTER = "\nApply the above lessons if you encounter similar situations.\n"


def _post(path: str, body: dict) -> dict | None:
    """POST to MCP server; return JSON or None on failure."""
    try:
        resp = requests.post(
            f"{_MCP_BASE_URL}{path}",
            json=body,
            timeout=_MCP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        _log.debug("[mcp] HTTP call failed (%s): %s", path, exc)
        return None


def _infer_context(
    prompt: str,
    cwd: str | None,
    has_tools: bool,
    estimated_file_count: int,
) -> dict[str, Any]:
    """
    Infer artifact context from what BaseAgent._call() already knows.
    Returns a dict with keys used by LessonQuery and StoreLessonRequest.
    """
    # File extensions — scan cwd/customer or cwd/system if present
    extensions: list[str] = []
    category = ""
    if cwd:
        for sub in ("customer", "system"):
            sub_dir = Path(cwd) / sub
            if sub_dir.exists():
                for p in sub_dir.rglob("*"):
                    if p.is_file() and p.suffix and p.suffix not in extensions:
                        extensions.append(p.suffix.lower())
        # Artifact category is the parent dir name of the workdir
        # e.g. workdir = /tmp/merge_abc123/  rel_path = actions/myartifact
        # The agent cwd is the temp dir itself; the category hint is in the prompt
        cat_match = re.search(r"bucket.*?[\"']([\w]+)[\"']", prompt[:500], re.IGNORECASE)
        if cat_match:
            category = cat_match.group(1).lower()

    return {
        "file_extensions": extensions[:10],
        "artifact_category": category,
        "prompt_size_band": prompt_size_band(len(prompt)),
        "has_tools": has_tools,
        "estimated_file_count": estimated_file_count,
    }


class LearningMixin:
    """
    Mixin for BaseAgent — adds lesson lookup and storage around Claude calls.

    BaseAgent already defines:
        self.agent_name: str
        self._router: ClaudeRouter | None
        self.model: str
    This mixin adds no new instance state.
    """

    # ------------------------------------------------------------------
    # Lookup (called BEFORE the Claude subprocess)
    # ------------------------------------------------------------------

    def _lookup_lessons(
        self,
        system_prompt: str,
        prompt: str,
        cwd: str | None,
        has_tools: bool,
        estimated_file_count: int,
    ) -> str:
        """
        Query MCP for known lessons. Returns (possibly augmented) system_prompt.

        If matching lessons are found, their prompt_injection text is appended
        to the system prompt so Claude is aware of known issues before it starts.
        """
        ctx = _infer_context(prompt, cwd, has_tools, estimated_file_count)

        # We query for the most common failure types on first call.
        # If no matching error_type found yet, we skip — no injection needed.
        # The lookup is best-effort: return original system_prompt on any failure.
        injections: list[str] = []

        for error_type in ("RuntimeError", "TimeoutError", "empty_merged_files", "ValueError"):
            result = _post("/find_lesson", {
                "agent_name": self.agent_name,  # type: ignore[attr-defined]
                "error_type": error_type,
                "error_message": "",
                "artifact_category": ctx["artifact_category"],
                "file_extensions": ctx["file_extensions"],
                "has_baseline": False,
                "prompt_size_band": ctx["prompt_size_band"],
            })
            if result and result.get("count", 0) > 0:
                for lesson in result["lessons"]:
                    inj = lesson.get("prompt_injection", "").strip()
                    if inj:
                        injections.append(f"- [{lesson['error_type']}] {inj}")

        if not injections:
            return system_prompt

        _log.info("[mcp] Injecting %d lesson(s) into system prompt for agent=%s",
                  len(injections), self.agent_name)  # type: ignore[attr-defined]
        injection_block = (
            _INJECTION_HEADER
            + "\n".join(injections)
            + _INJECTION_FOOTER
        )
        return system_prompt + injection_block

    # ------------------------------------------------------------------
    # Storage (called AFTER the Claude subprocess)
    # ------------------------------------------------------------------

    def _store_retry_success(
        self,
        attempt: int,
        prompt: str,
        cwd: str | None,
        has_tools: bool,
        estimated_file_count: int,
        project_id: str = "",
    ) -> None:
        """
        Store a lesson when a call succeeded on retry (attempt > 1).
        The fix is: 'retry resolved the issue'.
        """
        ctx = _infer_context(prompt, cwd, has_tools, estimated_file_count)
        injection = (
            f"This agent previously failed on attempt 1 and succeeded on attempt {attempt}. "
            f"If you encounter errors on first run, proceeding conservatively on retry is effective. "
            f"Context: {ctx['prompt_size_band']} prompt, "
            f"extensions: {', '.join(ctx['file_extensions']) or 'unknown'}."
        )
        _post("/store_lesson", {
            "agent_name": self.agent_name,  # type: ignore[attr-defined]
            "error_type": "success_after_retry",
            "error_message": f"Succeeded on attempt {attempt}",
            "file_extensions": ctx["file_extensions"],
            "artifact_category": ctx["artifact_category"],
            "prompt_size_band": ctx["prompt_size_band"],
            "has_baseline": False,
            "retry_attempt": attempt,
            "project_id": project_id,
            "outcome": "fixed",
            "fix_description": f"Retry succeeded on attempt {attempt}",
            "prompt_injection": injection,
        })

    def _store_failure(
        self,
        exc: Exception,
        attempt: int,
        prompt: str,
        cwd: str | None,
        has_tools: bool,
        estimated_file_count: int,
        project_id: str = "",
    ) -> None:
        """
        Store a lesson when all retries are exhausted.
        The injection warns future calls about this failure pattern.
        """
        error_type = type(exc).__name__
        error_msg = str(exc)[:500]
        ctx = _infer_context(prompt, cwd, has_tools, estimated_file_count)
        injection = (
            f"WARNING: A previous run of this agent failed with {error_type} after {attempt} attempts. "
            f"Error summary: {error_msg[:200]}. "
            f"Context: {ctx['prompt_size_band']} prompt, "
            f"extensions: {', '.join(ctx['file_extensions']) or 'unknown'}. "
            f"Be extra careful with files of these types and consider simplifying your approach."
        )
        _post("/store_lesson", {
            "agent_name": self.agent_name,  # type: ignore[attr-defined]
            "error_type": error_type,
            "error_message": error_msg,
            "file_extensions": ctx["file_extensions"],
            "artifact_category": ctx["artifact_category"],
            "prompt_size_band": ctx["prompt_size_band"],
            "has_baseline": False,
            "retry_attempt": attempt,
            "project_id": project_id,
            "outcome": "failed",
            "fix_description": f"All {attempt} attempts failed: {error_msg[:100]}",
            "prompt_injection": injection,
        })

    def _store_empty_merge(
        self,
        prompt: str,
        cwd: str | None,
        estimated_file_count: int,
        project_id: str = "",
    ) -> None:
        """
        Store a lesson when merge produced no output files.
        This is the most common 'silent failure' in the merge agent.
        """
        ctx = _infer_context(prompt, cwd, True, estimated_file_count)
        injection = (
            "WARNING: A previous merge of this artifact type produced no output files. "
            "Ensure you use the Write tool to save every merged file under ./merged/. "
            "Do not skip files that appear identical — write them explicitly. "
            f"Context: {ctx['prompt_size_band']} prompt, "
            f"extensions: {', '.join(ctx['file_extensions']) or 'unknown'}."
        )
        _post("/store_lesson", {
            "agent_name": "merge",
            "error_type": "empty_merged_files",
            "error_message": "Merge produced no output files",
            "file_extensions": ctx["file_extensions"],
            "artifact_category": ctx["artifact_category"],
            "prompt_size_band": ctx["prompt_size_band"],
            "has_baseline": False,
            "retry_attempt": 1,
            "project_id": project_id,
            "outcome": "failed",
            "fix_description": "No files written to ./merged/ — agent must write all files explicitly",
            "prompt_injection": injection,
        })
