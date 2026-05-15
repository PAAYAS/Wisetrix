"""
Streamlit UI for the Claude API-first upgrade agent (v2).

Multi-agent architecture with Git/Artifactory source providers,
quality gates, and risk scoring.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Make upgrade_lib importable when running via `streamlit run`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from upgrade_lib import (  # noqa: E402
    UpgradeClient,
    apply_business_rules,
    compare_artifact_local,
    QualityGate,
    RiskScorer,
    JiraClient,
    ReportGenerator,
)
from upgrade_lib.agents import ReviewAgent  # noqa: E402
from upgrade_lib.sources.local_provider import LocalProvider  # noqa: E402
from upgrade_lib.sources.git_provider import GitProvider  # noqa: E402
from upgrade_lib.sources.artifactory_provider import ArtifactoryProvider  # noqa: E402


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

PROJECTS_FILE = _ROOT / "projects.json"
RESULTS_DIR = _ROOT / "run_state"
RESULTS_DIR.mkdir(exist_ok=True)

TEXT_EXTS = {
    ".json", ".xml", ".java", ".js", ".jsp", ".groovy", ".txt",
    ".properties", ".yaml", ".yml", ".md", ".html", ".css",
}


# --------------------------------------------------------------------------- #
# Inline file I/O helpers
# --------------------------------------------------------------------------- #

def load_projects() -> dict:
    if PROJECTS_FILE.exists():
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    return {}


def save_projects(data: dict) -> None:
    PROJECTS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTS


def read_file_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except Exception:
            return None
    except Exception:
        return None


def read_artifact_files(artifact_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not artifact_dir.exists():
        return result
    for p in artifact_dir.rglob("*"):
        if p.is_file() and is_text_file(p):
            content = read_file_safe(p)
            if content is not None:
                rel = p.relative_to(artifact_dir).as_posix()
                result[rel] = content
    return result


def write_artifact_files(output_dir: Path, files: dict[str, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def zip_directory(src_dir: Path, arc_root: str | None = None) -> bytes:
    """Zip the contents of src_dir in-memory and return the bytes.

    arc_root: optional top-level directory name inside the zip (defaults to src_dir.name).
    """
    if not src_dir.exists():
        return b""
    root_name = arc_root if arc_root is not None else src_dir.name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in src_dir.rglob("*"):
            if path.is_file():
                rel = path.relative_to(src_dir)
                arcname = f"{root_name}/{rel.as_posix()}" if root_name else rel.as_posix()
                zf.write(path, arcname=arcname)
    return buf.getvalue()


SYSTEM_BUCKET_NAME = "SYSTEM"
ARTIFACT_INCLUDE_EXTS = {".json", ".xml", ".java", ".js", ".jsp"}
ARTIFACT_EXCLUDE_NAMES = {"component.info", "security.txt"}


def _looks_like_artifact_tree(root: Path) -> bool:
    if not root.is_dir():
        return False
    for _dp, _dn, files in os.walk(root):
        for f in files:
            if (
                Path(f).suffix.lower() in ARTIFACT_INCLUDE_EXTS
                and f not in ARTIFACT_EXCLUDE_NAMES
            ):
                return True
    return False


def detect_buckets(source_root: Path, fallback_bucket: str) -> list[tuple[str, Path]]:
    if not source_root.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(source_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if _looks_like_artifact_tree(child):
            found.append((child.name, child))
    if found:
        return found
    return [(fallback_bucket, source_root)]


def customer_buckets_for_project(source_root: Path, project_id: str) -> list[str]:
    buckets = detect_buckets(source_root, fallback_bucket=project_id)
    return [name for name, _p in buckets if name != SYSTEM_BUCKET_NAME]


def primary_customer_bucket(source_root: Path, project_id: str) -> str:
    customers = customer_buckets_for_project(source_root, project_id)
    return customers[0] if customers else project_id


def scan_artifacts(source_root: Path, project_id: str = "SOURCE") -> list[dict]:
    artifacts: list[dict] = []
    if not source_root.exists():
        return artifacts
    scan_roots = detect_buckets(source_root, fallback_bucket=project_id)
    seen_keys: set[str] = set()
    for bucket, bucket_root in scan_roots:
        for dirpath, _dirnames, filenames in os.walk(bucket_root):
            has_artifact_file = any(
                Path(f).suffix.lower() in ARTIFACT_INCLUDE_EXTS
                and f not in ARTIFACT_EXCLUDE_NAMES
                for f in filenames
            )
            if not has_artifact_file:
                continue
            rel = Path(dirpath).relative_to(bucket_root)
            parts = rel.parts
            if len(parts) < 2:
                continue
            rel_path = rel.as_posix()
            key = f"{bucket}::{rel_path}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            artifacts.append({
                "bucket": bucket,
                "category": parts[0],
                "subcategory": parts[1] if len(parts) > 2 else "",
                "name": parts[-1],
                "rel_path": rel_path,
                "source_rel": f"{bucket}/{rel_path}",
                "abs_path": dirpath,
            })
    artifacts.sort(key=lambda a: (a["bucket"], a["rel_path"]))
    return artifacts


def file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()


# --------------------------------------------------------------------------- #
# Source resolution helpers
# --------------------------------------------------------------------------- #

def resolve_project_paths(project: dict) -> dict:
    """
    Resolve project config into local paths, using the appropriate provider.
    Returns dict with resolved 'source_root', 'target_system', 'baseline_system'.
    Caches resolved paths in session state to avoid re-cloning/downloading.
    """
    cache_key = f"resolved_{json.dumps(project, sort_keys=True)}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    source_type = project.get("source_type", "local")
    target_type = project.get("target_type", "local")

    resolved = {}

    # Resolve source
    if source_type == "git":
        gp = GitProvider()
        git_result = gp.resolve(project)
        resolved["source_root"] = str(git_result.source_root)
        resolved["_git_metadata"] = git_result.metadata
    else:
        resolved["source_root"] = project.get("source_root", "")

    # Resolve target
    if target_type == "artifactory":
        ap = ArtifactoryProvider()
        art_result = ap.resolve({**project, "source_root": resolved["source_root"]})
        resolved["target_system"] = str(art_result.target_system)
        # Pull baseline from same artifactory result if URL was inlined
        resolved["baseline_system"] = (
            str(art_result.baseline_system) if art_result.baseline_system else ""
        )
        resolved["_art_metadata"] = art_result.metadata
    else:
        resolved["target_system"] = project.get("target_system", "")
        resolved["baseline_system"] = ""

    # Resolve baseline independently (target_type may be local while baseline is artifactory)
    baseline_type = project.get("baseline_type") or (
        "artifactory" if project.get("baseline_url") else
        ("local" if project.get("baseline_system") else "none")
    )
    if not resolved.get("baseline_system"):
        if baseline_type == "artifactory" and project.get("baseline_url"):
            ap = ArtifactoryProvider()
            baseline_cfg = {
                "artifactory_url": project["baseline_url"],
                "target_version": project.get("baseline_version", ""),
            }
            try:
                bl_result = ap.resolve(baseline_cfg)
                resolved["baseline_system"] = str(bl_result.target_system)
            except Exception as e:
                resolved["baseline_system"] = ""
                resolved["_baseline_error"] = str(e)
        elif baseline_type == "local":
            resolved["baseline_system"] = project.get("baseline_system", "")

    resolved["merge_output_dir"] = project.get("merge_output_dir", "")

    st.session_state[cache_key] = resolved
    return resolved


# --------------------------------------------------------------------------- #
# Run-state persistence
# --------------------------------------------------------------------------- #

def comparison_path(project_id: str) -> Path:
    return RESULTS_DIR / f"{project_id}.comparison.json"


def merge_report_path(project_id: str) -> Path:
    return RESULTS_DIR / f"{project_id}.merges.json"


def summary_path(project_id: str) -> Path:
    return RESULTS_DIR / f"{project_id}.summary.json"


def risk_path(project_id: str) -> Path:
    return RESULTS_DIR / f"{project_id}.risks.json"


def jira_tracker_path(project_id: str) -> Path:
    return RESULTS_DIR / f"{project_id}.jira_tracker.json"


def jira_tickets_path(project_id: str) -> Path:
    return RESULTS_DIR / f"{project_id}.jira_tickets.json"


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Session state / client
# --------------------------------------------------------------------------- #

@st.cache_resource
def get_client() -> UpgradeClient:
    return UpgradeClient()


@st.cache_resource
def get_quality_gate() -> QualityGate:
    return QualityGate()


@st.cache_resource
def get_risk_scorer() -> RiskScorer:
    return RiskScorer()


@st.cache_resource
def get_jira_client() -> JiraClient:
    return JiraClient()


@st.cache_resource
def get_report_generator() -> ReportGenerator:
    return ReportGenerator()


def init_session() -> None:
    st.session_state.setdefault("projects", load_projects())
    st.session_state.setdefault("current_project", None)
    st.session_state.setdefault("comparison_results", {})
    st.session_state.setdefault("merge_results", {})
    st.session_state.setdefault("chat_history", [])


# --------------------------------------------------------------------------- #
# Sidebar — project picker
# --------------------------------------------------------------------------- #

_NEW_PROJECT_LABEL = "+ New Project"


def render_sidebar() -> dict | None:
    st.sidebar.title("Yantrix")
    st.sidebar.caption("Multi-agent upgrade engine · Git + Artifactory + Quality Gates")

    projects = st.session_state.projects
    project_ids = list(projects.keys())

    # Always offer "+ New Project" at the top of the selector
    options = [_NEW_PROJECT_LABEL] + project_ids

    # Determine default index
    current = st.session_state.current_project
    if current and current in project_ids:
        default_idx = options.index(current)
    elif project_ids:
        default_idx = 1  # first real project
    else:
        default_idx = 0  # "+ New Project"

    selected = st.sidebar.selectbox("Select project", options, index=default_idx)

    if selected == _NEW_PROJECT_LABEL:
        st.session_state.current_project = None
    else:
        st.session_state.current_project = selected

    # ---- Project info ------------------------------------------------------
    if selected != _NEW_PROJECT_LABEL and selected in projects:
        proj = projects[selected]
        with st.sidebar.expander("Project Info"):
            src_type = proj.get("source_type", "local")
            tgt_type = proj.get("target_type", "local")
            st.caption(f"Source: **{src_type}**")
            if src_type == "git":
                st.caption(f"Repo: {proj.get('git_url', 'N/A')}")
                st.caption(f"Branch: {proj.get('git_branch', 'main')}")
            st.caption(f"Target: **{tgt_type}**")
            if tgt_type == "artifactory":
                st.caption(f"Version: {proj.get('target_version', 'N/A')}")

    return projects.get(selected) if selected != _NEW_PROJECT_LABEL else None


# --------------------------------------------------------------------------- #
# Risk display helpers
# --------------------------------------------------------------------------- #

def _risk_badge(level: str) -> str:
    colors = {"HIGH": "red", "MEDIUM": "orange", "LOW": "green"}
    return f":{colors.get(level, 'gray')}[{level}]"


def _verdict_badge(verdict: str) -> str:
    colors = {"PASS": "green", "WARN": "orange", "FAIL": "red"}
    return f":{colors.get(verdict, 'gray')}[{verdict}]"


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #

def tab_setup(project_id: str | None, projects: dict) -> None:
    is_new = project_id is None
    existing = projects.get(project_id, {}) if project_id else {}
    # Scope widget keys to project so switching projects re-loads values cleanly.
    ks = f"setup::{project_id or '__new__'}"

    if is_new:
        st.header("Create New Project")
        st.caption("Fill in the details below and click **Save Project**.")
    else:
        st.header(f"Edit Project — {project_id}")
        st.caption("Update settings or delete this project.")

    # Show existing projects as a quick reference
    if projects and is_new:
        with st.expander(f"Existing projects ({len(projects)})"):
            for pid, cfg in projects.items():
                src = cfg.get("source_type", "local")
                tgt = cfg.get("target_type", "local")
                ver = cfg.get("target_version", "")
                st.caption(f"**{pid}** — source: {src}, target: {tgt} {ver}")

    # Note: not using st.form so radio choices reactively reveal/hide inputs.
    pid = st.text_input(
        "Project ID",
        value="" if is_new else project_id,
        placeholder="e.g. ALDI, CEVA, DHL",
        disabled=not is_new,  # can't rename an existing project
        key=f"{ks}_pid",
    )

    st.subheader("Source (Customer Customizations)")
    src_options = ["git", "local"]
    src_default = src_options.index(existing.get("source_type", "git"))
    source_type = st.radio(
        "Source type", src_options, horizontal=True, index=src_default,
        key=f"{ks}_source_type",
    )

    if source_type == "git":
        git_url = st.text_input(
            "Git URL (Bitbucket)",
            value=existing.get("git_url", ""),
            placeholder="https://bitbucket.org/e2open/aldi_app.git",
            key=f"{ks}_git_url",
        )
        git_branch = st.text_input(
            "Branch", value=existing.get("git_branch", "main"), key=f"{ks}_git_branch",
        )
        source_subpath = st.text_input(
            "Subpath to repos",
            value=existing.get("source_subpath", "client_delivery/src/main/resources/app_root/repos"),
            help="Path from repo root to the artifact repos directory",
            key=f"{ks}_source_subpath",
        )
        source_root = ""
    else:
        git_url = ""
        git_branch = ""
        source_subpath = ""
        source_root = st.text_input(
            "Source root (local path)",
            value=existing.get("source_root", ""),
            placeholder="C:/GIT_REPO/ALDI/.../repos",
            key=f"{ks}_source_root",
        )

    st.subheader("Target SYSTEM (Upgrade Version)")
    tgt_options = ["artifactory", "local"]
    tgt_default = tgt_options.index(existing.get("target_type", "artifactory"))
    target_type = st.radio(
        "Target type", tgt_options, horizontal=True, index=tgt_default,
        key=f"{ks}_target_type",
    )

    if target_type == "artifactory":
        artifactory_url = st.text_input(
            "Artifactory URL",
            value=existing.get("artifactory_url", ""),
            placeholder="https://sv4.art.e2open.com/ui/native/gtm-release-dev/com/amberroad/solutions/gtm-install/26.2/",
            key=f"{ks}_artifactory_url",
        )
        target_version = st.text_input(
            "Target version", value=existing.get("target_version", "26.2"),
            key=f"{ks}_target_version",
        )
        target_system = ""
    else:
        artifactory_url = ""
        target_version = ""
        target_system = st.text_input(
            "Target SYSTEM root (local path)",
            value=existing.get("target_system", ""),
            key=f"{ks}_target_system",
        )

    st.subheader("Baseline SYSTEM (Common Ancestor)")
    st.caption(
        "Optional but recommended. The baseline is the original SYSTEM version "
        "the customer last upgraded from — used for true 3-way merges. "
        "Pick **artifactory** to download from JFrog or **local** to point at a folder."
    )
    bl_options = ["artifactory", "local", "none"]
    bl_default = 2  # none
    if existing.get("baseline_type") == "artifactory" or existing.get("baseline_url"):
        bl_default = 0
    elif existing.get("baseline_type") == "local" or existing.get("baseline_system"):
        bl_default = 1
    baseline_type = st.radio(
        "Baseline type", bl_options, horizontal=True, index=bl_default,
        key=f"{ks}_baseline_type",
    )

    if baseline_type == "artifactory":
        baseline_url = st.text_input(
            "Baseline Artifactory URL",
            value=existing.get("baseline_url", ""),
            placeholder="https://sv4.art.e2open.com/.../24.4.11/",
            key=f"{ks}_baseline_url",
        )
        baseline_version = st.text_input(
            "Baseline version",
            value=existing.get("baseline_version", "24.4.11"),
            key=f"{ks}_baseline_version",
        )
        baseline_system = ""
    elif baseline_type == "local":
        baseline_url = ""
        baseline_version = ""
        baseline_system = st.text_input(
            "Baseline SYSTEM root (local path)",
            value=existing.get("baseline_system", ""),
            key=f"{ks}_baseline_system",
        )
    else:
        baseline_url = ""
        baseline_version = ""
        baseline_system = ""

    # Output directory is auto-managed by the backend (./output/{project_id})
    merge_output = existing.get("merge_output_dir", f"./output/{pid}" if pid else "")

    # ---- JIRA project (optional) ----------------------------------------
    st.subheader("JIRA (optional)")
    jira_project_url = st.text_input(
        "JIRA project URL or key",
        value=existing.get("jira_project_url", ""),
        placeholder="https://jira.dev.e2open.com/jira/projects/ALDIBR1/issues  or  ALDIBR1",
        help="Used for automatic ticket lookup in the JIRA tab",
        key=f"{ks}_jira_project_url",
    )

    # ---- Buttons ---------------------------------------------------------
    if is_new:
        save_clicked = st.button("Create Project", type="primary", key=f"{ks}_create_btn")
        delete_clicked = False
    else:
        col1, col2 = st.columns(2)
        with col1:
            save_clicked = st.button("Save Changes", type="primary", key=f"{ks}_save_btn")
        with col2:
            delete_clicked = st.button("Delete Project", type="secondary", key=f"{ks}_delete_btn")

    if save_clicked and pid:
        project_config = {
            "source_type": source_type,
            "target_type": target_type,
            "merge_output_dir": merge_output or f"./output/{pid}",
        }
        if jira_project_url:
            project_config["jira_project_url"] = jira_project_url

        if source_type == "git":
            project_config.update({
                "git_url": git_url,
                "git_branch": git_branch,
                "source_subpath": source_subpath,
            })
        else:
            project_config["source_root"] = source_root

        if target_type == "artifactory":
            project_config.update({
                "artifactory_url": artifactory_url,
                "target_version": target_version,
            })
        else:
            project_config["target_system"] = target_system

        project_config["baseline_type"] = baseline_type
        if baseline_type == "artifactory":
            project_config.update({
                "baseline_url": baseline_url,
                "baseline_version": baseline_version,
            })
        elif baseline_type == "local":
            project_config["baseline_system"] = baseline_system

        projects[pid] = project_config
        st.session_state.projects = projects
        save_projects(projects)
        st.session_state.current_project = pid
        st.success(f"Project '{pid}' {'created' if is_new else 'updated'}.")
        st.rerun()
    elif save_clicked:
        st.warning("Project ID is required.")

    if delete_clicked:
        if pid and pid in projects:
            del projects[pid]
            st.session_state.projects = projects
            save_projects(projects)
            st.session_state.current_project = None
            st.success(f"Project '{pid}' deleted.")
            st.rerun()
        elif pid:
            st.warning(f"Project '{pid}' not found.")

    # Test connections
    if project_id and project_id in projects:
        proj = projects[project_id]
        st.divider()
        st.subheader("Test Connections")
        col1, col2 = st.columns(2)

        with col1:
            if proj.get("source_type") == "git" and proj.get("git_url"):
                if st.button("Test Git Connection"):
                    with st.spinner("Listing branches..."):
                        try:
                            gp = GitProvider()
                            branches = gp.list_branches(proj["git_url"])
                            st.success(f"Connected! Found {len(branches)} branches: {', '.join(branches[:10])}")
                        except Exception as e:
                            st.error(f"Git connection failed: {e}")

        with col2:
            if proj.get("target_type") == "artifactory" and proj.get("artifactory_url"):
                if st.button("Test Artifactory Connection"):
                    with st.spinner("Testing..."):
                        try:
                            ap = ArtifactoryProvider()
                            result = ap.test_connection(proj["artifactory_url"])
                            if result["success"]:
                                st.success(result["message"])
                            else:
                                st.error(result["message"])
                        except Exception as e:
                            st.error(f"Connection failed: {e}")


def tab_summary(project_id: str, project: dict) -> None:
    st.header("Summary")

    comp = load_json(comparison_path(project_id), {})
    if not comp:
        st.info("No comparison results yet. Use the **Scan & Compare** tab.")
        return

    total = len(comp)
    by_decision: dict[str, int] = {}
    for a in comp.values():
        by_decision[a.get("decision", "?")] = by_decision.get(a.get("decision", "?"), 0) + 1

    # Decision metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total artifacts", total)
    c2.metric("Merge", by_decision.get("Merge", 0))
    c3.metric("Retain", by_decision.get("Retain", 0))
    c4.metric("Remove", by_decision.get("Remove", 0))

    # Risk metrics
    risks = load_json(risk_path(project_id), {})
    if risks:
        st.divider()
        r1, r2, r3 = st.columns(3)
        risk_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for r in risks.values():
            level = r.get("level", "LOW") if isinstance(r, dict) else "LOW"
            risk_counts[level] = risk_counts.get(level, 0) + 1
        r1.metric("HIGH Risk", risk_counts["HIGH"])
        r2.metric("MEDIUM Risk", risk_counts["MEDIUM"])
        r3.metric("LOW Risk", risk_counts["LOW"])

    st.divider()

    saved = load_json(summary_path(project_id), {})
    btn_label = (
        "Regenerate narrative summary"
        if saved.get("content")
        else "Generate narrative summary"
    )
    if st.button(btn_label, type="primary"):
        with st.spinner("Agent is writing the summary..."):
            narrative = get_client().generate_summary_enhanced(
                comp,
                risk_assessments=risks if risks else None,
            )
        saved = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "content": narrative,
        }
        save_json(summary_path(project_id), saved)
        st.rerun()

    if saved.get("content"):
        st.caption(f"Last generated: {saved.get('generated_at', '')}")
        st.markdown(saved["content"])
    else:
        st.info("No saved summary yet — click the button above to generate one.")

    # ---- Report generation & download ----------------------------------------
    st.divider()
    st.subheader("Upgrade Report")

    merges = load_json(merge_report_path(project_id), {})
    jira_tix = load_json(jira_tickets_path(project_id), {})
    jira_state = load_json(jira_tracker_path(project_id), {})

    if st.button("Generate Upgrade Report", type="secondary"):
        with st.spinner("Generating upgrade report..."):
            rg = get_report_generator()
            try:
                resolved = resolve_project_paths(project)
            except Exception:
                resolved = {}

            meta = {
                "Project": project_id,
                "Source type": project.get("source_type", "local"),
                "Target version": project.get("target_version", "N/A"),
            }
            git_meta = resolved.get("_git_metadata")
            if git_meta:
                meta["Git branch"] = git_meta.get("branch", "N/A")
                meta["Git commit"] = git_meta.get("commit", "N/A")[:8]

            report_md = rg.generate(
                project_id=project_id,
                comparison=comp,
                merges=merges,
                risks=risks,
                summary_narrative=saved.get("content"),
                jira_state=jira_state if jira_state else None,
                metadata=meta,
                jira_tickets=jira_tix if jira_tix else None,
            )

            out_dir = Path(resolved.get("merge_output_dir") or project.get("merge_output_dir", f"./output/{project_id}"))
            report_path = rg.save(report_md, out_dir.parent if out_dir.name == project_id else out_dir, project_id)
            st.session_state[f"report_content_{project_id}"] = report_md
            # Stash the structured inputs so the PDF can be rendered directly.
            st.session_state[f"report_data_{project_id}"] = {
                "comparison": comp,
                "merges": merges,
                "risks": risks,
                "summary_narrative": saved.get("content"),
                "jira_state": jira_state if jira_state else None,
                "metadata": meta,
                "jira_tickets": jira_tix if jira_tix else None,
            }
        st.success(f"Report saved to `{report_path}`")

    report_content = st.session_state.get(f"report_content_{project_id}")
    report_data = st.session_state.get(f"report_data_{project_id}")
    if report_content:
        col1, col2 = st.columns(2)
        with col1:
            if report_data:
                try:
                    rg = get_report_generator()
                    pdf_bytes = rg.generate_pdf(
                        project_id=project_id,
                        **report_data,
                    )
                    st.download_button(
                        "Download PDF",
                        data=pdf_bytes,
                        file_name=f"UPGRADE_REPORT_{project_id}.pdf",
                        mime="application/pdf",
                        type="primary",
                    )
                except Exception as e:
                    st.error(f"PDF generation failed: {e}")
            else:
                st.info("Click 'Generate Upgrade Report' first to enable PDF download.")
        # with col2:
        #     st.download_button(
        #         "Download Markdown",
        #         data=report_content,
        #         file_name=f"UPGRADE_REPORT_{project_id}.md",
        #         mime="text/markdown",
        #     )
        with st.expander("Preview report"):
            st.markdown(report_content)


def tab_scan_compare(project_id: str, project: dict) -> None:
    st.header("Scan & Compare")

    # Resolve paths (handles git/artifactory/local)
    try:
        resolved = resolve_project_paths(project)
    except Exception as e:
        st.error(f"Failed to resolve project paths: {e}")
        st.info("Check your project configuration in the **Setup** tab.")
        return

    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])

    # Show resolved source info
    git_meta = resolved.get("_git_metadata")
    if git_meta:
        st.caption(
            f"Source: **Git** — branch `{git_meta.get('branch', 'N/A')}` "
            f"(commit: `{git_meta.get('commit', 'N/A')[:8]}`)"
        )

    if not source_root.exists():
        st.error(f"Source root not found: {source_root}")
        return

    artifacts = scan_artifacts(source_root, project_id=project_id)
    detected = sorted({a["bucket"] for a in artifacts})
    st.write(f"Found **{len(artifacts)}** artifacts in source.")
    if detected:
        st.caption(
            f"Detected buckets: {', '.join(detected)} · "
            "compare runs locally (deterministic, fast — no Claude calls)."
        )

    if st.button("Run Compare", type="primary"):
        targets = artifacts
        comp_results = load_json(comparison_path(project_id), {})
        st.info(f"Comparing {len(targets)} artifacts... please wait.")
        progress = st.progress(0.0)
        status = st.empty()

        for i, art in enumerate(targets, 1):
            key = art["source_rel"]
            rel = art["rel_path"]
            bucket = art["bucket"]
            status.text(f"[{i}/{len(targets)}] {key}")
            aldi_dir = source_root / bucket / rel
            sys_dir = target_root / rel

            meta = {
                "bucket": bucket,
                "category": art["category"],
                "name": art["name"],
                "rel_path": rel,
                "source_rel": key,
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                result = compare_artifact_local(aldi_dir, sys_dir, rel)
                comp_results[key] = {**meta, **result}
            except Exception as e:
                comp_results[key] = {**meta, "decision": "ERROR", "error": str(e)}
            progress.progress(i / len(targets))

        comp_results = apply_business_rules(comp_results)

        # Run risk assessment
        status.text("Assessing risk levels...")
        scorer = get_risk_scorer()
        risk_results = {}
        for key, result in comp_results.items():
            ra = scorer.assess(result, {"category": result.get("category", "")})
            risk_results[key] = {"level": ra.level, "score": ra.score, "factors": ra.factors}
            comp_results[key]["risk_level"] = ra.level
            comp_results[key]["risk_score"] = ra.score

        save_json(comparison_path(project_id), comp_results)
        save_json(risk_path(project_id), risk_results)
        st.session_state.comparison_results = comp_results
        progress.empty()
        status.empty()
        st.success(f"Compared {len(targets)} artifacts with risk assessment.")

    comp = load_json(comparison_path(project_id), {})
    if comp:
        # Filters
        col1, col2 = st.columns(2)
        with col1:
            decision_filter = st.multiselect(
                "Filter by decision", ["Merge", "Retain", "Remove", "ERROR"],
                default=[]
            )
        with col2:
            risk_filter = st.multiselect(
                "Filter by risk", ["HIGH", "MEDIUM", "LOW"],
                default=[]
            )

        filtered = comp
        if decision_filter:
            filtered = {k: v for k, v in filtered.items() if v.get("decision") in decision_filter}
        if risk_filter:
            filtered = {k: v for k, v in filtered.items() if v.get("risk_level") in risk_filter}

        # Load JIRA tickets if available
        jira_tix = load_json(jira_tickets_path(project_id), {})

        with st.expander(f"Results ({len(filtered)}/{len(comp)})", expanded=False):
            rows = [
                {
                    "bucket": r.get("bucket", ""),
                    "artifact": r.get("rel_path", k),
                    "decision": r.get("decision"),
                    "risk": r.get("risk_level", "—"),
                    "JIRA": jira_tix.get(k, "Not Found"),
                    "analysis": (r.get("analysis") or r.get("error") or "")[:100],
                }
                for k, r in filtered.items()
            ]
            st.dataframe(rows, use_container_width=True)


def _project_default_bucket(project: dict, project_id: str) -> str:
    resolved = resolve_project_paths(project)
    src = Path(resolved["source_root"])
    return primary_customer_bucket(src, project_id)


def _perform_merge(
    key: str,
    entry: dict,
    source_root: Path,
    target_root: Path,
    baseline_root: Path,
    out_root: Path,
    client: UpgradeClient,
    merges: dict,
    default_bucket: str,
    quality_gate: QualityGate | None = None,
    skip_diff: bool = False,
) -> dict:
    """Run a single artifact merge with quality gate."""
    rel = entry.get("rel_path", key)
    bucket = entry.get("bucket") or default_bucket
    aldi_files = read_artifact_files(source_root / bucket / rel)
    system_files = read_artifact_files(target_root / rel)
    baseline_files = (
        read_artifact_files(baseline_root / rel)
        if baseline_root and str(baseline_root) and baseline_root.exists()
        else {}
    )

    merge_res = client.merge_artifact(
        aldi_files, system_files, baseline_files, customer=bucket
    )

    out_dir = out_root / bucket / rel
    write_artifact_files(out_dir, merge_res["merged_files"])

    # Quality gate check (deterministic)
    quality_result = None
    if quality_gate:
        qr = quality_gate.check(merge_res["merged_files"])
        quality_result = qr.to_dict()

    # Auto-regenerate _diff.json for JSON-heavy artifacts
    # Skipped during Merge All for speed — can be generated later per-artifact
    diff_info = None
    diff_error: str | None = None
    if not skip_diff:
        for fname, content in merge_res["merged_files"].items():
            if fname.endswith(".json") and not fname.endswith("_diff.json"):
                sys_content = system_files.get(fname)
                if sys_content:
                    try:
                        artifact_id = Path(rel).name
                        diff_info = client.generate_diff_json(
                            content, sys_content, artifact_id,
                            merged_name=fname, system_name=fname,
                        )
                        diff_name = fname.replace(".json", "_diff.json")
                        (out_dir / diff_name).write_text(
                            diff_info["diff_json"], encoding="utf-8"
                        )
                    except Exception as e:
                        diff_error = str(e)
                break

    record = {
        "bucket": bucket,
        "rel_path": rel,
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "files": list(merge_res["merged_files"].keys()),
        "explanation": merge_res.get("explanation", ""),
        "diff_generated": diff_info is not None,
        "diff_error": diff_error,
        "out_dir": str(out_dir),
        "quality_result": quality_result,
    }
    merges[key] = record
    return record


def tab_merge_queue(project_id: str, project: dict) -> None:
    st.header("Merge Queue")

    comp = load_json(comparison_path(project_id), {})
    merges = load_json(merge_report_path(project_id), {})
    risks = load_json(risk_path(project_id), {})
    if not comp:
        st.info("Run Scan & Compare first.")
        return

    pending = {
        rel: r for rel, r in comp.items()
        if r.get("decision") == "Merge" and rel not in merges
    }
    done = {rel: r for rel, r in merges.items()}

    try:
        resolved = resolve_project_paths(project)
    except Exception as e:
        st.error(f"Failed to resolve paths: {e}")
        return

    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])
    baseline_root = Path(resolved.get("baseline_system") or "")
    out_root = Path(resolved.get("merge_output_dir") or project.get("merge_output_dir", ""))

    default_bucket = _project_default_bucket(project, project_id)

    # Use empty containers so metrics update live during merge
    metrics_row = st.columns(3)
    pending_metric = metrics_row[0].empty()
    merged_metric = metrics_row[1].empty()
    total_metric = metrics_row[2].empty()

    def _update_metrics():
        pending_metric.metric("Pending", len(pending))
        merged_metric.metric("Merged", len(done) + len(merges) - len(done))
        total_metric.metric("Total Merge candidates", len(pending) + len(done))

    _update_metrics()

    # ── Last single-merge download (post-rerun) ───────────────────────────
    last_zip = st.session_state.get(f"last_merged_zip_{project_id}")
    if last_zip and last_zip.get("bytes"):
        safe_key = last_zip["key"].replace("/", "_").replace("\\", "_")
        st.download_button(
            label=f"Download last merged: {last_zip['key']} (.zip)",
            data=last_zip["bytes"],
            file_name=f"{safe_key}.zip",
            mime="application/zip",
            key="download_last_merged",
        )

    # ── Download all merged output ────────────────────────────────────────
    if out_root and out_root.exists() and any(out_root.rglob("*")):
        try:
            zip_bytes = zip_directory(out_root, arc_root=project_id)
            st.download_button(
                label=f"Download all merged ({project_id}.zip)",
                data=zip_bytes,
                file_name=f"{project_id}-merged.zip",
                mime="application/zip",
                key="download_all_merged",
            )
        except Exception as e:
            st.caption(f"(Could not build merged zip: {e})")

    # ── Merge All ──────────────────────────────────────────────────────────
    if pending:
        all_col1, all_col2 = st.columns([1, 4])
        with all_col1:
            run_all = st.button(
                f"Merge All ({len(pending)})",
                type="primary",
                key="merge_all_btn",
            )
        with all_col2:
            st.caption(
                "Runs every pending artifact through Claude with quality gates. "
                "Each merge is persisted as it completes — safe to interrupt."
            )

        if run_all:
            client = get_client()
            qg = get_quality_gate()
            st.info(f"Merging {len(pending)} artifacts through Claude... this may take several minutes.")
            progress = st.progress(0.0)
            status = st.empty()
            keys = list(pending.keys())
            failed: list[tuple[str, str]] = []
            succeeded = 0
            for i, key in enumerate(keys, 1):
                risk_info = risks.get(key, {})
                risk_level = risk_info.get("level", "—")
                status.text(f"[{i}/{len(keys)}] [{risk_level}] {key}")
                try:
                    _perform_merge(
                        key, pending[key],
                        source_root, target_root, baseline_root, out_root,
                        client, merges, default_bucket, quality_gate=qg,
                        skip_diff=True,  # skip diff generation for speed
                    )
                    save_json(merge_report_path(project_id), merges)
                    succeeded += 1
                    # Update metrics live
                    merged_metric.metric("Merged", len(done) + succeeded)
                    pending_metric.metric("Pending", len(pending) - i + len(failed))
                except Exception as e:
                    failed.append((key, str(e)))
                progress.progress(i / len(keys))
            progress.empty()
            status.empty()
            ok = len(keys) - len(failed)
            if failed:
                # Persist failures so they survive rerun
                st.session_state[f"merge_failures_{project_id}"] = failed
                st.warning(f"Merged {ok}/{len(keys)} — {len(failed)} failed.")
                with st.expander("Failures", expanded=True):
                    for k, err in failed:
                        st.error(f"**{k}** — {err}")
                if ok == 0:
                    st.error("All merges failed. Check Claude CLI connectivity.")
                    # Don't rerun — keep errors visible
                else:
                    st.rerun()
            else:
                st.session_state.pop(f"merge_failures_{project_id}", None)
                st.success(f"Merged all {ok} pending artifacts.")
                st.rerun()

    # ── Show persisted failures from previous Merge All run ──────────────
    prev_failures = st.session_state.get(f"merge_failures_{project_id}")
    if prev_failures:
        with st.expander(f"Previous merge failures ({len(prev_failures)})", expanded=True):
            for k, err in prev_failures:
                st.error(f"**{k}** — {err}")
            if st.button("Dismiss", key="dismiss_merge_failures"):
                st.session_state.pop(f"merge_failures_{project_id}", None)
                st.rerun()

    # ── Pending list with per-row Merge button ─────────────────────────────
    st.subheader(f"Pending ({len(pending)})")
    if not pending:
        st.info("No pending merges.")
    else:
        h1, h2, h3, h4, h5 = st.columns([1, 2, 3, 1, 1])
        h1.markdown("**Risk**")
        h2.markdown("**Artifact**")
        h3.markdown("**Analysis**")
        h4.markdown("**Bucket**")
        h5.markdown("**Action**")

        for key, entry in pending.items():
            rel = entry.get("rel_path", key)
            bucket = entry.get("bucket") or default_bucket
            analysis = (entry.get("analysis") or "")[:100]
            risk_level = entry.get("risk_level", risks.get(key, {}).get("level", "—"))

            r1, r2, r3, r4, r5 = st.columns([1, 2, 3, 1, 1])
            r1.markdown(_risk_badge(risk_level))
            r2.write(rel)
            r3.caption(analysis)
            r4.write(bucket)
            clicked = r5.button("Merge", key=f"merge_btn::{key}")
            if clicked:
                client = get_client()
                qg = get_quality_gate()
                with st.spinner(f"Claude is merging {key}..."):
                    try:
                        rec = _perform_merge(
                            key, entry,
                            source_root, target_root, baseline_root, out_root,
                            client, merges, default_bucket, quality_gate=qg,
                        )
                        save_json(merge_report_path(project_id), merges)
                        qr = rec.get("quality_result", {})
                        verdict = qr.get("verdict", "N/A") if qr else "N/A"
                        st.success(f"Merged {_verdict_badge(verdict)} → {rec['out_dir']}")
                        if rec.get("diff_error"):
                            st.warning(f"_diff.json regeneration failed: {rec['diff_error']}")
                        # Stash merged-artifact zip so it survives the rerun
                        try:
                            art_dir = Path(rec["out_dir"])
                            st.session_state[f"last_merged_zip_{project_id}"] = {
                                "key": key,
                                "bytes": zip_directory(art_dir, arc_root=art_dir.name),
                            }
                        except Exception:
                            pass
                        st.rerun()
                    except Exception as e:
                        st.error(f"Merge failed: {e}")

    # ── Already-merged list ────────────────────────────────────────────────
    if done:
        with st.expander(f"Completed ({len(done)})", expanded=False):
            for key, entry in done.items():
                qr = entry.get("quality_result", {})
                verdict = qr.get("verdict", "—") if qr else "—"
                cols = st.columns([1, 3, 3, 2, 1])
                cols[0].markdown(_verdict_badge(verdict))
                cols[1].write(f"**{key}**")
                cols[2].caption(
                    (entry.get("explanation") or "")[:100] or "(no explanation)"
                )
                cols[3].caption(entry.get("merged_at", ""))
                art_dir = Path(entry.get("out_dir", ""))
                if art_dir.exists():
                    safe_key = key.replace("/", "_").replace("\\", "_")
                    try:
                        zip_bytes = zip_directory(art_dir, arc_root=art_dir.name)
                        cols[4].download_button(
                            label="ZIP",
                            data=zip_bytes,
                            file_name=f"{safe_key}.zip",
                            mime="application/zip",
                            key=f"dl_done::{key}",
                        )
                    except Exception:
                        pass


def tab_diff_viewer(project_id: str, project: dict) -> None:
    st.header("Diff Viewer")

    merges = load_json(merge_report_path(project_id), {})
    if not merges:
        st.info("No merged artifacts yet.")
        return

    key = st.selectbox("Artifact", list(merges.keys()))
    if not key:
        return

    entry = merges[key]
    rel = entry.get("rel_path", key)
    bucket = entry.get("bucket") or _project_default_bucket(project, project_id)

    try:
        resolved = resolve_project_paths(project)
    except Exception as e:
        st.error(f"Failed to resolve paths: {e}")
        return

    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])
    out_root = Path(resolved.get("merge_output_dir") or project.get("merge_output_dir", ""))

    aldi_files = read_artifact_files(source_root / bucket / rel)
    system_files = read_artifact_files(target_root / rel)
    merged_files = read_artifact_files(out_root / bucket / rel)

    all_names = sorted(set(aldi_files) | set(system_files) | set(merged_files))
    fname = st.selectbox("File", all_names)

    if fname:
        c1, c2, c3 = st.columns(3)
        with c1:
            st.caption(bucket)
            st.code(aldi_files.get(fname, "(absent)"), language=None)
        with c2:
            st.caption("Merged")
            st.code(merged_files.get(fname, "(absent)"), language=None)
        with c3:
            st.caption("SYSTEM 26.2")
            st.code(system_files.get(fname, "(absent)"), language=None)


def tab_review(project_id: str, project: dict) -> None:
    st.header("Review & Edit")

    merges = load_json(merge_report_path(project_id), {})
    if not merges:
        st.info("No merged artifacts yet.")
        return

    key = st.selectbox("Artifact to review", list(merges.keys()))
    if not key:
        return

    entry = merges[key]
    rel = entry.get("rel_path", key)
    bucket = entry.get("bucket") or _project_default_bucket(project, project_id)

    try:
        resolved = resolve_project_paths(project)
    except Exception as e:
        st.error(f"Failed to resolve paths: {e}")
        return

    source_root = Path(resolved["source_root"])
    target_root = Path(resolved["target_system"])
    out_root = Path(resolved.get("merge_output_dir") or project.get("merge_output_dir", ""))

    aldi_files = read_artifact_files(source_root / bucket / rel)
    system_files = read_artifact_files(target_root / rel)
    merged_files = read_artifact_files(out_root / bucket / rel)

    # Show existing quality gate result
    qr = entry.get("quality_result")
    if qr:
        verdict = qr.get("verdict", "N/A")
        st.markdown(f"**Quality Gate**: {_verdict_badge(verdict)}")
        findings = qr.get("findings", [])
        if findings:
            for f in findings:
                sev = f.get("severity", "INFO")
                icon = {"ERROR": "!!!", "WARNING": "!!", "INFO": "i"}.get(sev, "")
                st.caption(f"[{sev}] {f.get('category', '')} — {f.get('file', '')}: {f.get('message', '')}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Run Claude review (structured)", type="primary"):
            with st.spinner("Claude is reviewing..."):
                review_result = get_client().review_merge_structured(
                    merged_files, aldi_files, system_files, customer=bucket
                )
            st.markdown(f"**Verdict**: {_verdict_badge(review_result.get('verdict', 'N/A'))}")
            st.markdown(f"**Summary**: {review_result.get('summary', '')}")
            if review_result.get("findings"):
                st.subheader("Findings")
                for f in review_result["findings"]:
                    st.write(f"- [{f.get('severity')}] **{f.get('category')}** in `{f.get('file')}`: {f.get('message')}")
            if review_result.get("recommendations"):
                st.subheader("Recommendations")
                for r in review_result["recommendations"]:
                    st.write(f"- {r}")

    with col2:
        if st.button("Run Claude review (markdown)"):
            with st.spinner("Claude is reviewing..."):
                report = get_client().review_merge(merged_files, aldi_files, system_files, customer=bucket)
            st.markdown(report)

    st.divider()
    st.subheader("Inline edit")
    fname = st.selectbox("File", list(merged_files.keys()), key="edit_file")
    if fname:
        current = merged_files[fname]
        edited = st.text_area("Content", value=current, height=400)
        if st.button("Save edit") and edited != current:
            (out_root / bucket / rel / fname).write_text(edited, encoding="utf-8")
            st.success("Saved.")


def tab_chat(project_id: str, project: dict) -> None:
    st.header("Chat")

    for role, msg in st.session_state.chat_history:
        with st.chat_message(role):
            st.markdown(msg)

    if q := st.chat_input("Ask about any artifact, policy, or merge result"):
        st.session_state.chat_history.append(("user", q))
        with st.chat_message("user"):
            st.markdown(q)

        comp = load_json(comparison_path(project_id), {})
        merges = load_json(merge_report_path(project_id), {})
        risks = load_json(risk_path(project_id), {})
        context = json.dumps(
            {
                "comparison_summary": {k: v.get("decision") for k, v in comp.items()},
                "merges_done": list(merges.keys()),
                "risk_levels": {k: v.get("level") for k, v in risks.items()} if risks else {},
            },
            indent=2,
        )

        with st.chat_message("assistant"):
            with st.spinner("Claude is thinking..."):
                answer = get_client().chat(q, context=context)
            st.markdown(answer)
        st.session_state.chat_history.append(("assistant", answer))


def tab_jira(project_id: str, project: dict) -> None:
    st.header("JIRA Integration")

    jira = get_jira_client()

    if not jira.enabled:
        st.warning(
            "JIRA is disabled. Enable it in `config.yaml` / `config.yml`:\n\n"
            "```yaml\njira:\n  enabled: true\n  base_url: \"https://jira.dev.e2open.com/jira\"\n"
            "  username: \"your_username\"\n  password: \"your_password\"\n```"
        )
        return

    # ---- Connection test -----------------------------------------------------
    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("Test Connection"):
            with st.spinner("Testing JIRA connection..."):
                result = jira.test_connection()
            if result["success"]:
                st.success(result["message"])
            else:
                st.error(result["message"])
    with col2:
        st.caption(f"Server: {jira.base_url}")

    st.divider()

    # ---- JIRA ticket lookup for artifacts ------------------------------------
    st.subheader("Artifact Ticket Lookup")

    comp = load_json(comparison_path(project_id), {})
    jira_tix = load_json(jira_tickets_path(project_id), {})

    if comp:
        lookup_method = st.radio(
            "Lookup method",
            ["JIRA Project URL", "Git commit messages"],
            horizontal=True,
            help="**JIRA Project URL**: Fetches all tickets from a JIRA project and matches by artifact name. "
                 "**Git commit messages**: Extracts JIRA keys (e.g. ALDIBR1-123) from git history.",
        )

        if lookup_method == "JIRA Project URL":
            jira_project_url = st.text_input(
                "JIRA project URL or key",
                value=project.get("jira_project_url", ""),
                placeholder="https://jira.dev.e2open.com/jira/projects/ALDIBR1/issues  or  ALDIBR1",
            )
            if st.button("Lookup from JIRA Project", type="primary") and jira_project_url:
                jira_project_key = jira.extract_project_key_from_url(jira_project_url)
                if not jira_project_key:
                    st.error(f"Could not extract project key from: {jira_project_url}")
                else:
                    with st.spinner(f"Fetching issues from {jira_project_key} and matching to {len(comp)} artifacts..."):
                        artifact_keys = list(comp.keys())
                        matches = jira.match_issues_to_artifacts(jira_project_key, artifact_keys)
                        jira_tix.update(matches)

                    save_json(jira_tickets_path(project_id), jira_tix)
                    found = sum(1 for v in jira_tix.values() if v != "Not Found")
                    st.success(f"Matched {found}/{len(jira_tix)} artifacts to JIRA tickets from {jira_project_key}.")
                    st.rerun()

        else:  # Git commit messages
            if project.get("source_type") != "git":
                st.warning("Git commit lookup requires a Git source. Configure Git in the **Setup** tab.")
            else:
                if st.button("Extract JIRA keys from Git", type="primary"):
                    git_url = project.get("git_url", "")
                    git_branch = project.get("git_branch", "main")
                    with st.spinner(f"Scanning git history on `{git_branch}` for JIRA keys..."):
                        try:
                            gp = GitProvider()
                            subpath = project.get("source_subpath", "client_delivery/src/main/resources/app_root/repos")
                            art_paths = []
                            for key, entry in comp.items():
                                bucket = entry.get("bucket", "")
                                rel = entry.get("rel_path", "")
                                if bucket and rel:
                                    art_paths.append(f"{subpath}/{bucket}/{rel}")

                            path_to_keys = gp.extract_jira_keys_for_paths(
                                git_url, git_branch, art_paths,
                            )

                            for key, entry in comp.items():
                                bucket = entry.get("bucket", "")
                                rel = entry.get("rel_path", "")
                                full_path = f"{subpath}/{bucket}/{rel}"
                                found_keys = path_to_keys.get(full_path, set())
                                if found_keys:
                                    jira_tix[key] = ", ".join(sorted(found_keys))

                            save_json(jira_tickets_path(project_id), jira_tix)
                            found = sum(1 for v in jira_tix.values() if v != "Not Found")
                            st.success(f"Found JIRA keys for {found}/{len(comp)} artifacts from git history.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Git scan failed: {e}")

        # Show current mapping
        if jira_tix:
            found = sum(1 for v in jira_tix.values() if v != "Not Found")
            st.caption(f"{found} of {len(jira_tix)} artifacts have linked JIRA tickets")
            with st.expander("Artifact → JIRA Mapping", expanded=False):
                rows = [
                    {
                        "artifact": k.split("/")[-1] if "/" in k else k,
                        "JIRA": v,
                        "full_key": k,
                    }
                    for k, v in sorted(jira_tix.items())
                ]
                st.dataframe(rows, use_container_width=True)
    else:
        st.info("Run Scan & Compare first to populate artifacts.")

    st.divider()

    # ---- Ticket detail viewer ------------------------------------------------
    st.subheader("Ticket Details")
    st.caption("View a JIRA ticket's summary, status, and discussion.")

    # Build list of known ticket keys from lookup results
    known_keys = sorted({
        k.strip()
        for v in jira_tix.values()
        if v and v != "Not Found"
        for k in v.split(",")
    })

    ticket_input = st.text_input(
        "JIRA ticket key",
        placeholder="e.g. ALDIBR1-42",
    )
    if known_keys:
        st.caption(f"Known tickets from lookup: {', '.join(known_keys[:20])}")

    if ticket_input and ticket_input.strip():
        ticket_key = ticket_input.strip()
        with st.spinner(f"Fetching {ticket_key}..."):
            details = jira.get_issue_details(ticket_key)

        if details:
            st.markdown(f"### {details['key']} — {details['summary']}")
            c1, c2, c3 = st.columns(3)
            c1.metric("Status", details["status"])
            c2.metric("Type", details["issue_type"])
            c3.metric("Assignee", details["assignee"] or "Unassigned")

            comments = details.get("comments", [])
            if comments:
                st.markdown(f"**Discussion** ({len(comments)} comments)")
                for i, c in enumerate(comments, 1):
                    with st.expander(f"Comment {i} — {c['author']} ({c['created'][:10] if len(c['created']) >= 10 else c['created']})"):
                        st.text(c["body"])
            else:
                st.info("No comments on this ticket.")

            st.caption(f"[Open in JIRA]({details['url']})")
        else:
            st.error(f"Could not fetch {ticket_key}. Check the key and JIRA connection.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    st.set_page_config(page_title="Yantrix", layout="wide")
    init_session()

    project = render_sidebar()
    project_id = st.session_state.current_project
    projects = st.session_state.projects

    if not project or not project_id:
        st.title("Yantrix — New Project")
        tab_setup(None, projects)
        return

    st.title(f"Yantrix — {project_id}")

    tabs = st.tabs([
        "Summary",
        "Setup",
        "Scan & Compare",
        "Merge Queue",
        "Diff Viewer",
        "Review & Edit",
        "JIRA",
        "Chat",
    ])

    with tabs[0]:
        tab_summary(project_id, project)
    with tabs[1]:
        tab_setup(project_id, projects)
    with tabs[2]:
        tab_scan_compare(project_id, project)
    with tabs[3]:
        tab_merge_queue(project_id, project)
    with tabs[4]:
        tab_diff_viewer(project_id, project)
    with tabs[5]:
        tab_review(project_id, project)
    with tabs[6]:
        tab_jira(project_id, project)
    with tabs[7]:
        tab_chat(project_id, project)


if __name__ == "__main__":
    main()
