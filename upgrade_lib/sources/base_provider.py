"""
Base class for source providers.

A source provider resolves configuration (git URL, artifactory URL, or local path)
into local filesystem paths that the upgrade pipeline can use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ResolvedSource:
    """Local filesystem paths resolved from a source provider."""
    source_root: Path               # Customer repos directory
    target_system: Path             # SYSTEM target (e.g., 26.2)
    baseline_system: Path | None    # SYSTEM baseline (e.g., 24.4.11) — optional
    metadata: dict = field(default_factory=dict)  # Provider-specific metadata


class SourceProvider(ABC):
    """Abstract base for source resolution."""

    provider_type: str = "base"

    @abstractmethod
    def resolve(self, config: dict) -> ResolvedSource:
        """
        Resolve configuration into local filesystem paths.

        Args:
            config: Project configuration dict from projects.json

        Returns:
            ResolvedSource with local paths ready for the pipeline.

        Raises:
            ValueError: If configuration is invalid or resolution fails.
        """
        ...

    @abstractmethod
    def validate(self, config: dict) -> list[str]:
        """
        Validate configuration without resolving.

        Returns:
            List of validation error messages. Empty if valid.
        """
        ...
