"""
UpgradeClient — backward-compatible facade over specialized agents.

No ANTHROPIC_API_KEY required. Uses Claude Code's existing OAuth session.

This module delegates to specialized agents in upgrade_lib.agents:
  - MergeAgent   → 3-way merge
  - DiffAgent    → _diff.json generation
  - ReviewAgent  → quality review (PASS/WARN/FAIL)
  - SummaryAgent → executive narrative

The public API is identical to v1, so existing code continues to work.
Usage stats are aggregated across all agents.
"""

from __future__ import annotations

import json
from typing import Any

from upgrade_lib.agents.base_agent import DEFAULT_MODEL, UsageStats
from upgrade_lib.agents.merge_agent import MergeAgent
from upgrade_lib.agents.diff_agent import DiffAgent
from upgrade_lib.agents.review_agent import ReviewAgent
from upgrade_lib.agents.summary_agent import SummaryAgent
from upgrade_lib.claude_router import ClaudeRouter, default_router


class UpgradeClient:
    """
    Backward-compatible facade. Delegates to specialized agents.

    Authentication: relies on Claude Code's existing OAuth login
    (~/.claude). If that is absent, the SDK will fall back to
    ANTHROPIC_API_KEY — but we don't require it.

    The ClaudeRouter sits between each agent and the Claude CLI, selecting
    the cheapest model that can handle the request. Pass `router=False` to
    disable routing and use `model` for every call (old behaviour).
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_turns: int = 1,
        router: ClaudeRouter | None | bool = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        # router=None  → use default_router (auto model selection)
        # router=False → disable routing, all agents use `model`
        # router=ClaudeRouter(...) → use custom router
        _router = default_router if router is None else (None if router is False else router)
        self._merge = MergeAgent(model=model, max_turns=max_turns, router=_router)
        self._diff = DiffAgent(model=model, max_turns=max_turns, router=_router)
        self._review = ReviewAgent(model=model, max_turns=max_turns, router=_router)
        self._summary = SummaryAgent(model=model, max_turns=max_turns, router=_router)

    @property
    def usage(self) -> UsageStats:
        """Aggregate usage across all agents."""
        total = UsageStats()
        total.merge(self._merge.usage)
        total.merge(self._diff.usage)
        total.merge(self._review.usage)
        total.merge(self._summary.usage)
        return total

    # ---- public API (unchanged signatures) -----------------------------------

    def compare_artifact(
        self,
        aldi_files: dict[str, str],
        system_files: dict[str, str],
        customer: str | None = None,
    ) -> dict[str, Any]:
        """Claude-based compare (rarely used — local compare is preferred)."""
        return self._merge.merge(aldi_files, system_files, customer=customer)

    def merge_artifact(
        self,
        aldi_files: dict[str, str],
        system_files: dict[str, str],
        baseline_files: dict[str, str] | None = None,
        customer: str | None = None,
    ) -> dict[str, Any]:
        return self._merge.merge(aldi_files, system_files, baseline_files, customer)

    def generate_diff_json(
        self,
        merged_json: str,
        system_json: str,
        artifact_name: str,
        merged_name: str = "validationset.json",
        system_name: str = "validationset.json",
    ) -> dict[str, Any]:
        return self._diff.generate(
            merged_json, system_json, artifact_name, merged_name, system_name
        )

    def review_merge(
        self,
        merged_files: dict[str, str],
        aldi_files: dict[str, str],
        system_files: dict[str, str],
        customer: str | None = None,
    ) -> str:
        """Legacy review returning freeform markdown."""
        return self._review.review_markdown(merged_files, aldi_files, system_files, customer)

    def review_merge_structured(
        self,
        merged_files: dict[str, str],
        customer_files: dict[str, str],
        system_files: dict[str, str],
        customer: str | None = None,
    ) -> dict[str, Any]:
        """New structured review returning PASS/WARN/FAIL verdict."""
        return self._review.review(merged_files, customer_files, system_files, customer)

    def chat(self, question: str, context: str = "") -> str:
        return self._merge.chat(question, context)

    def generate_summary(self, all_results: Any) -> str:
        return self._summary.summarize_legacy(all_results)

    def generate_summary_enhanced(
        self,
        all_results: Any,
        risk_assessments: dict | None = None,
        quality_results: dict | None = None,
        project_id: str | None = None,
    ) -> str:
        """Enhanced summary with risk + quality context."""
        return self._summary.summarize(
            all_results, risk_assessments, quality_results, project_id=project_id
        )
