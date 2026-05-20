"""
ClaudeRouter — model selection layer between agents and Claude CLI.

Acts as a bridge: every agent call passes through here before hitting the
Claude CLI subprocess. The router picks the cheapest model that can handle
the request correctly, logging its decision for observability.

Routing table (default):
  merge   → Sonnet  (tool-using, up to 200 turns, complex 3-way merge logic)
  diff    → Haiku   (single-shot JSON delta — fast & cheap)
  review  → Sonnet  (structured PASS/WARN/FAIL analysis)
  summary → Haiku   (narrative generation, no tools)
  chat    → Haiku   (interactive Q&A)
  risk    → Haiku   (deterministic scoring prompt)

Escalation rules (applied on top of defaults):
  1. Tool-using call + many files  → Sonnet minimum
  2. Very large prompt (>100 KB)   → Sonnet minimum
  3. Merge with >100 files         → Opus (large artifact set)

All non-Claude paths (FastAPI routers, source providers, JIRA, compare,
quality gates) are unaffected — they never touch this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model constants  — update here if you upgrade the Claude family
# ---------------------------------------------------------------------------

HAIKU  = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-5"
OPUS   = "claude-opus-4-5"


# ---------------------------------------------------------------------------
# Request / Decision value objects
# ---------------------------------------------------------------------------

@dataclass
class RouteRequest:
    """Everything the router needs to make a routing decision."""
    agent_name: str                     # "merge" | "diff" | "review" | "summary" | "chat" | "risk"
    prompt_len: int                     # len(prompt) in characters
    max_turns: int = 1                  # 1 = single-shot; >1 = tool-using agentic call
    has_tools: bool = False             # True when allowed_tools is non-empty
    estimated_file_count: int = 0       # hint from the agent (0 = unknown)
    # Optional: caller can pin a model, bypassing routing logic entirely
    model_override: str | None = None


@dataclass
class RouteDecision:
    """What the router decided and why."""
    model: str
    reason: str


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------

# Default model per agent — cheapest that can reliably handle the task.
_AGENT_DEFAULTS: dict[str, str] = {
    "merge":   SONNET,   # complex tool-using 3-way merge
    "diff":    HAIKU,    # simple JSON delta generation
    "review":  SONNET,   # structured quality analysis
    "summary": HAIKU,    # narrative text generation
    "chat":    HAIKU,    # interactive Q&A
    "risk":    HAIKU,    # fast scoring prompt
    "base":    SONNET,   # fallback for unknown agent names
}

# Escalation thresholds
_TOOL_CALL_FILE_THRESHOLD = 50    # files: Haiku → Sonnet
_LARGE_PROMPT_CHARS       = 100_000  # ~25K tokens: Haiku → Sonnet
_LARGE_MERGE_FILE_COUNT   = 100   # files: Sonnet → Opus


# ---------------------------------------------------------------------------
# ClaudeRouter
# ---------------------------------------------------------------------------

class ClaudeRouter:
    """
    Routes each agent call to the appropriate Claude model.

    Usage
    -----
    router = ClaudeRouter()
    decision = router.route(RouteRequest(agent_name="merge", ...))
    # decision.model  → the model string to pass to `--model`
    # decision.reason → logged for observability

    Customising routing
    -------------------
    Pass `default_overrides` to change per-agent defaults without subclassing:

        router = ClaudeRouter(default_overrides={"diff": SONNET})

    Pass `model_override` on a RouteRequest to pin one specific call:

        req = RouteRequest(agent_name="review", ..., model_override=OPUS)
    """

    def __init__(
        self,
        default_overrides: dict[str, str] | None = None,
    ) -> None:
        self._defaults = {**_AGENT_DEFAULTS, **(default_overrides or {})}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, request: RouteRequest) -> RouteDecision:
        """
        Return a RouteDecision for the given request.

        Rules are applied in priority order:
          1. Hard override on the request → always wins
          2. Agent-name default (from routing table)
          3. Escalation rules (in ascending model-tier order)
        """
        # 1. Caller-level override
        if request.model_override:
            return RouteDecision(
                model=request.model_override,
                reason="caller override",
            )

        model = self._defaults.get(request.agent_name, SONNET)
        reason = f"default for agent '{request.agent_name}'"

        # 2. Escalation: tool-using + large file set -> Sonnet minimum
        if (
            request.has_tools
            and request.estimated_file_count > _TOOL_CALL_FILE_THRESHOLD
            and model == HAIKU
        ):
            model = SONNET
            reason = (
                f"tool-using call with {request.estimated_file_count} files "
                f"exceeds threshold ({_TOOL_CALL_FILE_THRESHOLD}) -> escalated to Sonnet"
            )

        # 3. Escalation: large prompt -> Sonnet minimum
        if request.prompt_len > _LARGE_PROMPT_CHARS and model == HAIKU:
            model = SONNET
            reason = (
                f"prompt length {request.prompt_len:,} chars "
                f"exceeds {_LARGE_PROMPT_CHARS:,} -> escalated to Sonnet"
            )

        # 4. Escalation: very large merge -> Opus
        if (
            request.agent_name == "merge"
            and request.estimated_file_count > _LARGE_MERGE_FILE_COUNT
        ):
            model = OPUS
            reason = (
                f"merge with {request.estimated_file_count} files "
                f"exceeds {_LARGE_MERGE_FILE_COUNT} -> escalated to Opus"
            )

        decision = RouteDecision(model=model, reason=reason)
        _log.info(
            "[router] agent=%s  model=%s  reason=%s",
            request.agent_name,
            decision.model,
            decision.reason,
        )
        return decision

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def model_for(self, agent_name: str) -> str:
        """Quick lookup of the default model for an agent (no escalation)."""
        return self._defaults.get(agent_name, SONNET)

    def __repr__(self) -> str:
        return f"ClaudeRouter(defaults={self._defaults})"


# ---------------------------------------------------------------------------
# Module-level singleton — agents import this directly
# ---------------------------------------------------------------------------

#: Default shared router.  Replace with ClaudeRouter(default_overrides={...})
#: in settings or at startup if you want to pin specific models.
default_router = ClaudeRouter()
