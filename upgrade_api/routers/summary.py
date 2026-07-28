"""Summary + Upgrade Report endpoints.

GET  /projects/{id}/summary                  -> aggregates + saved narrative
POST /projects/{id}/summary/narrative        -> regenerate Claude narrative
POST /projects/{id}/report                   -> build & save UPGRADE_REPORT.md
GET  /projects/{id}/report/pdf               -> stream the rendered PDF
GET  /projects/{id}/report/markdown          -> stream the markdown
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from upgrade_api.paths import (
    comparison_path,
    jira_tickets_path,
    load_json,
    merge_report_path,
    risk_path,
    save_json,
    summary_path,
    timings_path,
)
from upgrade_api.scan_util import resolve_project_paths
from upgrade_api.state import load_projects
from upgrade_lib.claude_client import UpgradeClient
from upgrade_lib.report import ReportGenerator


router = APIRouter(prefix="/projects/{project_id}", tags=["summary"])


def _project_or_404(project_id: str) -> dict:
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return projects[project_id]


def _aggregate(project_id: str) -> dict:
    comp = load_json(comparison_path(project_id), {})
    risks = load_json(risk_path(project_id), {})
    merges = load_json(merge_report_path(project_id), {})

    decision_counts: dict[str, int] = {}
    for r in comp.values():
        d = r.get("decision", "?")
        decision_counts[d] = decision_counts.get(d, 0) + 1

    risk_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for r in risks.values():
        lvl = r.get("level", "LOW") if isinstance(r, dict) else "LOW"
        risk_counts[lvl] = risk_counts.get(lvl, 0) + 1

    verdict_counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for rec in merges.values():
        qr = rec.get("quality_result") or {}
        v = qr.get("verdict")
        if v in verdict_counts:
            verdict_counts[v] += 1

    return {
        "total_artifacts": len(comp),
        "decision_counts": decision_counts,
        "risk_counts": risk_counts,
        "merged_count": len(merges),
        "verdict_counts": verdict_counts,
    }


@router.get("/summary")
def get_summary(project_id: str) -> dict:
    _project_or_404(project_id)
    saved = load_json(summary_path(project_id), {})
    return {
        "metrics": _aggregate(project_id),
        "narrative": saved,
    }


@router.post("/summary/narrative")
async def regenerate_narrative(project_id: str) -> dict:
    _project_or_404(project_id)
    comp = load_json(comparison_path(project_id), {})
    if not comp:
        raise HTTPException(400, "No comparison results yet — run scan & compare first")
    risks = load_json(risk_path(project_id), {})

    client = UpgradeClient()
    narrative = await anyio.to_thread.run_sync(
        lambda: client.generate_summary_enhanced(
            comp,
            risk_assessments=risks if risks else None,
            project_id=project_id,
        )
    )
    saved = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content": narrative,
    }
    save_json(summary_path(project_id), saved)
    return saved


def _build_report_inputs(project_id: str, project: dict) -> dict:
    comp = load_json(comparison_path(project_id), {})
    risks = load_json(risk_path(project_id), {})
    merges = load_json(merge_report_path(project_id), {})
    jira_tix = load_json(jira_tickets_path(project_id), {})
    saved_summary = load_json(summary_path(project_id), {})

    try:
        resolved = resolve_project_paths(project)
    except Exception:
        resolved = {}

    metadata: dict[str, str] = {
        "Project": project_id,
        "Source type": project.get("source_type", "local"),
        "Target version": project.get("target_version", "N/A"),
    }
    git_meta = resolved.get("_git_metadata")
    if git_meta:
        metadata["Git branch"] = git_meta.get("branch", "N/A")
        metadata["Git commit"] = str(git_meta.get("commit", "N/A"))[:8]

    return {
        "comparison": comp,
        "merges": merges,
        "risks": risks,
        "summary_narrative": saved_summary.get("content"),
        "metadata": metadata,
        "jira_tickets": jira_tix if jira_tix else None,
        "timings": load_json(timings_path(project_id), {}),
        "resolved": resolved,
    }


@router.post("/report")
def build_report(project_id: str) -> dict:
    project = _project_or_404(project_id)
    if not load_json(comparison_path(project_id), {}):
        raise HTTPException(400, "No comparison results yet — run scan & compare first")
    inputs = _build_report_inputs(project_id, project)

    rg = ReportGenerator()
    report_md = rg.generate(
        project_id=project_id,
        comparison=inputs["comparison"],
        merges=inputs["merges"],
        risks=inputs["risks"],
        summary_narrative=inputs["summary_narrative"],
        metadata=inputs["metadata"],
        jira_tickets=inputs["jira_tickets"],
        timings=inputs["timings"],
    )

    resolved = inputs["resolved"]
    out_dir_str = resolved.get("merge_output_dir") or project.get(
        "merge_output_dir", f"./output/{project_id}"
    )
    out_dir = Path(out_dir_str)
    # mirror Streamlit: save into parent if the dir already ends with the project id
    target_dir = out_dir.parent if out_dir.name == project_id else out_dir
    saved_path = rg.save(report_md, target_dir, project_id)
    return {"path": str(saved_path), "bytes": len(report_md.encode("utf-8"))}


def _zip_filename_header(name: str) -> dict:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


@router.get("/report/pdf")
def report_pdf(project_id: str) -> Response:
    project = _project_or_404(project_id)
    if not load_json(comparison_path(project_id), {}):
        raise HTTPException(400, "No comparison results yet")
    inputs = _build_report_inputs(project_id, project)
    pdf = ReportGenerator.generate_pdf(
        project_id=project_id,
        comparison=inputs["comparison"],
        merges=inputs["merges"],
        risks=inputs["risks"],
        summary_narrative=inputs["summary_narrative"],
        metadata=inputs["metadata"],
        jira_tickets=inputs["jira_tickets"],
        timings=inputs["timings"],
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers=_zip_filename_header(f"UPGRADE_REPORT_{project_id}.pdf"),
    )


@router.get("/report/markdown")
def report_markdown(project_id: str) -> Response:
    project = _project_or_404(project_id)
    if not load_json(comparison_path(project_id), {}):
        raise HTTPException(400, "No comparison results yet")
    inputs = _build_report_inputs(project_id, project)
    rg = ReportGenerator()
    md = rg.generate(
        project_id=project_id,
        comparison=inputs["comparison"],
        merges=inputs["merges"],
        risks=inputs["risks"],
        summary_narrative=inputs["summary_narrative"],
        metadata=inputs["metadata"],
        jira_tickets=inputs["jira_tickets"],
        timings=inputs["timings"],
    )
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers=_zip_filename_header(f"UPGRADE_REPORT_{project_id}.md"),
    )
