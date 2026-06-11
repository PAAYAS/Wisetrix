"""
DiffAgent — generates _diff.json runtime deltas.

Compares merged output against SYSTEM baseline to produce the
domain-specific delta format used at runtime.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from upgrade_lib.agents.base_agent import BaseAgent, extract_json
from upgrade_lib import prompts

_log = logging.getLogger(__name__)

# When the combined merged+system JSON would make the piped prompt approach the
# Claude CLI's 10MB stdin limit, switch to a file-based prompt (content on disk,
# read via the Read tool) instead of inlining it. Threshold leaves generous
# headroom for the spec + instructions + JSON escaping overhead.
_INLINE_CONTENT_LIMIT = 6_000_000  # bytes (chars ~ bytes for JSON/ASCII)
# Extra turns for the file-based path (Claude must Read the files, possibly in
# chunks, before responding).
_FILE_MODE_MAX_TURNS = 15


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

        Small artifacts are sent inline (content embedded in the prompt). Large
        artifacts are written to temp files and read via the Read tool, so the
        piped stdin never exceeds the CLI's 10MB limit.

        Returns:
            {
                "diff_json": "<full _diff.json content>",
                "explanation": "<short description of deltas>"
            }
        """
        spec = prompts.get_diff_format_spec()

        if len(merged_json) + len(system_json) <= _INLINE_CONTENT_LIMIT:
            prompt = prompts.DIFF_JSON_PROMPT.format(
                diff_format_spec=spec,
                merged_json=merged_json,
                system_json=system_json,
                artifact_name=artifact_name,
                merged_name=merged_name,
                system_name=system_name,
            )
            raw = self._call(prompt)
            return extract_json(raw)

        # ── Large content: file-based to avoid the 10MB stdin limit ───────────
        workdir = Path(tempfile.mkdtemp(prefix="diff_"))
        _log.info(
            "[diff] large content (%d bytes) — using file-based prompt (%s)",
            len(merged_json) + len(system_json), workdir,
        )
        try:
            (workdir / "merged.json").write_text(merged_json, encoding="utf-8")
            (workdir / "system.json").write_text(system_json, encoding="utf-8")
            prompt = prompts.DIFF_JSON_FILE_PROMPT.format(
                diff_format_spec=spec,
                artifact_name=artifact_name,
                merged_name=merged_name,
                system_name=system_name,
            )
            raw = self._call(
                prompt,
                allowed_tools=["Read"],
                add_dirs=[str(workdir)],
                cwd=str(workdir),
                max_turns_override=_FILE_MODE_MAX_TURNS,
            )
            return extract_json(raw)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
