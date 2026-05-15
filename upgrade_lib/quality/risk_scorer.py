"""
RiskScorer — deterministic risk scoring for upgrade artifacts.

No Claude calls. Pure Python computation based on artifact metadata
and comparison results. Wraps RiskAgent for convenience.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from upgrade_lib.agents.risk_agent import RiskAgent, RiskAssessment


class RiskScorer:
    """Convenience wrapper over RiskAgent for batch operations."""

    def __init__(self) -> None:
        self._agent = RiskAgent()

    def assess(
        self,
        comparison_result: dict,
        artifact_meta: dict | None = None,
    ) -> RiskAssessment:
        return self._agent.assess(comparison_result, artifact_meta)

    def assess_all(
        self,
        comparison_results: dict,
    ) -> dict[str, RiskAssessment]:
        return self._agent.assess_all(comparison_results)

    @staticmethod
    def summary(assessments: dict[str, RiskAssessment]) -> dict[str, int]:
        """Return counts by risk level."""
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for ra in assessments.values():
            counts[ra.level] = counts.get(ra.level, 0) + 1
        return counts


# Re-export for convenience
__all__ = ["RiskScorer", "RiskAssessment"]
