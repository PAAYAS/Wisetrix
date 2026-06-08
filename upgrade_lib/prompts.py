"""
Prompt templates and policy loaders.

Policy documents (merge_policy.md, diff_format_spec.md) live in policies/.
Per-agent system prompts live in upgrade_lib/prompts/*.md.

Active prompt templates (used by agents):
  - DIFF_JSON_PROMPT  → DiffAgent
  - REVIEW_PROMPT     → ReviewAgent (legacy markdown mode)
  - CHAT_PROMPT       → MergeAgent.chat()
  - SUMMARY_PROMPT    → SummaryAgent (legacy mode)

Note: MergeAgent uses an inline tools-based prompt defined in merge_agent.py.
"""

from pathlib import Path


# --------------------------------------------------------------------------- #
# Policy loader — loads from policies/ directory
# --------------------------------------------------------------------------- #

_POLICY_CACHE: dict[str, str] = {}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_doc(name: str) -> str:
    """Load a markdown policy doc from policies/, cached."""
    if name in _POLICY_CACHE:
        return _POLICY_CACHE[name]
    path = _PROJECT_ROOT / "policies" / name
    if path.exists():
        _POLICY_CACHE[name] = path.read_text(encoding="utf-8")
    else:
        _POLICY_CACHE[name] = f"(missing policy document: {name})"
    return _POLICY_CACHE[name]


def get_merge_policy() -> str:
    return _load_doc("merge_policy.md")


def get_diff_format_spec() -> str:
    return _load_doc("diff_format_spec.md")


# Default customer label used when a caller doesn't specify one.
DEFAULT_CUSTOMER = "CUSTOMER"


# --------------------------------------------------------------------------- #
# Prompt templates — used by agents
# --------------------------------------------------------------------------- #

DIFF_JSON_PROMPT = """Generate a runtime _diff.json capturing the delta between the merged artifact and SYSTEM 26.2 base.

# Diff Format Spec
{diff_format_spec}

# Inputs

## Merged JSON
Filename: {merged_name}
Content:
{merged_json}

## SYSTEM 26.2 base JSON
Filename: {system_name}
Content:
{system_json}

# Your Task

Produce the complete _diff.json content for artifact id: {artifact_name}.

Follow the Diff Format Spec exactly:
- MOD_FIELDS for top-level scalar differences (always include BASE_SET_ID).
- NEW_RECORD for child records present in merged but not in SYSTEM (by primary key).
- DEL_RECORD for child records in SYSTEM but not in merged.
- Skip children where both sides agree OR where the only difference is ROW_SEQ/SET_VALIDATION_ID due to merge ordering.

Respond as a single JSON object, nothing else:

{{
  "diff_json": "<full _diff.json content as a string, exactly as it should be written to disk>",
  "explanation": "<1-2 sentences on what deltas were emitted>"
}}
"""


REVIEW_PROMPT = """Review the merged output of an upgrade for correctness and safety.

# Inputs

## Merged files (output)
{merged_files}

## {customer} files (source — customer customizations)
{customer_files}

## SYSTEM 26.2 files (target)
{system_files}

# Your Task

Perform a quality review. Look for:
1. Conflict markers (<<<<<<< ======= >>>>>>>) — must not exist.
2. Java/JS/JSP: import-usage mismatches (imported but unused, or used but not imported).
3. Java: duplicate class-level methods (same signature twice).
4. JSON: invalid structure, duplicate primary keys in arrays.
5. JSON: ROW_SEQ / SET_VALIDATION_ID not sequential.
6. Loss of {customer} customization that should have been preserved.
7. Missing SYSTEM 26.2 upgrade changes that should have been adopted.

Respond as plain markdown with sections:
- ## Summary (PASS / WARN / FAIL)
- ## Findings (bulleted, with file:line references where possible)
- ## Recommendations (bulleted, actionable)
"""


CHAT_PROMPT = """You are assisting an engineer working on a GTM upgrade.

# Merge Policy (reference)
{merge_policy}

# Context provided by the user
{context}

# Question
{question}

Respond clearly and concisely. Cite specific files, fields, or lines when relevant.
"""


SUMMARY_PROMPT = """Generate an executive summary narrative from the run results below.

# Per-artifact results
{all_results}

# Your Task

Produce a markdown summary with:
- ## Overview — totals by decision (Merge / Retain / Remove)
- ## Notable Changes — any artifacts that needed significant merge work
- ## Risks / Warnings — flagged reviews, potential regressions
- ## Next Steps — recommended actions for the engineer

Keep it concise but actionable.
"""
