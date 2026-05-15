"""
ReviewAgent — quality review of merged output.

Returns structured PASS/WARN/FAIL verdict with typed findings,
not freeform markdown. This enables quality gates in the pipeline.
"""

from __future__ import annotations

from typing import Any

from upgrade_lib.agents.base_agent import BaseAgent, extract_json, render_files
from upgrade_lib import prompts


# The review prompt is enhanced to request structured JSON output.
_REVIEW_PROMPT_STRUCTURED = """Review the merged output of an upgrade for correctness and safety.

# Inputs

## Merged files (output)
{merged_files}

## {customer} files (source — customer customizations)
{customer_files}

## SYSTEM 26.2 files (target)
{system_files}

# Your Task

Perform a quality review. Check for:
1. Conflict markers (<<<<<<< ======= >>>>>>>) — must not exist.
2. Java/JS/JSP: import-usage mismatches (imported but unused, or used but not imported).
3. Java: duplicate class-level methods (same signature twice).
4. JSON: invalid structure, duplicate primary keys in arrays.
5. JSON: ROW_SEQ / SET_VALIDATION_ID not sequential.
6. Loss of {customer} customization that should have been preserved.
7. Missing SYSTEM 26.2 upgrade changes that should have been adopted.

Respond as a single JSON object, nothing else:

{{
  "verdict": "PASS" | "WARN" | "FAIL",
  "findings": [
    {{
      "severity": "ERROR" | "WARNING" | "INFO",
      "category": "<conflict_markers|import_mismatch|duplicate_method|invalid_json|invalid_xml|sequencing|lost_customization|missing_upgrade|other>",
      "file": "<filename>",
      "line": <line number or null>,
      "message": "<human-readable description>"
    }}
  ],
  "recommendations": ["<actionable recommendation>"],
  "summary": "<2-3 sentence overall assessment>"
}}
"""


class ReviewAgent(BaseAgent):
    agent_name = "review"
    system_prompt_file = "review_system.md"

    def review(
        self,
        merged_files: dict[str, str],
        customer_files: dict[str, str],
        system_files: dict[str, str],
        customer: str | None = None,
    ) -> dict[str, Any]:
        """
        Review merged output for quality issues.

        Returns:
            {
                "verdict": "PASS" | "WARN" | "FAIL",
                "findings": [{"severity", "category", "file", "line", "message"}],
                "recommendations": ["..."],
                "summary": "..."
            }
        """
        cust = customer or prompts.DEFAULT_CUSTOMER
        prompt = _REVIEW_PROMPT_STRUCTURED.format(
            merged_files=render_files(merged_files),
            customer_files=render_files(customer_files),
            system_files=render_files(system_files),
            customer=cust,
        )
        raw = self._call(prompt)
        return extract_json(raw)

    def review_markdown(
        self,
        merged_files: dict[str, str],
        customer_files: dict[str, str],
        system_files: dict[str, str],
        customer: str | None = None,
    ) -> str:
        """Legacy review returning freeform markdown (backward compat)."""
        cust = customer or prompts.DEFAULT_CUSTOMER
        prompt = prompts.REVIEW_PROMPT.format(
            merged_files=render_files(merged_files),
            aldi_files=render_files(customer_files),
            system_files=render_files(system_files),
            customer=cust,
        )
        return self._call(prompt)
