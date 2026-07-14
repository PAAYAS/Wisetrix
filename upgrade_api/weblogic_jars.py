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
    decide_from_diff,
    decide_from_jira,
    diff_java_trees,
    parse_jar_folder,
    pick_target_version,
    repo_base,
    sources_url,
)

_log = logging.getLogger(__name__)

# (connect, read) timeouts: fail fast when a source server is unreachable so a
# down Nexus/Artifactory can't stall the scan for minutes, but allow time to
# read a large sources jar once connected.
_HTTP_TIMEOUT = (10, 60)

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


def reconcile_web_inf_lib(
    repo_root: str | Path,
    project: dict,
    progress_cb: "Any" = None,
) -> dict[str, Any]:
    """Return comparison entries (keyed by source_rel) for each WEB-INF/lib jar.

    The decision is made **per ``.java`` file**: each file's git commit → pivot
    ticket → linked TA/PDSUPPORT ticket → Resolution=Fixed with a Fix Version in
    ``(curVer, targetVer]`` → that file is Removed. Per-file decisions roll up to
    the jar (all Removed → jar Remove; otherwise Retain, copying only the kept
    files). ``progress_cb(phase, data)`` is called once per jar so the UI can show
    "Comparing <jar>".

    ``repo_root`` is the git clone dir (WEB-INF/lib lives outside the
    ``plugins/IMPLEMENTATION`` source_root).
    """
    def emit(phase: str, **data: Any) -> None:
        if progress_cb is not None:
            try:
                progress_cb(phase, data)
            except Exception:  # noqa: BLE001
                pass

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

    # Repository bases for the source-jar download fallback (rule 12.2).
    nexus_base = repo_base(project["baseline_url"]) if project.get("baseline_url") else ""
    art_base = repo_base(project["artifactory_url"]) if project.get("artifactory_url") else ""
    art_auth = _artifactory_auth()

    # ── Enumerate every .java file under every jar folder ─────────────────────
    # jar_java[d] = [(java_path, folder_rel, repo_rel), ...]
    jar_java: dict[Path, list[tuple[Path, str, str]]] = {}
    all_repo_rels: list[str] = []
    for d in jar_folders:
        items: list[tuple[Path, str, str]] = []
        for jf in sorted(d.rglob("*.java")):
            if not jf.is_file():
                continue
            folder_rel = jf.relative_to(d).as_posix()
            repo_rel = f"{L.WEB_INF_LIB_REL}/{d.name}/{folder_rel}"
            items.append((jf, folder_rel, repo_rel))
            all_repo_rels.append(repo_rel)
        jar_java[d] = items

    # ── One git history walk → per-file commit JIRA keys ─────────────────────
    git_hits: dict[str, set[str]] = {}
    try:
        from upgrade_lib.sources.git_provider import GitProvider

        git_hits = GitProvider().extract_jira_keys_for_paths(
            git_url, branch, all_repo_rels
        )
    except Exception as e:  # noqa: BLE001 — evidence gathering is best-effort
        _log.warning("rule12: git JIRA scan failed (%s); files fall back to warnings", e)

    # ── JIRA client (safe no-op when disabled) + caches ──────────────────────
    try:
        from upgrade_lib.jira.jira_client import JiraClient

        jira = JiraClient()
    except Exception as e:  # noqa: BLE001
        _log.warning("rule12: JIRA client init failed (%s)", e)
        jira = None

    jira_on = jira is not None and getattr(jira, "enabled", False)
    _link_cache: dict[str, list[str]] = {}   # commit key → linked fix-ticket keys
    _info_cache: dict[str, dict | None] = {}  # ticket key → resolution/fixVersions

    def _fix_tickets(commit_keys: list[str]) -> list[str]:
        """Commit keys → the TA/PDSUPPORT tickets to check (self if already in a
        fix project, plus linked tickets), cached per commit key."""
        out: list[str] = []
        seen: set[str] = set()
        for ck in commit_keys:
            if _project_of(ck) in fix_projects and ck not in seen:
                seen.add(ck)
                out.append(ck)
            if ck not in _link_cache:
                _link_cache[ck] = (
                    jira.get_linked_issue_keys(ck, fix_projects) if jira_on else []
                )
            for lk in _link_cache[ck]:
                if lk not in seen:
                    seen.add(lk)
                    out.append(lk)
        return out

    def _infos(keys: list[str]) -> list[dict]:
        infos: list[dict] = []
        for k in keys:
            if k not in _info_cache:
                _info_cache[k] = (
                    jira.get_resolution_and_fix_versions(k) if jira_on else None
                )
            if _info_cache[k]:
                infos.append(_info_cache[k])
        return infos

    total = len(jar_folders)
    for i, d in enumerate(jar_folders, 1):
        # Progress reads "Comparing <folder>" (the jar folder name).
        emit(f"Comparing {d.name}", jar=d.name, index=i, total=total)

        parsed = parse_jar_folder(d.name)
        if not parsed:
            key = f"WEB-INF/lib/{d.name}"
            results[key] = {
                "bucket": L.WEB_INF_LIB_BUCKET,
                "category": d.name,
                "name": d.name,
                "rel_path": d.name,
                "source_rel": key,
                "source_abs": str(d),
                "output_rel": f"{L.WEB_INF_LIB_OUTPUT_PREFIX}/{d.name}",
                "engine": "weblogic-rule12",
                "decision": "",   # no decision — report to Core
                "core_warning": "Unrecognized jar folder name. This needs to be reported to Core.",
                "analysis": "Unparseable jar folder name.",
            }
            continue

        artifact_id, cur_ver = parsed

        # Source jars for the diff fallback are downloaded lazily — once per jar,
        # only when a file has no JIRA fix evidence. Bind artifact_id/cur_ver as
        # defaults so the closure captures this iteration's values.
        _srcs: dict[str, Any] = {"loaded": False, "baseline": None, "target": None, "tgt_ver": None}

        def _ensure_sources(_aid=artifact_id, _cv=cur_ver) -> None:
            if _srcs["loaded"]:
                return
            _srcs["loaded"] = True
            if nexus_base:
                _srcs["baseline"] = _read_java_from_jar(
                    sources_url(nexus_base, _aid, _cv), None
                )
            if art_base and target_version:
                pair = _resolve_target_sources(art_base, _aid, target_version, art_auth)
                if pair:
                    _srcs["tgt_ver"], _srcs["target"] = pair

        # ── One comparison row PER .java file ────────────────────────────────
        for jf, folder_rel, repo_rel in jar_java[d]:
            fkey = f"WEB-INF/lib/{d.name}/{folder_rel}"
            commit_keys = sorted(git_hits.get(repo_rel, set()))
            fix_keys = _fix_tickets(commit_keys)
            infos = _infos(fix_keys)
            fbase = {
                "bucket": L.WEB_INF_LIB_BUCKET,
                "category": d.name,                       # the jar folder
                "name": folder_rel.rsplit("/", 1)[-1],    # the .java filename
                "rel_path": f"{d.name}/{folder_rel}",
                "source_rel": fkey,
                "source_abs": str(jf),
                "output_rel": f"{L.WEB_INF_LIB_OUTPUT_PREFIX}/{d.name}/{folder_rel}",
                "engine": "weblogic-rule12",
                "artifact_id": artifact_id,
                "current_version": cur_ver,
                "commit_tickets": commit_keys,
                "fix_tickets": fix_keys,
            }

            # JIRA first.
            dec = (
                decide_from_jira(infos, cur_ver, target_version)
                if target_version else None
            )
            if dec:
                results[fkey] = {
                    **fbase,
                    "decision": "Remove",
                    "forced_decision": "Remove",
                    "basis": "jira",
                    "jira_key": dec["jira_key"],
                    "fix_version": dec["fix_version"],
                    "analysis": dec["reason"],
                }
                continue

            # Else: source-jar diff fallback for this file (rule 12.2).
            _ensure_sources()
            try:
                cust_src = jf.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                cust_src = None
            base_src = (_srcs["baseline"] or {}).get(folder_rel) if _srcs["baseline"] else None
            tgt_src = (_srcs["target"] or {}).get(folder_rel) if _srcs["target"] else None
            ddec = decide_from_diff(cust_src, base_src, tgt_src)
            if ddec["remove"]:
                results[fkey] = {
                    **fbase,
                    "decision": "Remove",
                    "forced_decision": "Remove",
                    "basis": "diff",
                    "target_version": _srcs["tgt_ver"],
                    "analysis": ddec["reason"],
                }
            else:
                # No decision (not Remove/Retain/Merge) — blank the decision and
                # surface a "report to Core" note, mirroring the DB warning.
                results[fkey] = {
                    **fbase,
                    "decision": "",
                    "basis": "diff",
                    "target_version": _srcs["tgt_ver"],
                    "core_warning": ddec["reason"],
                    "analysis": ddec["reason"],
                }

    return results
