"""
DiffAgent — generates _diff.json runtime deltas.

Compares merged output against SYSTEM baseline to produce the
domain-specific delta format used at runtime.
"""

from __future__ import annotations

from typing import Any

from upgrade_lib.agents.base_agent import BaseAgent, extract_json
from upgrade_lib import prompts


class DiffAgent(BaseAgent):
    agent_name = "diff"
    system_prompt_file = "diff_system.md"

    def generate(
        self,
        merged_json: str,
        system_json: str,
        artifact_name: str,
        merged_name: str = "validationset.json",
        system_name: str = "validationset.json",
    ) -> dict[str, Any]:
        """
        Generate _diff.json for a merged artifact.

        Returns:
            {
                "diff_json": "<full _diff.json content>",
                "explanation": "<short description of deltas>"
            }
        """
        prompt = prompts.DIFF_JSON_PROMPT.format(
            diff_format_spec=prompts.get_diff_format_spec(),
            merged_json=merged_json,
            system_json=system_json,
            artifact_name=artifact_name,
            merged_name=merged_name,
            system_name=system_name,
        )
        raw = self._call(prompt)
        return extract_json(raw)
