"""
JiraTracker — high-level upgrade-to-JIRA mapping.

Maps upgrade pipeline events (scan, merge, review, finalize) to JIRA
operations (create epic, create subtasks, add comments, transition).

Usage:
    tracker = JiraTracker(project_key="ALDI", epic_key="ALDI-1234")
    tracker.start_run(run_metadata)
    tracker.log_scan(scan_summary)
    tracker.log_merge(artifact_key, merge_result)
    tracker.log_review(artifact_key, review_result)
    tracker.finalize(report_path)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from upgrade_lib.jira.jira_client import JiraClient, JiraConfig, JiraIssue

logger = logging.getLogger(__name__)


@dataclass
class TrackerState:
    """Persisted state for a tracked upgrade run."""
    epic_key: str = ""
    subtasks: dict[str, str] = field(default_factory=dict)  # artifact_key → issue_key
    started_at: str = ""
    finalized_at: str = ""

    def to_dict(self) -> dict:
        return {
            "epic_key": self.epic_key,
            "subtasks": self.subtasks,
            "started_at": self.started_at,
            "finalized_at": self.finalized_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TrackerState:
        return cls(
            epic_key=data.get("epic_key", ""),
            subtasks=data.get("subtasks", {}),
            started_at=data.get("started_at", ""),
            finalized_at=data.get("finalized_at", ""),
        )


class JiraTracker:
    """
    Maps upgrade pipeline events to JIRA operations.

    All methods are safe to call even when JIRA is disabled — they
    return False/None and log warnings.
    """

    def __init__(
        self,
        project_key: str,
        epic_key: str = "",
        client: JiraClient | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.project_key = project_key
        self.client = client or JiraClient()
        self._state_dir = state_dir
        self._state = TrackerState(epic_key=epic_key)

        # Load persisted state if available
        if state_dir:
            self._load_state()

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    @property
    def epic_key(self) -> str:
        return self._state.epic_key

    @property
    def subtasks(self) -> dict[str, str]:
        return self._state.subtasks

    @property
    def state(self) -> TrackerState:
        return self._state

    # ---- state persistence ----------------------------------------------------

    def _state_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return self._state_dir / f"{self.project_key}.jira_tracker.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._state = TrackerState.from_dict(data)
            except Exception as e:
                logger.warning(f"Failed to load tracker state: {e}")

    def _save_state(self) -> None:
        path = self._state_path()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._state.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # ---- pipeline events → JIRA -----------------------------------------------

    def start_run(self, metadata: dict[str, Any] | None = None) -> JiraIssue | None:
        """
        Start an upgrade run. Creates or finds the Epic in JIRA.

        Args:
            metadata: Optional run metadata (source version, target version, etc.)

        Returns:
            The Epic JiraIssue, or None if JIRA is disabled/failed.
        """
        if not self.enabled:
            logger.info("JIRA disabled — skipping start_run")
            return None

        self._state.started_at = datetime.now(timezone.utc).isoformat()

        # If we already have an epic, just add a comment
        if self._state.epic_key:
            issue = self.client.get_issue(self._state.epic_key)
            if issue:
                meta_text = _format_metadata(metadata) if metadata else "No metadata provided."
                self.client.add_comment(
                    self._state.epic_key,
                    f"*Upgrade run started*\n\n{meta_text}",
                )
                self._save_state()
                return issue

        # Create new Epic
        meta_text = _format_metadata(metadata) if metadata else ""
        summary = f"Upgrade Migration — {self.project_key}"
        description = (
            f"Automated upgrade migration for {self.project_key}.\n\n"
            f"Started: {self._state.started_at}\n\n"
            f"{meta_text}"
        )
        issue = self.client.create_issue(
            project_key=self.project_key,
            summary=summary,
            description=description,
            issue_type="Epic",
            labels=["yantrix", "upgrade"],
        )
        if issue:
            self._state.epic_key = issue.key
            self._save_state()
        return issue

    def log_scan(self, scan_summary: dict[str, Any]) -> bool:
        """
        Log scan/compare results as a comment on the Epic.

        Args:
            scan_summary: Dict with totals by decision, risk distribution, etc.
        """
        if not self.enabled or not self._state.epic_key:
            return False

        total = scan_summary.get("total", 0)
        by_decision = scan_summary.get("by_decision", {})
        by_risk = scan_summary.get("by_risk", {})

        lines = [
            "*Scan & Compare completed*",
            "",
            f"Total artifacts: *{total}*",
            "",
            "||Decision||Count||",
        ]
        for dec, count in sorted(by_decision.items()):
            lines.append(f"|{dec}|{count}|")

        if by_risk:
            lines.append("")
            lines.append("||Risk Level||Count||")
            for level, count in sorted(by_risk.items()):
                lines.append(f"|{level}|{count}|")

        return self.client.add_comment(self._state.epic_key, "\n".join(lines))

    def log_merge(self, artifact_key: str, merge_result: dict[str, Any]) -> JiraIssue | None:
        """
        Log a merge result. Creates a subtask under the Epic.

        Args:
            artifact_key: e.g. "ALDI/validationsets/PostValidationSet"
            merge_result: Dict with explanation, quality_result, etc.
        """
        if not self.enabled or not self._state.epic_key:
            return None

        # Check if subtask already exists
        existing_key = self._state.subtasks.get(artifact_key)
        if existing_key:
            # Add comment to existing subtask
            explanation = merge_result.get("explanation", "No explanation.")
            qr = merge_result.get("quality_result", {})
            verdict = qr.get("verdict", "N/A") if qr else "N/A"
            comment = (
                f"*Merge completed* — Quality: *{verdict}*\n\n"
                f"{explanation}"
            )
            self.client.add_comment(existing_key, comment)
            return self.client.get_issue(existing_key)

        # Create subtask
        explanation = merge_result.get("explanation", "")
        qr = merge_result.get("quality_result", {})
        verdict = qr.get("verdict", "N/A") if qr else "N/A"
        files = merge_result.get("files", [])

        description = (
            f"Artifact: {artifact_key}\n"
            f"Quality gate: {verdict}\n"
            f"Files: {', '.join(files)}\n\n"
            f"h3. Merge Explanation\n{explanation}"
        )

        issue = self.client.create_issue(
            project_key=self.project_key,
            summary=f"Merge: {artifact_key.split('/')[-1]}",
            description=description,
            parent_key=self._state.epic_key,
            labels=["yantrix", "merge"],
        )
        if issue:
            self._state.subtasks[artifact_key] = issue.key
            self._save_state()
        return issue

    def log_review(self, artifact_key: str, review_result: dict[str, Any]) -> bool:
        """
        Log a review result as a comment on the artifact's subtask.

        Args:
            artifact_key: e.g. "ALDI/validationsets/PostValidationSet"
            review_result: Dict with verdict, findings, recommendations.
        """
        if not self.enabled:
            return False

        issue_key = self._state.subtasks.get(artifact_key)
        if not issue_key:
            logger.warning(f"No subtask for {artifact_key} — skipping review log")
            return False

        verdict = review_result.get("verdict", "N/A")
        findings = review_result.get("findings", [])
        recommendations = review_result.get("recommendations", [])

        lines = [f"*Review completed* — Verdict: *{verdict}*", ""]
        if findings:
            lines.append("h3. Findings")
            for f in findings:
                sev = f.get("severity", "INFO")
                msg = f.get("message", "")
                file = f.get("file", "")
                lines.append(f"* [{sev}] {file}: {msg}")
        if recommendations:
            lines.append("")
            lines.append("h3. Recommendations")
            for r in recommendations:
                lines.append(f"* {r}")

        return self.client.add_comment(issue_key, "\n".join(lines))

    def finalize(self, report_content: str | None = None) -> bool:
        """
        Finalize the upgrade run. Adds final comment and optionally transitions Epic.

        Args:
            report_content: Optional UPGRADE_REPORT.md content to attach as comment.
        """
        if not self.enabled or not self._state.epic_key:
            return False

        self._state.finalized_at = datetime.now(timezone.utc).isoformat()

        total_subtasks = len(self._state.subtasks)
        lines = [
            "*Upgrade run finalized*",
            "",
            f"Artifacts processed: *{total_subtasks}*",
            f"Started: {self._state.started_at}",
            f"Finalized: {self._state.finalized_at}",
        ]

        if report_content:
            # Truncate if too long for a comment (JIRA limit ~32KB)
            if len(report_content) > 30000:
                report_content = report_content[:30000] + "\n\n_(truncated — see full report in output directory)_"
            lines.append("")
            lines.append("{noformat}")
            lines.append(report_content)
            lines.append("{noformat}")

        self.client.add_comment(self._state.epic_key, "\n".join(lines))
        self._save_state()
        return True

    def get_linked_issues(self) -> list[JiraIssue]:
        """Get all issues linked to this upgrade run (epic + subtasks)."""
        if not self.enabled:
            return []
        issues = []
        if self._state.epic_key:
            epic = self.client.get_issue(self._state.epic_key)
            if epic:
                issues.append(epic)
        for artifact_key, issue_key in self._state.subtasks.items():
            issue = self.client.get_issue(issue_key)
            if issue:
                issues.append(issue)
        return issues

    def create_bulk_subtasks(
        self,
        artifact_keys: list[str],
        prefix: str = "Merge",
    ) -> list[JiraIssue]:
        """
        Create subtasks for multiple artifacts at once.

        Args:
            artifact_keys: List of artifact keys to create subtasks for.
            prefix: Prefix for subtask summaries (e.g., "Merge", "Review").

        Returns:
            List of created JiraIssue objects.
        """
        if not self.enabled or not self._state.epic_key:
            return []

        created = []
        for key in artifact_keys:
            if key in self._state.subtasks:
                continue  # already has a subtask
            name = key.split("/")[-1]
            issue = self.client.create_issue(
                project_key=self.project_key,
                summary=f"{prefix}: {name}",
                description=f"Artifact: {key}",
                parent_key=self._state.epic_key,
                labels=["yantrix", prefix.lower()],
            )
            if issue:
                self._state.subtasks[key] = issue.key
                created.append(issue)

        if created:
            self._save_state()
        return created


def _format_metadata(metadata: dict[str, Any]) -> str:
    """Format run metadata as JIRA wiki markup table."""
    if not metadata:
        return ""
    lines = ["||Key||Value||"]
    for k, v in metadata.items():
        lines.append(f"|{k}|{v}|")
    return "\n".join(lines)
