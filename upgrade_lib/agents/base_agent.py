"""
BaseAgent — shared Claude CLI transport extracted from UpgradeClient.

Every specialized agent inherits from this class. It handles:
  - Direct subprocess calls to Claude CLI (reliable in Streamlit)
  - Per-agent system prompt loading from upgrade_lib/prompts/{file}.md
  - JSON extraction from Claude responses
  - Usage tracking (calls, tokens, cost)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Imported lazily to avoid circular imports — router lives one level up
def _get_router():
    from upgrade_lib.claude_router import default_router
    return default_router


DEFAULT_MODEL = "claude-sonnet-4-5"


def _find_claude_exe() -> str:
    """Find the actual claude.exe binary, bypassing shell/CMD wrappers.

    On Windows, shutil.which('claude') returns a shell script or .CMD wrapper
    which subprocess may struggle with. This resolves the real binary.

    Result is cached at module level — no filesystem scan on every call.
    """
    return _CLAUDE_EXE_CACHE


def _resolve_claude_exe() -> str:
    """Resolve claude exe path once at import time."""
    if sys.platform == "win32":
        exe = Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if exe.exists():
            return str(exe)
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    raise FileNotFoundError(
        "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
    )


_CLAUDE_EXE_CACHE: str = _resolve_claude_exe()


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# CLI call timeout in seconds (10 minutes — large merges can be slow)
_CLI_TIMEOUT = 600


# --------------------------------------------------------------------------- #
# Usage tracking
# --------------------------------------------------------------------------- #

@dataclass
class UsageStats:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def merge(self, other: UsageStats) -> None:
        """Merge another UsageStats into this one (for facade aggregation)."""
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def render_files(files: dict[str, str] | None) -> str:
    """Render a {filename: content} dict as a readable prompt block."""
    if not files:
        return "(none)"
    chunks = []
    for name, content in files.items():
        if content is None:
            continue
        chunks.append(f"### {name}\n```\n{content}\n```")
    return "\n\n".join(chunks) if chunks else "(none)"


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from Claude's response, tolerant of code fences."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in response: {text[:300]}")
    return json.loads(text[start : end + 1])


# --------------------------------------------------------------------------- #
# BaseAgent
# --------------------------------------------------------------------------- #

# LearningMixin imported lazily (same pattern as router) to avoid circular imports
def _get_learning_mixin():
    from upgrade_lib.mcp.hooks import LearningMixin
    return LearningMixin


class BaseAgent(_get_learning_mixin()):
    """
    Base class for all upgrade agents.

    Subclasses set:
      - agent_name: str           e.g. "merge", "review"
      - system_prompt_file: str   e.g. "merge_system.md"
    and implement their public methods.

    Inherits LearningMixin which hooks into _call() to:
      - inject lessons from the MCP server before each Claude call
      - store new lessons after failures or retries


    Uses direct subprocess calls to Claude CLI instead of the async SDK,
    which has event-loop conflicts inside Streamlit on Windows.
    """

    agent_name: str = "base"
    system_prompt_file: str | None = None

    _ROUTER_DEFAULT = object()  # sentinel: "caller did not pass router"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_turns: int = 1,
        router=_ROUTER_DEFAULT,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.usage = UsageStats()
        # router not passed  -> use the module-level default router (auto select)
        # router=None        -> routing disabled; always use self.model
        # router=ClaudeRouter -> use that specific router
        if router is self._ROUTER_DEFAULT:
            self._router = _get_router()
        else:
            self._router = router
        # Cache system prompt once per instance — avoids disk read on every call
        self._system_prompt_cache: str | None = None

    # ---- system prompt -------------------------------------------------------

    def _load_system_prompt(self) -> str:
        """Load per-agent system prompt from prompts/ directory (cached per instance)."""
        if self._system_prompt_cache is not None:
            return self._system_prompt_cache
        if not self.system_prompt_file:
            prompt = self._default_system_prompt()
        else:
            path = _PROMPTS_DIR / self.system_prompt_file
            prompt = path.read_text(encoding="utf-8") if path.exists() else self._default_system_prompt()
        self._system_prompt_cache = prompt
        return prompt

    @staticmethod
    def _default_system_prompt() -> str:
        return (
            "You are an expert GTM upgrade engineer. You merge customer "
            "customizations on top of new version upgrades while preserving "
            "functional intent. You are precise, conservative, and never invent "
            "fields or records. You follow the provided merge policy exactly."
        )

    # ---- core transport ------------------------------------------------------

    _MAX_RETRIES = 3
    _RETRY_BACKOFF = 5  # seconds between retries

    def _call(
        self,
        prompt: str,
        system: str | None = None,
        *,
        allowed_tools: list[str] | None = None,
        add_dirs: list[str] | None = None,
        max_turns_override: int | None = None,
        cwd: str | None = None,
        timeout_override: int | None = None,
        estimated_file_count: int = 0,
    ) -> str:
        """Call Claude CLI via subprocess.run — reliable in any context.

        Uses `claude -p` (print mode) which is a simple request/response
        without the streaming/initialize handshake that causes timeouts
        inside Streamlit.

        Optional kwargs enable tool-using agentic calls:
          allowed_tools:          e.g. ["Read", "Write", "Glob"]
          add_dirs:               extra directories Claude is allowed to access
          max_turns_override:     override self.max_turns for this call
          cwd:                    working directory for the subprocess
          timeout_override:       override _CLI_TIMEOUT for this call
          estimated_file_count:   hint to the router (e.g. number of files staged)
        """
        cli = _find_claude_exe()
        system_prompt = system or self._load_system_prompt()
        env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sdk-py"}
        max_turns = max_turns_override if max_turns_override is not None else self.max_turns
        timeout = timeout_override if timeout_override is not None else _CLI_TIMEOUT

        # --- Route: ask the router which model to use for this call ----------
        if self._router:
            from upgrade_lib.claude_router import RouteRequest
            decision = self._router.route(RouteRequest(
                agent_name=self.agent_name,
                prompt_len=len(prompt),
                max_turns=max_turns,
                has_tools=bool(allowed_tools),
                estimated_file_count=estimated_file_count,
            ))
            model = decision.model
        else:
            model = self.model
        # ---------------------------------------------------------------------

        # --- MCP Lesson Lookup: inject known fixes into the system prompt -----
        system_prompt = self._lookup_lessons(
            system_prompt, prompt, cwd,
            has_tools=bool(allowed_tools),
            estimated_file_count=estimated_file_count,
        )
        # ---------------------------------------------------------------------

        last_err: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                _log.info("[%s] Calling Claude CLI model=%s (attempt %d)...", self.agent_name, model, attempt)
                start = time.time()

                # Write system prompt to a temp file to avoid command-line
                # length issues on Windows
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", delete=False, encoding="utf-8"
                ) as sp_file:
                    sp_file.write(system_prompt)
                    sp_path = sp_file.name

                try:
                    cmd = [
                        cli,
                        "-p",                # print mode; reads prompt from stdin
                        "--model", model,
                        "--output-format", "text",
                        "--system-prompt-file", sp_path,
                        "--max-turns", str(max_turns),
                        "--permission-mode", "bypassPermissions",
                    ]
                    if allowed_tools:
                        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
                    if add_dirs:
                        cmd.append("--add-dir")
                        cmd.extend(add_dirs)

                    result = subprocess.run(
                        cmd,
                        input=prompt,          # pass prompt via stdin
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=env,
                        encoding="utf-8",
                        errors="replace",
                        cwd=cwd,
                    )
                finally:
                    os.unlink(sp_path)

                elapsed = time.time() - start
                _log.info("[%s] CLI returned in %.1fs (exit=%d)", self.agent_name, elapsed, result.returncode)

                if result.returncode != 0:
                    stderr = (result.stderr or "").strip()
                    stdout = (result.stdout or "").strip()
                    detail = stderr or stdout or "(no output)"
                    raise RuntimeError(
                        f"Claude CLI exited with code {result.returncode}: {detail[:500]}"
                    )

                self.usage.calls += 1
                # MCP: if we succeeded on a retry, that pattern is worth remembering
                if attempt > 1:
                    self._store_retry_success(attempt, prompt, cwd, bool(allowed_tools), estimated_file_count)
                return result.stdout

            except subprocess.TimeoutExpired:
                last_err = TimeoutError(
                    f"Claude CLI timed out after {timeout}s"
                )
                _log.warning(
                    "[%s] CLI timeout (attempt %d/%d), retrying in %ds...",
                    self.agent_name, attempt, self._MAX_RETRIES, self._RETRY_BACKOFF,
                )
            except RuntimeError as exc:
                err_str = str(exc)
                # Don't retry on permanent errors
                if "Prompt is too long" in err_str:
                    raise
                last_err = exc
                _log.warning(
                    "[%s] CLI error (attempt %d/%d): %s — retrying in %ds...",
                    self.agent_name, attempt, self._MAX_RETRIES, err_str[:200], self._RETRY_BACKOFF,
                )
            except Exception as exc:
                # Non-retryable errors
                raise

            if attempt < self._MAX_RETRIES:
                time.sleep(self._RETRY_BACKOFF)

        # MCP: all retries exhausted — store the failure pattern
        if last_err is not None:
            self._store_failure(last_err, self._MAX_RETRIES, prompt, cwd, bool(allowed_tools), estimated_file_count)
        raise last_err  # type: ignore[misc]
