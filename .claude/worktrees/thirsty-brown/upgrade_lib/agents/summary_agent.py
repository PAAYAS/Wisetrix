"""
SummaryAgent — generates executive narrative for an upgrade run.

Input includes comparison results, merge records, review verdicts,
and risk assessments. Produces a structured markdown summary.
"""

from __future__ import annotations

import json
from typing import Any

from upgrade_lib.agents.base_agent import BaseAgent
from upgrade_lib import prompts


_SUMMARY_PROMPT_ENHANCED = """Generate an executive summary narrative for the **{project_name}** upgrade run.

# Aggregate counts
{aggregate_summary}

# Risk Distribution
{risk_summary}

# Quality Gate Results
{quality_summary}

# High-risk artifacts (full detail)
{high_risk_details}

# Notable / non-trivial artifacts (compact)
{notable_details}

# Sample of remaining artifacts (compact, capped)
{sample_details}

# Your Task

Produce a markdown summary referring to this upgrade as the **{project_name}** upgrade throughout. Include:
- ## Overview — totals by decision (Merge / Retain / Remove), risk distribution (HIGH/MEDIUM/LOW)
- ## Quality Gate Summary — PASS/WARN/FAIL counts, any blocking findings
- ## High-Risk Merges — artifacts scored HIGH that need manual attention (table: artifact, risk factors, review verdict)
- ## Notable Changes — any artifacts that needed significant merge work
- ## Risks / Warnings — flagged reviews, potential regressions
- ## Next Steps — recommended actions for the engineer (prioritized by risk)

Keep it concise but actionable. Prioritize HIGH-risk items at the top. Do not use the word "CargoWise".
"""


# Maximum number of compact records to send for non-high-risk artifacts.
_NOTABLE_CAP = 40
_SAMPLE_CAP = 60
_ANALYSIS_CHARS = 400


def _compact_record(key: str, entry: dict, *, full: bool = False) -> dict:
    """Strip a comparison entry down to the fields the summary actually needs."""
    if not isinstance(entry, dict):
        return {"key": key, "value": str(entry)[:200]}
    keep_keys = (
        "decision", "risk_level", "risk_score", "bucket", "category",
        "subcategory", "name", "rel_path",
    )
    out: dict[str, Any] = {"key": key}
    for k in keep_keys:
        if entry.get(k) not in (None, ""):
            out[k] = entry[k]

    analysis = entry.get("analysis")
    if isinstance(analysis, str) and analysis:
        limit = _ANALYSIS_CHARS if full else 160
        out["analysis"] = analysis[:limit] + ("…" if len(analysis) > limit else "")

    if full:
        # For high-risk items keep a few extra signals.
        for extra in ("risk_factors", "review_verdict", "quality_verdict"):
            if entry.get(extra) not in (None, "", []):
                out[extra] = entry[extra]
    return out


def _is_notable(entry: dict) -> bool:
    """Heuristic: artifact is interesting beyond aggregate counts."""
    if not isinstance(entry, dict):
        return False
    if entry.get("risk_level") == "MEDIUM":
        return True
    if entry.get("decision") in ("Remove", "ERROR"):
        return True
    if entry.get("review_verdict") in ("WARN", "FAIL"):
        return True
    if entry.get("quality_verdict") in ("WARN", "FAIL"):
        return True
    return False


def _build_payload_sections(all_results: Any) -> tuple[str, str, str, str]:
    """Build the compact payload sections sent to Claude.

    Returns (aggregate_summary, high_risk_details, notable_details, sample_details).
    """
    if not isinstance(all_results, dict):
        # Already serialized — just pass through (fallback).
        return ("(see raw results)", str(all_results)[:8000], "", "")

    decision_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    high_risk: list[dict] = []
    notable: list[dict] = []
    others: list[tuple[str, dict]] = []

    for key, entry in all_results.items():
        if not isinstance(entry, dict):
            continue
        decision_counts[entry.get("decision", "?")] = (
            decision_counts.get(entry.get("decision", "?"), 0) + 1
        )
        level = entry.get("risk_level", "LOW")
        risk_counts[level] = risk_counts.get(level, 0) + 1
        if level == "HIGH":
            high_risk.append(_compact_record(key, entry, full=True))
        elif _is_notable(entry):
            notable.append(_compact_record(key, entry))
        else:
            others.append((key, entry))

    notable_capped = notable[:_NOTABLE_CAP]
    sample_capped = [_compact_record(k, e) for k, e in others[:_SAMPLE_CAP]]
    omitted = max(0, len(notable) - len(notable_capped)) + max(0, len(others) - len(sample_capped))

    aggregate = {
        "total": len(all_results),
        "by_decision": decision_counts,
        "by_risk": risk_counts,
        "omitted_from_payload": omitted,
    }
    return (
        json.dumps(aggregate, indent=2),
        json.dumps(high_risk, indent=2) if high_risk else "(none)",
        json.dumps(notable_capped, indent=2) if notable_capped else "(none)",
        json.dumps(sample_capped, indent=2) if sample_capped else "(none)",
    )


class SummaryAgent(BaseAgent):
    agent_name = "summary"
    system_prompt_file = "summary_system.md"

    def summarize(
        self,
        all_results: Any,
        risk_assessments: dict | None = None,
        quality_results: dict | None = None,
        project_id: str | None = None,
    ) -> str:
        """
        Generate executive narrative summary.

        Args:
            all_results: Comparison + merge results (dict or JSON string)
            risk_assessments: Optional risk scores per artifact
            quality_results: Optional quality gate results per artifact
            project_id: Project identifier used to name the upgrade in the narrative
                        (e.g. "ALDI" → "ALDI GTM Upgrade")

        Returns:
            Markdown string with structured summary.
        """
        # Derive a human-friendly upgrade name from the project ID.
        # "ALDI-2-upgrade" → "ALDI", "EMRSN" → "EMRSN", None → "GTM"
        if project_id:
            customer = project_id.split("-")[0].upper()
            project_name = f"{customer} GTM Upgrade"
        else:
            project_name = "GTM Upgrade"
        if isinstance(all_results, str):
            # Caller already serialized — fall back to legacy single-blob behaviour
            # but truncate aggressively so we never exceed CLI prompt limits.
            blob = all_results[:60000]
            aggregate_summary = "(provided as raw blob below)"
            high_risk_details = blob
            notable_details = "(included in raw blob)"
            sample_details = "(included in raw blob)"
        else:
            (
                aggregate_summary,
                high_risk_details,
                notable_details,
                sample_details,
            ) = _build_payload_sections(all_results)

        # Build risk summary
        risk_summary = "(not available)"
        if risk_assessments:
            counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
            for ra in risk_assessments.values():
                level = ra.level if hasattr(ra, "level") else ra.get("level", "LOW")
                counts[level] = counts.get(level, 0) + 1
            risk_summary = json.dumps(counts, indent=2)

        # Build quality summary
        quality_summary = "(not available)"
        if quality_results:
            verdicts = {"PASS": 0, "WARN": 0, "FAIL": 0}
            for qr in quality_results.values():
                verdict = qr.verdict if hasattr(qr, "verdict") else qr.get("verdict", "PASS")
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
            quality_summary = json.dumps(verdicts, indent=2)

        prompt = _SUMMARY_PROMPT_ENHANCED.format(
            project_name=project_name,
            aggregate_summary=aggregate_summary,
            risk_summary=risk_summary,
            quality_summary=quality_summary,
            high_risk_details=high_risk_details,
            notable_details=notable_details,
            sample_details=sample_details,
        )
        return self._call(prompt)

    def summarize_legacy(self, all_results: Any) -> str:
        """Legacy summary without risk/quality context (backward compat)."""
        serialized = (
            all_results
            if isinstance(all_results, str)
            else json.dumps(all_results, indent=2, default=str)
        )
        prompt = prompts.SUMMARY_PROMPT.format(all_results=serialized)
        return self._call(prompt)
