"""
MergeAgent — orchestrates a 3-way merge by handing files to Claude.

The Python side does NO merge logic. It only:
  1. Writes the customer / system / baseline file sets to a temp working dir
  2. Hands Claude the paths + the merge policy
  3. Lets Claude read files (with Read tool) and write merged files (with Write tool)
  4. Reads the merged output back into a dict

All merge decisions — 3-way comparison, conflict resolution, JSON resequencing,
class-method dedupe, etc. — are made by Claude, guided by policies/merge_policy.md.
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


_MERGE_TOOLS_PROMPT = """You are merging a {customer} customized artifact on top of SYSTEM 26.2.

# Merge Policy (authoritative — follow exactly)
{merge_policy}

# Working Directories

You have Read, Write, and Glob tools available. The artifact's three file sets
are laid out under the working directory:

  ./customer/  — {customer} customized files (current state)
  ./system/    — SYSTEM 26.2 upgrade target files
  ./baseline/  — Baseline common ancestor (may be empty if no baseline supplied)
  ./merged/    — EMPTY. You must write all merged output files here.

# Your Task

Perform a 3-way merge of every file that should exist in the output artifact and
write the merged result to ./merged/ — preserving the same relative paths.

## Step 1 — Discover files
Use Glob with patterns like `customer/**/*`, `system/**/*`, `baseline/**/*`
(or List the directories) to enumerate every file in each set. The full set of
output filenames is the UNION of files in customer/ and system/.

## Step 2 — For each filename, apply 3-way merge logic
- Read the customer, system, and baseline versions (skip baseline if absent).
- For very large files, use Read with offset/limit to load in chunks.
- Apply the Merge Policy rules above.
- If a file exists only in customer/ → keep it as-is (write to merged/).
- If a file exists only in system/ → take it as-is (new in upgrade).
- If a file is identical across sources → no merge needed, write any version.
- If only one side changed from baseline → take the changed side.
- If both sides changed → produce a true 3-way merge per the policy.
- NEVER emit conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`).
- NEVER drop or skip the baseline when present — it is the common ancestor and
  is required to distinguish "customer changed it" from "system changed it".

## Step 3 — Write merged files
Use the Write tool to save each merged file under ./merged/ at its original
relative path. Create subdirectories as needed.

## Step 4 — Final response
After all files are written, respond with a SINGLE JSON object (and nothing else):

{{
  "explanation": "<concise bullet-point narrative of what changed and why, per file or grouped>"
}}

Do NOT include file contents in the JSON — they are already on disk under ./merged/.

# Important Constraints
- Read every file you intend to merge. Do not assume content.
- For huge files (>1MB), read in chunks via offset/limit and reason locally.
- Preserve trailing newlines and exact whitespace where the policy demands it.
- For JSON: deep-merge by primary key, resequence ROW_SEQ / SET_VALIDATION_ID where required.
- For Java/JS/JSP: when a method exists in both customer and SYSTEM with the same
  signature but DIFFERENT bodies, do NOT just keep SYSTEM's method — merge the body
  statement-by-statement against the baseline so the customer's added statements AND
  SYSTEM's new statements are BOTH preserved. Only dedupe a method when both bodies are
  functionally identical. Never dedupe inside anonymous inner classes. Keep imports
  consistent with usage.
- Do NOT write MERGE_REPORT.md, MERGE_SUMMARY.txt, VERIFICATION_CHECKLIST.txt or any
  analysis/documentation files. Write ONLY the actual merged artifact files to ./merged/.
- NEVER respond with a summary instead of merging, and NEVER stop because the artifact
  seems large or complex. You write files incrementally with the Write tool, so total
  size is never a reason to stop. If there are many files, merge and write them one at a
  time until every file exists in ./merged/. A summary is not an acceptable substitute
  for writing the files.
"""


def _write_tree(files: dict[str, str] | None, root: Path) -> int:
    """Write a {relative_path: content} dict into root/. Returns file count.

    Pure I/O — no merge logic.
    """
    if not files:
        return 0
    count = 0
    for rel_path, content in files.items():
        if content is None:
            continue
        dest = root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        count += 1
    return count


def _read_tree(root: Path) -> dict[str, str]:
    """Read every file under root/ into a {relative_path: content} dict.

    Pure I/O — no merge logic.
    """
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            try:
                result[rel] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Binary file — read as latin-1 to preserve bytes
                result[rel] = path.read_text(encoding="latin-1")
    return result


def _turns_for_files(total_files: int, total_bytes: int = 0) -> int:
    """Scale max_turns to actual file count and total content size.

    Turn budget per file (worst case with chunked reads):
      3 Glob turns (customer + system + baseline discovery)  — paid once
      per file: 3 reads (one per source) + up to 3 chunk reads for large
                files + 1 write = up to 7 turns/file
      1 final JSON response turn

    We use 10 turns/file as the per-file budget to give comfortable headroom,
    then add extra turns if total content is large (chunked reads needed).

    total_bytes = 0 means unknown — fall back to file-count-only estimate.
    """
    # 3 discovery turns + 10 per file + 1 final response
    base = 3 + (total_files * 10) + 1

    # Extra turns for large content (each 50KB chunk costs ~1 extra turn per file)
    if total_bytes > 0:
        extra_chunks = max(0, (total_bytes // 50_000) - total_files)
        base += extra_chunks

    # Always give at least 30 turns; never exceed 200
    return max(30, min(base, 200))


def _detect_drops(
    customer_files: dict[str, str] | None,
    system_files: dict[str, str] | None,
    baseline_files: dict[str, str] | None,
    merged_files: dict[str, str],
) -> list[dict]:
    """Real dropped additions (method/statement/element) in the merged output,
    excluding the guard's own skipped/truncated meta entries. No-op without a
    baseline. Never raises."""
    if not baseline_files:
        return []
    try:
        from upgrade_lib.quality.customization_guard import detect_dropped_additions

        found = detect_dropped_additions(
            customer_files or {}, system_files or {}, baseline_files, merged_files
        )
        return [d for d in found if d.get("kind") not in ("skipped", "truncated")]
    except Exception:  # noqa: BLE001 — detection must never break a merge
        return []


def _format_retry_prompt(missing: list[str], dropped: list[dict]) -> str:
    """Build a targeted re-merge prompt listing files still missing and, per
    file, the specific additions the merge dropped — so Claude fixes exactly
    what's wrong instead of redoing everything blindly."""
    parts = [
        "Your previous merge output is INCOMPLETE or dropped content. Do NOT "
        "summarize and do NOT stop. Use Read/Write against the SAME "
        "./customer, ./system, ./baseline and ./merged directories.",
    ]
    if missing:
        parts.append(
            f"\nThese files are still missing from ./merged/ — merge and Write "
            f"each: {missing}"
        )
    if dropped:
        by_file: dict[str, list[str]] = {}
        for d in dropped:
            by_file.setdefault(d.get("file", "?"), []).append(str(d.get("message", "")))
        parts.append(
            "\nThe following additions were DROPPED and MUST be restored. "
            "Re-merge each file so BOTH the customer's customizations AND SYSTEM "
            "26.2's upgrade changes are present (never keep only one side):"
        )
        for fname, msgs in by_file.items():
            parts.append(f"  {fname}:")
            for m in msgs[:10]:
                parts.append(f"    - {m}")
            if len(msgs) > 10:
                parts.append(f"    - …and {len(msgs) - 10} more in this file.")
    parts.append('\nWhen every file is correct, reply with a single JSON object: {"explanation": "..."}.')
    return "\n".join(parts)


class MergeAgent(BaseAgent):
    agent_name = "merge"
    system_prompt_file = "merge_system.md"

    # Hard ceiling — only reached for very large artifact sets.
    _MERGE_MAX_TURNS = 200
    # Tool-using merges can take longer than single-shot prompts.
    _MERGE_TIMEOUT_SECONDS = 1800  # 30 minutes per artifact
    # Total merge invocations before giving up: the first pass plus targeted
    # re-prompts listing the files still missing from ./merged/. Guards against
    # Claude bailing with a summary or writing only a subset of files.
    _MERGE_COMPLETION_ATTEMPTS = 3

    def merge(
        self,
        customer_files: dict[str, str],
        system_files: dict[str, str],
        baseline_files: dict[str, str] | None = None,
        customer: str | None = None,
    ) -> dict[str, Any]:
        """
        Merge a customer artifact on top of SYSTEM upgrade.

        All merge logic is performed by Claude. Python only stages files to
        a temp dir and reads the merged output back.

        Returns:
            {
                "merged_files": {"filename": "content", ...},
                "explanation": "bullet-point narrative"
            }
        """
        cust = customer or prompts.DEFAULT_CUSTOMER
        policy = prompts.get_merge_policy()

        # Stage inputs and prepare output dir in an isolated workspace
        workdir = Path(tempfile.mkdtemp(prefix="merge_"))
        try:
            customer_dir = workdir / "customer"
            system_dir = workdir / "system"
            baseline_dir = workdir / "baseline"
            merged_dir = workdir / "merged"
            for d in (customer_dir, system_dir, baseline_dir, merged_dir):
                d.mkdir(parents=True, exist_ok=True)

            n_cust = _write_tree(customer_files, customer_dir)
            n_sys = _write_tree(system_files, system_dir)
            n_base = _write_tree(baseline_files, baseline_dir)

            _log.info(
                "[merge] Staged %d customer / %d system / %d baseline files in %s",
                n_cust, n_sys, n_base, workdir,
            )

            # If there's literally nothing to merge, return early
            if n_cust == 0 and n_sys == 0:
                return {"merged_files": {}, "explanation": "No files in either source."}

            prompt = _MERGE_TOOLS_PROMPT.format(
                customer=cust,
                merge_policy=policy,
            )

            total_files = n_cust + n_sys + n_base
            total_bytes = sum(len(c) for c in (customer_files or {}).values()) + \
                          sum(len(c) for c in (system_files or {}).values()) + \
                          sum(len(c) for c in (baseline_files or {}).values())
            dynamic_turns = _turns_for_files(total_files, total_bytes)
            _log.info(
                "[merge] Invoking Claude with Read/Write tools "
                "(workdir=%s, total_files=%d, total_bytes=%d, max_turns=%d)",
                workdir, total_files, total_bytes, dynamic_turns,
            )

            # The full set of expected output files is the UNION of customer/ and
            # system/ relative paths. Claude may bail with a summary or write only
            # some files; if so, re-prompt with the exact missing paths before we
            # give up. This is the safety net for artifact types (e.g. windowdef_tiles)
            # that have no deterministic fallback.
            expected = {p for p in (customer_files or {})} | \
                       {p for p in (system_files or {})}

            merged_files: dict[str, str] = {}
            raw = ""
            missing: set[str] = set()
            dropped: list[dict] = []
            for attempt in range(1, self._MERGE_COMPLETION_ATTEMPTS + 1):
                if attempt == 1:
                    prompt_to_send = prompt
                else:
                    # Re-prompt for BOTH missing files and dropped additions
                    # (methods/statements/elements the merge silently lost).
                    _log.warning(
                        "[merge] Incomplete/incorrect output (attempt %d): "
                        "%d missing, %d dropped — re-prompting",
                        attempt - 1, len(missing), len(dropped),
                    )
                    prompt_to_send = _format_retry_prompt(sorted(missing), dropped)

                raw = self._call(
                    prompt_to_send,
                    allowed_tools=["Read", "Write", "Glob", "LS"],
                    add_dirs=[str(workdir)],
                    max_turns_override=dynamic_turns,
                    cwd=str(workdir),
                    timeout_override=self._MERGE_TIMEOUT_SECONDS,
                    estimated_file_count=total_files,
                )

                # Read merged files back from disk and check completeness +
                # dropped additions (code/XML). JSON/.txt aren't staged here —
                # they're handled deterministically upstream.
                merged_files = _read_tree(merged_dir)
                missing = expected - set(merged_files)
                dropped = _detect_drops(
                    customer_files, system_files, baseline_files, merged_files
                )
                _log.info(
                    "[merge] attempt %d: wrote %d/%d files, %d missing, %d dropped",
                    attempt, len(merged_files), len(expected), len(missing), len(dropped),
                )
                if merged_files and not missing and not dropped:
                    break  # complete and nothing dropped — done

            # Extract explanation (best-effort — the merged files are the source of truth)
            explanation = ""
            try:
                result = extract_json(raw)
                explanation = result.get("explanation", "")
            except Exception as exc:
                _log.warning("[merge] Could not parse explanation JSON: %s", exc)
                explanation = raw.strip()[-2000:] if raw else ""

            if not merged_files:
                # MCP: record this silent failure pattern so future runs are warned
                self._store_empty_merge(prompt, str(workdir), total_files)
                raise RuntimeError(
                    f"Merge produced no output files after "
                    f"{self._MERGE_COMPLETION_ATTEMPTS} attempts — Claude returned a "
                    "summary/analysis instead of writing to ./merged/. Last response: "
                    + (raw[:1000] if raw else "(empty)")
                )

            return {
                "merged_files": merged_files,
                "explanation": explanation,
            }
        finally:
            # Always clean up the temp workspace
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass

    def chat(self, question: str, context: str = "") -> str:
        """Free-form Q&A with merge policy context."""
        prompt = prompts.CHAT_PROMPT.format(
            merge_policy=prompts.get_merge_policy(),
            context=context or "(none)",
            question=question,
        )
        return self._call(prompt)
