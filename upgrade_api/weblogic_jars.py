"""Rule 12 orchestration — WEB-INF/lib source-JAR reconciliation.

Ties the pure logic in ``upgrade_lib.weblogic.jar_reconcile`` to the live
sources: the git clone (for commit→JIRA evidence) and the JIRA API (for
resolution + fix versions). Produces comparison-result entries so the lib jars
appear alongside the other artifacts with a decision:

    Remove  — a related TA/PD JIRA is Resolved=Fixed with a Fix Version in
              (curVer, targetVer]; the fix is already in the target.
    Retain  — no such evidence; kept and flagged ``core_warning`` (report to Core).

Best-effort: any failure (no git history, JIRA disabled/unreachable) degrades to
a Retain+warning rather than breaking the scan. Lives in upgrade_api because it
depends on the provider + JIRA layers.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from upgrade_lib.weblogic import layout as L
from upgrade_lib.weblogic.jar_reconcile import (
    decide_from_jira,
    diff_java_trees,
    parse_jar_folder,
    pick_target_version,
    repo_base,
    sources_url,
)

_log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 60

# JIRA projects that carry the authoritative Resolution + Fix Versions for a
# WEB-INF/lib fix. A commit references a dev ticket (e.g. DOO-387) whose Issue
# Links point at one of these. Overridable per project via "jira_fix_projects".
_DEFAULT_FIX_PROJECTS = {"TA", "PDSUPPORT"}


def _project_of(issue_key: str) -> str:
    return issue_key.split("-", 1)[0] if "-" in issue_key else ""


def _artifactory_auth() -> tuple | None:
    """Artifactory basic-auth from config.yaml (target downloads). Nexus
    (baseline) is public and uses no auth."""
    try:
        from upgrade_lib.sources.artifactory_provider import (
            _load_artifactory_credentials,
        )

        creds = _load_artifactory_credentials()
        if creds.get("username") and creds.get("password"):
            return (creds["username"], creds["password"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_java_from_jar(url: str, auth: tuple | None) -> dict[str, str] | None:
    """Download a ``-sources.jar`` and return {relpath: content} for its .java
    files. Returns None on any failure (best-effort)."""
    if requests is None:
        return None
    try:
        resp = requests.get(url, auth=auth, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        out: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if name.endswith(".java"):
                    try:
                        out[name] = zf.read(name).decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        continue
        return out
    except Exception as e:  # noqa: BLE001
        _log.warning("rule12: failed to download/extract %s: %s", url, e)
        return None


def _list_artifactory_versions(
    art_base: str, artifact_id: str, auth: tuple | None
) -> list[str]:
    """List available version folders for an artifact via the Artifactory
    Storage API. Returns [] on failure."""
    if requests is None:
        return []
    # https://host/artifactory/<repo>/...  ->  https://host/artifactory/api/storage/<repo>/...
    storage = art_base.replace("/artifactory/", "/artifactory/api/storage/", 1)
    from upgrade_lib.weblogic.jar_reconcile import GROUP_PATH

    url = f"{storage}/{GROUP_PATH}/{artifact_id}"
    try:
        resp = requests.get(
            url, auth=auth, timeout=_HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        versions = []
        for child in data.get("children", []):
            if child.get("folder") and child.get("uri"):
                versions.append(child["uri"].lstrip("/"))
        return versions
    except Exception as e:  # noqa: BLE001
        _log.warning("rule12: version listing failed for %s: %s", artifact_id, e)
        return []


def _resolve_target_sources(
    art_base: str, artifact_id: str, target_version: str, auth: tuple | None
) -> tuple[str, dict[str, str]] | None:
    """Find and download the best target ``-sources.jar`` (target_version, or the
    latest available <= target_version). Returns (version, java_files) or None."""
    versions = _list_artifactory_versions(art_base, artifact_id, auth)
    # Candidate order: highest <= target first, then step down.
    candidates: list[str] = []
    if versions:
        from upgrade_lib.weblogic.jar_reconcile import version_tuple

        tv = version_tuple(target_version)
        candidates = sorted(
            [v for v in versions if version_tuple(v) <= tv],
            key=version_tuple,
            reverse=True,
        )
    # Always also try the exact target as a fallback if listing was empty.
    if target_version and target_version not in candidates:
        candidates.append(target_version)

    for cand in candidates:
        files = _read_java_from_jar(sources_url(art_base, artifact_id, cand), auth)
        if files:  # non-empty: has .java (skip 404s and empty/degenerate jars)
            return cand, files
    return None


def _read_customer_java(folder: Path) -> dict[str, str]:
    """Read the customer's exploded ``.java`` sources from a WEB-INF/lib folder."""
    out: dict[str, str] = {}
    for p in folder.rglob("*.java"):
        if p.is_file():
            try:
                out[p.relative_to(folder).as_posix()] = p.read_text(
                    encoding="utf-8", errors="replace"
                )
            except Exception:  # noqa: BLE001
                continue
    return out


def reconcile_web_inf_lib(repo_root: str | Path, project: dict) -> dict[str, Any]:
    """Return comparison entries (keyed by source_rel) for each WEB-INF/lib jar.

    ``repo_root`` is the git clone dir (WEB-INF/lib lives outside the
    ``plugins/IMPLEMENTATION`` source_root).
    """
    results: dict[str, Any] = {}
    lib_dir = Path(repo_root) / L.WEB_INF_LIB_REL
    if not lib_dir.is_dir():
        return results

    jar_folders = [
        d for d in sorted(lib_dir.iterdir())
        if d.is_dir() and d.name.lower().endswith(".jar")
    ]
    if not jar_folders:
        return results

    target_version = (project.get("target_version") or "").strip()
    git_url = project.get("git_url") or ""
    branch = project.get("git_branch") or "main"
    fix_projects = set(project.get("jira_fix_projects") or _DEFAULT_FIX_PROJECTS)

    # Repository bases for source-jar downloads (rule 12.2 diff).
    nexus_base = repo_base(project["baseline_url"]) if project.get("baseline_url") else ""
    art_base = repo_base(project["artifactory_url"]) if project.get("artifactory_url") else ""
    art_auth = _artifactory_auth()

    # ── git commit → JIRA keys for the lib folders (best-effort) ─────────────
    git_hits: dict[str, set[str]] = {}
    rel_paths = [f"{L.WEB_INF_LIB_REL}/{d.name}" for d in jar_folders]
    try:
        from upgrade_lib.sources.git_provider import GitProvider

        git_hits = GitProvider().extract_jira_keys_for_paths(
            git_url, branch, rel_paths
        )
    except Exception as e:  # noqa: BLE001 — evidence gathering is best-effort
        _log.warning("rule12: git JIRA scan failed (%s); falling back to warnings", e)

    # ── JIRA client (safe no-op when disabled) ───────────────────────────────
    try:
        from upgrade_lib.jira.jira_client import JiraClient

        jira = JiraClient()
    except Exception as e:  # noqa: BLE001
        _log.warning("rule12: JIRA client init failed (%s)", e)
        jira = None

    for d in jar_folders:
        key = f"WEB-INF/lib/{d.name}"
        out_rel = f"{L.WEB_INF_LIB_OUTPUT_PREFIX}/{d.name}"
        base = {
            "bucket": L.WEB_INF_LIB_BUCKET,
            "category": "WEB-INF/lib",
            "name": d.name,
            "rel_path": d.name,
            "source_rel": key,
            "source_abs": str(d),
            "output_rel": out_rel,
            "engine": "weblogic-rule12",
        }

        parsed = parse_jar_folder(d.name)
        if not parsed:
            results[key] = {
                **base,
                "decision": "Retain",
                "forced_decision": "Retain",
                "copy_as_is": True,
                "core_warning": "Unrecognized jar folder name — kept for Core review.",
                "analysis": "Unparseable jar folder name; retained pending Core review.",
            }
            continue

        artifact_id, cur_ver = parsed
        # Commit tickets for this jar folder (the pivots, e.g. DOO-387).
        commit_keys = sorted(git_hits.get(f"{L.WEB_INF_LIB_REL}/{d.name}", set()))

        # Resolve the authoritative TA/PDSUPPORT tickets: a commit ticket links
        # (Clones / is related to / …) to the TA/PDSUPPORT ticket that carries
        # Resolution + Fix Versions. A commit key already in a fix project is
        # itself a candidate; each commit key's links are also followed.
        fix_ticket_keys: list[str] = []
        if jira is not None and getattr(jira, "enabled", False):
            seen: set[str] = set()
            for ck in commit_keys:
                if _project_of(ck) in fix_projects and ck not in seen:
                    seen.add(ck)
                    fix_ticket_keys.append(ck)
                for linked in jira.get_linked_issue_keys(ck, fix_projects):
                    if linked not in seen:
                        seen.add(linked)
                        fix_ticket_keys.append(linked)

        jira_infos: list[dict] = []
        for fk in fix_ticket_keys:
            info = jira.get_resolution_and_fix_versions(fk)
            if info:
                jira_infos.append(info)

        decision = (
            decide_from_jira(jira_infos, cur_ver, target_version)
            if target_version else None
        )

        if decision:
            results[key] = {
                **base,
                "decision": "Remove",
                "forced_decision": "Remove",
                "commit_tickets": commit_keys,
                "fix_tickets": fix_ticket_keys,
                "jira_key": decision["jira_key"],
                "fix_version": decision["fix_version"],
                "artifact_id": artifact_id,
                "current_version": cur_ver,
                "analysis": decision["reason"],
            }
        else:
            # No JIRA-fix evidence → download baseline (Nexus) + target
            # (Artifactory) -sources.jar and 3-way diff vs the customer's
            # exploded sources, then warn with the code delta (rule 12.2).
            base_warn = (
                f"No related TA/PD JIRA marked Resolved=Fixed with a Fix Version in "
                f"({cur_ver}, {target_version or '?'}] — this must be reported to Core."
            )
            jar_diff: dict | None = None
            diff_note = ""
            try:
                customer_java = _read_customer_java(d)
                baseline_java = (
                    _read_java_from_jar(
                        sources_url(nexus_base, artifact_id, cur_ver), None
                    )
                    if nexus_base else None
                )
                target_pair = (
                    _resolve_target_sources(art_base, artifact_id, target_version, art_auth)
                    if (art_base and target_version) else None
                )
                if baseline_java is not None and target_pair is not None:
                    tgt_ver, target_java = target_pair
                    jar_diff = diff_java_trees(customer_java, baseline_java, target_java)
                    jar_diff["baseline_version"] = cur_ver
                    jar_diff["target_version"] = tgt_ver
                    c, u = jar_diff["customizations"], jar_diff["upstream"]
                    if not jar_diff["customer_customized"]:
                        diff_note = (
                            f" No customer customization vs {cur_ver} sources; target "
                            f"{tgt_ver} changed {len(u['modified'])} file(s) — likely "
                            "safe to take from target (confirm with Core)."
                        )
                    else:
                        diff_note = (
                            f" Customer delta vs {cur_ver}: {len(c['modified'])} modified, "
                            f"{len(c['added'])} added, {len(c['removed'])} removed; target "
                            f"{tgt_ver} changed {len(u['modified'])} file(s). Report to Core."
                        )
                else:
                    diff_note = " (Source-jar download unavailable — code delta not computed.)"
            except Exception as e:  # noqa: BLE001 — diff is best-effort
                _log.warning("rule12: source-jar diff failed for %s: %s", d.name, e)
                diff_note = " (Source-jar diff failed.)"

            entry = {
                **base,
                "decision": "Retain",
                "forced_decision": "Retain",
                "copy_as_is": True,
                "commit_tickets": commit_keys,
                "fix_tickets": fix_ticket_keys,
                "artifact_id": artifact_id,
                "current_version": cur_ver,
                "core_warning": base_warn + diff_note,
                "analysis": base_warn + diff_note,
            }
            if jar_diff is not None:
                entry["jar_diff"] = jar_diff
            results[key] = entry

    return results
