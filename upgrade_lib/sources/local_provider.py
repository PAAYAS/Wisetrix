"""
LocalProvider — wraps existing local path logic for backward compatibility.

Used when projects.json has the v1 format with source_root, target_system, etc.
as local paths, or when source_type is explicitly "local".
"""

from __future__ import annotations

from pathlib import Path

from upgrade_lib.sources.base_provider import SourceProvider, ResolvedSource


class LocalProvider(SourceProvider):
    """Resolves local filesystem paths directly from config."""

    provider_type = "local"

    def resolve(self, config: dict) -> ResolvedSource:
        errors = self.validate(config)
        if errors:
            raise ValueError(f"Invalid local config: {'; '.join(errors)}")

        source_root = Path(config["source_root"])
        target_system = Path(config["target_system"])
        baseline_str = config.get("baseline_system", "")
        baseline_system = Path(baseline_str) if baseline_str else None

        return ResolvedSource(
            source_root=source_root,
            target_system=target_system,
            baseline_system=baseline_system,
            metadata={"provider": "local"},
        )

    def validate(self, config: dict) -> list[str]:
        errors = []
        if not config.get("source_root"):
            errors.append("source_root is required")
        elif not Path(config["source_root"]).exists():
            errors.append(f"source_root does not exist: {config['source_root']}")

        if not config.get("target_system"):
            errors.append("target_system is required")
        elif not Path(config["target_system"]).exists():
            errors.append(f"target_system does not exist: {config['target_system']}")

        baseline = config.get("baseline_system", "")
        if baseline and not Path(baseline).exists():
            errors.append(f"baseline_system does not exist: {baseline}")

        return errors
