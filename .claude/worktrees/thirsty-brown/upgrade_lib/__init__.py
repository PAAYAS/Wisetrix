"""
upgrade_lib — GTM Docker-to-Docker upgrade engine.

Architecture:
  agents/    — Specialized Claude agents (merge, diff, review, risk, summary)
  quality/   — Deterministic quality gates + risk scoring
  sources/   — Git, Artifactory, and local path providers
  jira/      — JIRA integration (client + upgrade tracker)
  report/    — Upgrade report generation
  compare.py — Deterministic local compare (ported from v1)
  prompts.py — Prompt templates + policy loaders
"""

# Facade (backward compat)
from .claude_client import UpgradeClient

# Compare
from .compare import apply_business_rules, compare_artifact_local
from . import prompts

# Agents
from .agents import (
    MergeAgent,
    DiffAgent,
    ReviewAgent,
    RiskAgent,
    SummaryAgent,
    UsageStats,
)

# Quality
from .quality import QualityGate, QualityResult, Finding, RiskScorer, RiskAssessment

# Sources
from .sources import (
    SourceProvider,
    ResolvedSource,
    LocalProvider,
    GitProvider,
    ArtifactoryProvider,
)

# JIRA
from .jira import JiraClient, JiraTracker

# Report
from .report import ReportGenerator

__all__ = [
    "UpgradeClient",
    "apply_business_rules",
    "compare_artifact_local",
    "prompts",
    "MergeAgent",
    "DiffAgent",
    "ReviewAgent",
    "RiskAgent",
    "SummaryAgent",
    "UsageStats",
    "QualityGate",
    "QualityResult",
    "Finding",
    "RiskScorer",
    "RiskAssessment",
    "SourceProvider",
    "ResolvedSource",
    "LocalProvider",
    "GitProvider",
    "ArtifactoryProvider",
    "JiraClient",
    "JiraTracker",
    "ReportGenerator",
]
