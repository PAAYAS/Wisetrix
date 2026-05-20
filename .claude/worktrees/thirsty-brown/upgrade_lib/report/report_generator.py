"""
ReportGenerator — generates UPGRADE_REPORT.md from upgrade run results.

Produces a structured markdown report with:
  - Run metadata (date, customer, source, target, duration, cost)
  - Decision summary table (Merge/Retain/Remove counts)
  - Risk distribution table (HIGH/MEDIUM/LOW)
  - Quality gate results (PASS/WARN/FAIL)
  - High-risk merge details (needs manual attention)
  - Per-artifact merge details
  - AI recommendations (from SummaryAgent)
  - JIRA tracking links (if configured)

Saved to: output/{project_id}/UPGRADE_REPORT.md
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from upgrade_lib.report.pdf_renderer import render_pdf


class ReportGenerator:
    """Generates UPGRADE_REPORT.md from upgrade run data."""

    def generate(
        self,
        project_id: str,
        comparison: dict[str, Any],
        merges: dict[str, Any],
        risks: dict[str, Any] | None = None,
        summary_narrative: str | None = None,
        jira_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        jira_tickets: dict[str, str] | None = None,
    ) -> str:
        """
        Generate the full UPGRADE_REPORT.md content.

        Args:
            project_id: Project identifier (e.g., "ALDI")
            comparison: Comparison results dict (key → result)
            merges: Merge results dict (key → merge record)
            risks: Risk assessment dict (key → {level, score, factors})
            summary_narrative: SummaryAgent narrative (markdown)
            jira_state: JiraTracker state dict (epic_key, subtasks)
            metadata: Run metadata (source info, target version, etc.)
            jira_tickets: Dict mapping artifact key → JIRA ticket key (or "Not Found")

        Returns:
            Complete markdown report as string.
        """
        sections = [
            self._header(project_id, metadata),
            self._artifact_table(comparison, risks, jira_tickets),
            self._decision_summary(comparison),
            self._risk_distribution(comparison, risks),
            self._quality_gate_summary(merges),
            self._high_risk_merges(merges, risks),
            self._merge_details(merges, jira_tickets),
            self._ai_recommendations(summary_narrative),
            self._jira_tracking(jira_state),
            self._footer(),
        ]
        return "\n\n".join(s for s in sections if s)

    def save(
        self,
        report_content: str,
        output_dir: Path,
        project_id: str,
    ) -> Path:
        """Save report to output directory. Returns the file path."""
        out_path = output_dir / project_id / "UPGRADE_REPORT.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_content, encoding="utf-8")
        return out_path

    @staticmethod
    def generate_pdf(
        project_id: str,
        comparison: dict[str, Any],
        merges: dict[str, Any],
        risks: dict[str, Any] | None = None,
        summary_narrative: str | None = None,
        jira_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        jira_tickets: dict[str, str] | None = None,
    ) -> bytes:
        """
        Render the report directly as a polished PDF using reportlab.

        Bypasses the markdown→HTML→PDF pipeline entirely so the layout
        is fully controlled (column widths, wrapping, color-coded badges,
        cover page, page numbers, etc.).
        """
        return render_pdf(
            project_id=project_id,
            comparison=comparison,
            merges=merges,
            risks=risks,
            summary_narrative=summary_narrative,
            jira_state=jira_state,
            metadata=metadata,
            jira_tickets=jira_tickets,
        )

    # ---- section builders -----------------------------------------------------

    @staticmethod
    def _header(project_id: str, metadata: dict[str, Any] | None) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# Upgrade Report — {project_id}",
            "",
            f"Generated: {now}",
        ]
        if metadata:
            lines.append("")
            lines.append("| Field | Value |")
            lines.append("|-------|-------|")
            for k, v in metadata.items():
                lines.append(f"| {k} | {v} |")
        return "\n".join(lines)

    @staticmethod
    def _artifact_table(
        comparison: dict[str, Any],
        risks: dict[str, Any] | None,
        jira_tickets: dict[str, str] | None,
    ) -> str:
        """Full artifact table with Decision, Risk, and JIRA columns."""
        lines = [
            "## Artifact Overview",
            "",
            "| Artifact | Bucket | Decision | Risk | JIRA |",
            "|----------|--------|----------|------|------|",
        ]
        for key, entry in sorted(comparison.items(), key=lambda x: x[0]):
            name = entry.get("name", key.split("/")[-1] if "/" in key else key)
            bucket = entry.get("bucket", "")
            decision = entry.get("decision", "Unknown")
            risk_level = "—"
            if risks:
                r = risks.get(key, {})
                risk_level = r.get("level", "—") if isinstance(r, dict) else "—"
            jira_key = "Not Found"
            if jira_tickets:
                jira_key = jira_tickets.get(key, "Not Found")
            lines.append(f"| {name} | {bucket} | {decision} | {risk_level} | {jira_key} |")
        return "\n".join(lines)

    @staticmethod
    def _decision_summary(comparison: dict[str, Any]) -> str:
        by_decision: dict[str, int] = {}
        for entry in comparison.values():
            dec = entry.get("decision", "Unknown")
            by_decision[dec] = by_decision.get(dec, 0) + 1

        total = len(comparison)
        lines = [
            "## Decision Summary",
            "",
            f"Total artifacts scanned: **{total}**",
            "",
            "| Decision | Count | % |",
            "|----------|------:|--:|",
        ]
        for dec in ["Merge", "Retain", "Remove", "ERROR"]:
            count = by_decision.get(dec, 0)
            if count > 0:
                pct = f"{count / total * 100:.1f}" if total else "0"
                lines.append(f"| {dec} | {count} | {pct}% |")

        # Include any other decisions not in the standard list
        for dec, count in sorted(by_decision.items()):
            if dec not in ("Merge", "Retain", "Remove", "ERROR"):
                pct = f"{count / total * 100:.1f}" if total else "0"
                lines.append(f"| {dec} | {count} | {pct}% |")

        return "\n".join(lines)

    @staticmethod
    def _risk_distribution(
        comparison: dict[str, Any],
        risks: dict[str, Any] | None,
    ) -> str:
        if not risks:
            return ""

        by_level: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for r in risks.values():
            level = r.get("level", "LOW") if isinstance(r, dict) else "LOW"
            by_level[level] = by_level.get(level, 0) + 1

        total = sum(by_level.values())
        lines = [
            "## Risk Distribution",
            "",
            "| Risk Level | Count | % |",
            "|------------|------:|--:|",
        ]
        for level in ["HIGH", "MEDIUM", "LOW"]:
            count = by_level[level]
            pct = f"{count / total * 100:.1f}" if total else "0"
            icon = {"HIGH": "!!!", "MEDIUM": "!!", "LOW": "OK"}.get(level, "")
            lines.append(f"| {icon} {level} | {count} | {pct}% |")

        return "\n".join(lines)

    @staticmethod
    def _quality_gate_summary(merges: dict[str, Any]) -> str:
        if not merges:
            return ""

        verdicts: dict[str, int] = {"PASS": 0, "WARN": 0, "FAIL": 0}
        for m in merges.values():
            qr = m.get("quality_result", {})
            if qr:
                v = qr.get("verdict", "N/A")
                verdicts[v] = verdicts.get(v, 0) + 1

        total_checked = sum(verdicts.values())
        if total_checked == 0:
            return ""

        lines = [
            "## Quality Gate Results",
            "",
            f"Artifacts checked: **{total_checked}**",
            "",
            "| Verdict | Count |",
            "|---------|------:|",
        ]
        for v in ["PASS", "WARN", "FAIL"]:
            if verdicts.get(v, 0) > 0:
                lines.append(f"| {v} | {verdicts[v]} |")

        # List findings from FAIL/WARN merges
        problem_merges = [
            (k, m) for k, m in merges.items()
            if m.get("quality_result", {}).get("verdict") in ("FAIL", "WARN")
        ]
        if problem_merges:
            lines.append("")
            lines.append("### Issues Found")
            lines.append("")
            for key, merge in problem_merges:
                qr = merge.get("quality_result", {})
                verdict = qr.get("verdict", "N/A")
                lines.append(f"**{key}** — {verdict}")
                for f in qr.get("findings", []):
                    sev = f.get("severity", "INFO")
                    msg = f.get("message", "")
                    file = f.get("file", "")
                    lines.append(f"  - [{sev}] `{file}`: {msg}")
                lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _high_risk_merges(
        merges: dict[str, Any],
        risks: dict[str, Any] | None,
    ) -> str:
        if not risks or not merges:
            return ""

        high_risk = []
        for key, merge in merges.items():
            risk = risks.get(key, {})
            level = risk.get("level", "LOW") if isinstance(risk, dict) else "LOW"
            if level == "HIGH":
                high_risk.append((key, merge, risk))

        if not high_risk:
            return ""

        lines = [
            "## High-Risk Merges (Manual Review Recommended)",
            "",
            "| Artifact | Risk Score | Factors | Quality |",
            "|----------|-----------|---------|---------|",
        ]
        for key, merge, risk in high_risk:
            score = risk.get("score", 0) if isinstance(risk, dict) else 0
            factors = ", ".join(risk.get("factors", [])) if isinstance(risk, dict) else ""
            qr = merge.get("quality_result", {})
            verdict = qr.get("verdict", "N/A") if qr else "N/A"
            name = key.split("/")[-1] if "/" in key else key
            lines.append(f"| {name} | {score:.2f} | {factors} | {verdict} |")

        return "\n".join(lines)

    @staticmethod
    def _merge_details(
        merges: dict[str, Any],
        jira_tickets: dict[str, str] | None = None,
    ) -> str:
        if not merges:
            return ""

        lines = [
            "## Merge Details",
            "",
            f"Total merged: **{len(merges)}**",
            "",
        ]
        for key, merge in merges.items():
            bucket = merge.get("bucket", "")
            merged_at = merge.get("merged_at", "")
            files = merge.get("files", [])
            explanation = merge.get("explanation", "No explanation.")
            diff_ok = merge.get("diff_generated", False)
            diff_err = merge.get("diff_error")
            jira_key = (jira_tickets or {}).get(key, "Not Found")

            lines.append(f"### {key}")
            lines.append("")
            lines.append(f"- **Bucket**: {bucket}")
            lines.append(f"- **JIRA**: {jira_key}")
            lines.append(f"- **Merged at**: {merged_at}")
            lines.append(f"- **Files**: {', '.join(files)}")
            lines.append(f"- **_diff.json**: {'Generated' if diff_ok else 'N/A'}"
                         + (f" (error: {diff_err})" if diff_err else ""))
            lines.append("")
            lines.append("**Explanation:**")
            lines.append("")
            lines.append(explanation)
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _ai_recommendations(summary_narrative: str | None) -> str:
        if not summary_narrative:
            return ""
        return f"## AI Recommendations\n\n{summary_narrative}"

    @staticmethod
    def _jira_tracking(jira_state: dict[str, Any] | None) -> str:
        if not jira_state:
            return ""

        epic_key = jira_state.get("epic_key", "")
        subtasks = jira_state.get("subtasks", {})

        if not epic_key:
            return ""

        lines = [
            "## JIRA Tracking",
            "",
            f"- **Epic**: {epic_key}",
            f"- **Subtasks**: {len(subtasks)}",
        ]
        if subtasks:
            lines.append("")
            lines.append("| Artifact | JIRA Issue |")
            lines.append("|----------|-----------|")
            for artifact, issue_key in subtasks.items():
                name = artifact.split("/")[-1] if "/" in artifact else artifact
                lines.append(f"| {name} | {issue_key} |")

        return "\n".join(lines)

    @staticmethod
    def _footer() -> str:
        return (
            "---\n\n"
            "_Generated by Wisetrix — GTM Docker-to-Docker Upgrade Engine_"
        )
