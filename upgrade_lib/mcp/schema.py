"""
Lesson schema — the unit of knowledge the MCP learning server stores.

A Lesson captures:
  - What went wrong (or what succeeded after retries)
  - The context fingerprint used for matching similar future errors
  - The prompt injection text inserted into Claude's system prompt
    when this lesson is matched, so it self-corrects without asking the user
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "not",
    "are", "has", "had", "but", "its", "you", "can", "will", "which",
    "when", "what", "how", "all", "one", "into", "than", "been", "more",
}


def extract_keywords(
    error_message: str,
    category: str = "",
    file_extensions: list[str] | None = None,
) -> list[str]:
    """
    Derive a keyword list from an error message + artifact context.

    Used for similarity matching — no embeddings, pure token overlap.
    """
    tokens = re.findall(r"\b[a-z]{3,}\b", error_message.lower())
    tokens = [t for t in tokens if t not in _STOPWORDS]
    if category:
        tokens.append(category.lower())
    for ext in (file_extensions or []):
        tokens.append(ext.lstrip(".").lower())
    # deduplicate preserving order
    seen: set[str] = set()
    result = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result[:30]  # cap at 30 keywords


def prompt_size_band(prompt_len: int) -> str:
    """Bucket prompt length for matching — exact size is too noisy."""
    if prompt_len < 20_000:
        return "small"
    if prompt_len < 100_000:
        return "medium"
    return "large"


# ---------------------------------------------------------------------------
# Lesson
# ---------------------------------------------------------------------------

@dataclass
class Lesson:
    """A single learned error-fix pair."""

    # Identity
    id: str                         # UUID4 hex, set by LessonStore
    created_at: str                 # ISO-8601 UTC

    # What triggered this lesson
    agent_name: str                 # "merge" | "diff" | "review" | "summary" | "risk"
    error_type: str                 # "RuntimeError" | "TimeoutError" | "ValueError"
                                    # | "empty_merged_files" | "success_after_retry"
    error_message: str              # str(exc) truncated to 500 chars

    # Context fingerprint (used for similarity matching)
    file_extensions: list[str]      # e.g. [".java", ".json"]
    artifact_category: str          # e.g. "actions", "datasets", "" if unknown
    prompt_size_band: str           # "small" | "medium" | "large"
    has_baseline: bool              # whether baseline_files was provided
    retry_attempt: int              # which attempt succeeded (1 = first, 2-3 = retry)
    project_id: str                 # which project this was observed on

    # What fixed it
    outcome: str                    # "fixed" | "failed"
    fix_description: str            # human-readable description of the fix
    prompt_injection: str           # text appended to system_prompt when this lesson matches

    # Extracted keywords for matching
    keywords: list[str] = field(default_factory=list)

    # Metadata
    usage_count: int = 0            # how many times this lesson was applied
    last_used: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lesson":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def index_entry(self) -> dict[str, Any]:
        """Minimal record written to index.json for fast scanning."""
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "error_type": self.error_type,
            "artifact_category": self.artifact_category,
            "file_extensions": self.file_extensions,
            "keywords": self.keywords,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "usage_count": self.usage_count,
        }


# ---------------------------------------------------------------------------
# LessonQuery — what callers send to find_lessons()
# ---------------------------------------------------------------------------

@dataclass
class LessonQuery:
    agent_name: str
    error_type: str
    error_message: str = ""
    artifact_category: str = ""
    file_extensions: list[str] = field(default_factory=list)
    has_baseline: bool = False
    prompt_size_band: str = "small"
