"""Source providers for resolving customer repos, SYSTEM targets, and baselines."""

from upgrade_lib.sources.base_provider import SourceProvider, ResolvedSource
from upgrade_lib.sources.local_provider import LocalProvider
from upgrade_lib.sources.git_provider import GitProvider
from upgrade_lib.sources.artifactory_provider import ArtifactoryProvider

__all__ = [
    "SourceProvider",
    "ResolvedSource",
    "LocalProvider",
    "GitProvider",
    "ArtifactoryProvider",
]
