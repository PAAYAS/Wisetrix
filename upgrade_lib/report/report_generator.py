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

from upgrade_lib.quality.cat_classifier import estimate_effort
from upgrade_lib.report.pdf_renderer import render_pdf


def _fmt_duration(seconds: float | int | None) -> str:
    """Human-friendly duration, e.g. 8s / 3m 12s / 1h 04m."""
    if not seconds or seconds < 0:
        return "0s"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


# CAT 1-5 canonical names (E2open GTM configuration-severity scale).
_CAT_NAMES: dict[int, str] = {
    1: "Self-Service Setting",
    2: "Pre-Defined Configuration",
    3: "Extension Configuration",
    4: "Complex Model Configuration",
    5: "Custom Application Development",
}


def _cat_cell(entry: dict[str, Any]) -> str:
    """Render an artifact's CAT for a table cell: e.g. 'CAT3 ✓' / 'CAT4 ✗'."""
    lvl = entry.get("cat_level")
    if not isinstance(lvl, int) or not (1 <= lvl <= 5):
        return "—"
    uf = entry.get("cat_upgrade_friendly")
    marker = "" if uf is None else (" ✓" if uf else " ✗")
    return f"CAT{lvl}{marker}"


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
        timings: dict[str, Any] | None = None,
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
            self._time_and_effort(comparison, timings),
            self._risk_distribution(comparison, risks),
            self._cat_distribution(comparison),
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
        timings: dict[str, Any] | None = None,
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
            timings=timings,
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
        """Full artifact table with Decision, Risk, CAT, and JIRA columns."""
        lines = [
            "## Artifact Overview",
            "",
            "| Artifact | Bucket | Decision | Risk | CAT | JIRA |",
            "|----------|--------|----------|------|-----|------|",
        ]
        for key, entry in sorted(comparison.items(), key=lambda x: x[0]):
            name = entry.get("name", key.split("/")[-1] if "/" in key else key)
            bucket = entry.get("bucket", "")
            decision = entry.get("decision", "Unknown")
            risk_level = "—"
            if risks:
                r = risks.get(key, {})
                risk_level = r.get("level", "—") if isinstance(r, dict) else "—"
            cat = _cat_cell(entry)
            jira_key = "Not Found"
            if jira_tickets:
                jira_key = jira_tickets.get(key, "Not Found")
            lines.append(
                f"| {name} | {bucket} | {decision} | {risk_level} | {cat} | {jira_key} |"
            )
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
    def _time_and_effort(
        comparison: dict[str, Any],
        timings: dict[str, Any] | None,
    ) -> str:
        """Actual tool run-time + estimated effort to complete the upgrade."""
        timings = timings or {}
        compare_s = timings.get("compare_seconds")
        merge_s = timings.get("merge_seconds")
        eff = estimate_effort(comparison)

        lines = ["## Upgrade Time & Effort", ""]

        # ── Actual tool run-time (measured wall-clock) ──────────────────────
        if compare_s is not None or merge_s is not None:
            c = compare_s or 0
            m = merge_s or 0
            lines += [
                "**Tool run-time (measured)** — how long the tool itself took:",
                "",
                "| Stage | Duration |",
                "|-------|---------|",
                f"| Scan & compare | {_fmt_duration(c)} |",
                f"| Merge | {_fmt_duration(m)} |",
                f"| **Total tool time** | **{_fmt_duration(c + m)}** |",
                "",
            ]
        else:
            lines += [
                "_Tool run-time not recorded yet — run a scan/compare (and merge) "
                "to capture it._",
                "",
            ]

        # ── Estimated manual effort, without the tool (from CAT levels) ─────
        lines += [
            f"**Estimated manual effort (without the tool): ~{eff['total_days']} "
            f"person-days** (~{eff['total_hours']:.0f} h) to reconcile the "
            f"{eff['counted']} carried-forward artifact(s) by hand — vs the tool "
            "run-time above. Rough planning figure from each artifact's CAT level "
            "(CAT1 lowest → CAT5 highest effort); excludes Removed artifacts.",
            "",
            "| CAT | Effort | Artifacts | Est. manual hours |",
            "|-----|--------|----------:|------------------:|",
        ]
        for lvl in range(1, 6):
            row = eff["per_cat"][lvl]
            if row["count"]:
                lines.append(
                    f"| CAT{lvl} | {row['loe']} | {row['count']} | {row['hours']:g} |"
                )
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
    def _cat_distribution(comparison: dict[str, Any]) -> str:
        """CAT 1-5 configuration-severity distribution (E2open GTM scale)."""
        by_cat: dict[int, int] = {i: 0 for i in range(1, 6)}
        uf_yes = 0
        for entry in comparison.values():
            lvl = entry.get("cat_level")
            if isinstance(lvl, int) and 1 <= lvl <= 5:
                by_cat[lvl] += 1
                if entry.get("cat_upgrade_friendly"):
                    uf_yes += 1

        total = sum(by_cat.values())
        if total == 0:
            return ""

        lines = [
            "## CAT Distribution",
            "",
            "Configuration-severity classification (E2open GTM CAT 1-5). "
            f"**{uf_yes} of {total}** artifacts are upgrade-friendly.",
            "",
            "| CAT | Category | Count | % |",
            "|-----|----------|------:|--:|",
        ]
        for lvl in range(1, 6):
            count = by_cat[lvl]
            pct = f"{count / total * 100:.1f}" if total else "0"
            lines.append(f"| CAT{lvl} | {_CAT_NAMES[lvl]} | {count} | {pct}% |")

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
