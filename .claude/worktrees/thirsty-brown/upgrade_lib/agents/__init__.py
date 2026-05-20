"""
Specialized upgrade agents — each handles one phase of the pipeline.

BaseAgent provides the shared Claude Agent SDK transport.
Individual agents add focused system prompts and input/output contracts.
"""

from upgrade_lib.agents.base_agent import BaseAgent, UsageStats
from upgrade_lib.agents.merge_agent import MergeAgent
from upgrade_lib.agents.diff_agent import DiffAgent
from upgrade_lib.agents.review_agent import ReviewAgent
from upgrade_lib.agents.risk_agent import RiskAgent
from upgrade_lib.agents.summary_agent import SummaryAgent

__all__ = [
    "BaseAgent",
    "UsageStats",
    "MergeAgent",
    "DiffAgent",
    "ReviewAgent",
    "RiskAgent",
    "SummaryAgent",
]
