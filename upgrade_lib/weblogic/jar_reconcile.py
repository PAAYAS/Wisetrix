"""Rule 12 — WEB-INF/lib source-JAR reconciliation (pure logic).

The customer's ``app_root/install/app_war_src/WEB-INF/lib`` holds folders named
``<artifactId>-<curVer>.jar`` containing extracted ``.java`` sources (their
possibly-customised copy of a library). For each we decide:

  * **Remove** — a related JIRA (TA / PD support) is Resolved=Fixed with a
    Fix Version in ``(curVer, targetVer]`` → the fix is already in the target,
    so the customer's WEB-INF/lib copy is dropped.
  * **Warning** — no git/JIRA evidence of a fix → must be reported to Core.

This module holds only the *pure* pieces (URL building, version math, the
JIRA-based decision). Network/git/JIRA orchestration lives in
``upgrade_api/weblogic_jars.py`` so ``upgrade_lib`` stays dependency-light and
this logic stays unit-testable.
"""

from __future__ import annotations

import re

# Maven group path shared by all WEB-INF/lib source jars (confirmed constant).
GROUP_PATH = "com/amberroad/solutions"

# Resolution value that counts as "fixed upstream".
FIXED_RESOLUTION = "fixed"

_JAR_FOLDER_RE = re.compile(r"^(?P<art>.+)-(?P<ver>\d[\d.]*)\.jar$", re.IGNORECASE)


def parse_jar_folder(name: str) -> tuple[str, str] | None:
    """``admissibility-api-20.3.jar`` -> ``("admissibility-api", "20.3")``.

    Returns None if the name isn't an ``<artifactId>-<version>.jar`` folder.
    """
    m = _JAR_FOLDER_RE.match(name.strip())
    if not m:
        return None
    return m.group("art"), m.group("ver")


def repo_base(url: str) -> str:
    """Reduce a full artifact URL to its repository base (before the group path).

    Converts Artifactory UI URLs to API URLs, then strips at ``/com/amberroad/…``:

      http://nexus:8081/repository/Releases/com/amberroad/solutions/gtm-install/20.3/gtm-install-20.3.jar
        -> http://nexus:8081/repository/Releases
      https://art/ui/native/gtm-release-dev/com/amberroad/solutions/gtm-install/26.2
        -> https://art/artifactory/gtm-release-dev
    """
    u = url.replace("/ui/native/", "/artifactory/")
    marker = "/" + GROUP_PATH
    i = u.find(marker)
    return (u[:i] if i != -1 else u).rstrip("/")


def sources_url(base: str, artifact_id: str, version: str) -> str:
    """Build the ``-sources.jar`` download URL for an artifact + version."""
    base = base.rstrip("/")
    return (
        f"{base}/{GROUP_PATH}/{artifact_id}/{version}/"
        f"{artifact_id}-{version}-sources.jar"
    )


def version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple for ordering (non-numeric → 0)."""
    parts: list[int] = []
    for seg in str(v).split("."):
        m = re.match(r"\d+", seg)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def _cmp_key(v: str) -> tuple[int, ...]:
    return version_tuple(v)


def in_fix_window(fix_version: str, cur_version: str, target_version: str) -> bool:
    """True if ``cur_version < fix_version <= target_version``."""
    fv, cv, tv = _cmp_key(fix_version), _cmp_key(cur_version), _cmp_key(target_version)
    return cv < fv <= tv


def pick_target_version(available: list[str], target_version: str) -> str | None:
    """Choose the highest available version that is ``<= target_version``.

    Used when the exact target (e.g. 26.2) has no ``-sources.jar`` — fall back
    to the latest release at or below it (e.g. 26.1).
    """
    tv = _cmp_key(target_version)
    candidates = [v for v in available if _cmp_key(v) <= tv]
    if not candidates:
        return None
    return max(candidates, key=_cmp_key)


def _norm_java(text: str) -> str:
    """Normalize source for comparison (line endings + surrounding whitespace)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def diff_java_trees(
    customer: dict[str, str],
    baseline: dict[str, str],
    target: dict[str, str],
) -> dict:
    """Three-way diff of extracted ``.java`` source trees.

    ``customer`` = the customer's exploded WEB-INF/lib sources,
    ``baseline`` = the pristine current-version ``-sources.jar`` (Nexus),
    ``target``   = the pristine target-version ``-sources.jar`` (Artifactory).

    Returns customizations (customer vs baseline) and upstream changes
    (baseline vs target), plus convenience flags.
    """
    cust, base, tgt = set(customer), set(baseline), set(target)

    def _modified(a: dict, b: dict, keys: set[str]) -> list[str]:
        return sorted(k for k in keys if _norm_java(a[k]) != _norm_java(b[k]))

    customizations = {
        "added": sorted(cust - base),
        "removed": sorted(base - cust),
        "modified": _modified(customer, baseline, cust & base),
    }
    upstream = {
        "added": sorted(tgt - base),
        "removed": sorted(base - tgt),
        "modified": _modified(baseline, target, base & tgt),
    }
    customer_customized = any(customizations.values())
    return {
        "customizations": customizations,
        "upstream": upstream,
        "customer_customized": customer_customized,
        "customer_files": len(customer),
        "baseline_files": len(baseline),
        "target_files": len(target),
    }


def decide_from_jira(
    jira_infos: list[dict],
    cur_version: str,
    target_version: str,
) -> dict | None:
    """Decide Remove from JIRA evidence, or None if no ticket qualifies.

    ``jira_infos`` = ``[{key, resolution, fix_versions:[...], summary?}]``.
    Returns ``{"decision": "Remove", "reason": ..., "jira_key": ..., ...}`` when a
    ticket is Resolution=Fixed with a Fix Version in ``(curVer, targetVer]``.
    """
    for info in jira_infos:
        resolution = str(info.get("resolution") or "").strip().lower()
        if resolution != FIXED_RESOLUTION:
            continue
        for fv in info.get("fix_versions") or []:
            if in_fix_window(fv, cur_version, target_version):
                return {
                    "decision": "Remove",
                    "jira_key": info.get("key", ""),
                    "fix_version": fv,
                    "reason": (
                        f"{info.get('key', 'ticket')} Resolved=Fixed with Fix "
                        f"Version {fv} in ({cur_version}, {target_version}] — "
                        "fix is already in the target; WEB-INF/lib copy removed."
                    ),
                }
    return None
