"""
RiskAgent — deterministic risk scoring per artifact.

No Claude calls. Pure Python computation based on artifact metadata
and comparison results. Assigns HIGH / MEDIUM / LOW risk levels
to help engineers prioritize manual review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RiskAssessment:
    level: str          # "HIGH" | "MEDIUM" | "LOW"
    score: float        # 0.0 - 1.0
    factors: list[str] = field(default_factory=list)


# Categories that carry inherently higher upgrade risk.
_HIGH_RISK_CATEGORIES = {
    "actions", "custom_privilages", "datasets", "integration_def",
    "bizpolicydefs", "bizruledefs",
}

# File extensions that indicate code (harder to merge than config).
_CODE_EXTENSIONS = {".java", ".js", ".jsp", ".groovy"}


class RiskAgent:
    """Deterministic risk scorer — no Claude calls."""

    agent_name = "risk"

    def assess(
        self,
        comparison_result: dict,
        artifact_meta: dict | None = None,
    ) -> RiskAssessment:
        """
        Assess risk for a single artifact based on its comparison result.

        Args:
            comparison_result: Output from compare_artifact_local()
            artifact_meta: Optional artifact descriptor from scan_artifacts()

        Returns:
            RiskAssessment with level, score, and human-readable factors.
        """
        score = 0.0
        factors: list[str] = []
        meta = artifact_meta or {}

        file_decisions = comparison_result.get("file_decisions", {})
        file_count = len(file_decisions)

        # Factor 1: File count
        if file_count > 5:
            score += 0.15
            factors.append(f"High file count ({file_count} files)")
        elif file_count > 3:
            score += 0.08
            factors.append(f"Moderate file count ({file_count} files)")

        # Factor 2: Change volume (proportion of files needing merge)
        merge_count = sum(
            1 for d in file_decisions.values()
            if (d == "Merge" if isinstance(d, str) else d.get("status") == "MERGE")
        )
        if file_count > 0:
            change_ratio = merge_count / file_count
            if change_ratio > 0.7:
                score += 0.2
                factors.append(f"High change volume ({merge_count}/{file_count} files differ)")
            elif change_ratio > 0.4:
                score += 0.1
                factors.append(f"Moderate change volume ({merge_count}/{file_count} files differ)")

        # Factor 3: Category risk
        category = meta.get("category", "") or comparison_result.get("category", "")
        if category in _HIGH_RISK_CATEGORIES:
            score += 0.2
            factors.append(f"High-risk category: {category}")

        # Factor 4: Code files involved
        has_code = any(
            Path(f).suffix.lower() in _CODE_EXTENSIONS
            for f in file_decisions.keys()
        )
        if has_code:
            score += 0.2
            factors.append("Contains code files (.java/.js/.jsp)")

        # Factor 5: No target in SYSTEM (source-only, no upgrade reference)
        if not comparison_result.get("target_exists", True):
            score += 0.15
            factors.append("Source-only artifact (no SYSTEM counterpart)")

        # Factor 6: Business rule override (already flagged for removal)
        decision_note = comparison_result.get("decision_note", "")
        if decision_note and "Auto-removed" in decision_note:
            # Auto-removed artifacts are LOW risk by definition
            score = max(0.0, score - 0.3)
            factors.append("Auto-removed by business rule (lower risk)")

        # Clamp and classify
        score = min(1.0, max(0.0, score))
        if score >= 0.5:
            level = "HIGH"
        elif score >= 0.25:
            level = "MEDIUM"
        else:
            level = "LOW"

        return RiskAssessment(level=level, score=round(score, 2), factors=factors)

    def assess_all(
        self,
        comparison_results: dict,
    ) -> dict[str, RiskAssessment]:
        """
        Assess risk for all artifacts in a comparison run.

        Args:
            comparison_results: Full comparison dict (keyed by source_rel)

        Returns:
            Dict mapping source_rel → RiskAssessment
        """
        assessments: dict[str, RiskAssessment] = {}
        for key, result in comparison_results.items():
            meta = {
                "category": result.get("category", ""),
                "bucket": result.get("bucket", ""),
                "name": result.get("name", ""),
            }
            assessments[key] = self.assess(result, meta)
        return assessments
