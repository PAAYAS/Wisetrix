"""Locate DB seed-data files for an artifact.

The customer DB seed data lives in a separate git repo (`<project>_db`) under a
conventional path. For `bizpolicydefs`, each policy is exported as one workbook:

    <db_source_root>/bppol/bppol.<ORG>.<POLICY_NAME>.xlsx

where <db_source_root> is:

    <repo>/src/main/resources/seed_data_src/workspace/packages/<project>_seed_data/data

The repo and the `<project>` token vary per customer, so the subpath is built
from the project id (lower-cased) and can be overridden in project config.
"""

from __future__ import annotations

import re
from pathlib import Path


def project_token_from_db_url(db_git_url: str) -> str:
    """Derive the seed-data project token from a *_db git URL.

    e.g. "https://git.dev.e2open.com/scm/ser/agco_db.git" → "agco"
    Falls back to the bare repo name (minus a trailing _db) lower-cased.
    """
    if not db_git_url:
        return ""
    name = db_git_url.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.git$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"_db$", "", name, flags=re.IGNORECASE)
    return name.strip().lower()


def db_subpath_default(project_token: str) -> str:
    """Default seed-data subpath within the *_db git repo for a project.

    `project_token` is the customer/project token used in the repo layout,
    e.g. "agco" → ".../packages/agco_seed_data/data". We lower-case it to match
    the observed convention (agco_seed_data).
    """
    token = (project_token or "").strip().lower()
    return (
        "src/main/resources/seed_data_src/workspace/packages/"
        f"{token}_seed_data/data"
    )


def bppol_dir(db_source_root: Path | str) -> Path:
    """Directory holding the bppol workbooks."""
    return Path(db_source_root) / "bppol"


def bppol_filename(org: str, policy_name: str) -> str:
    """Workbook filename for a single policy: bppol.<ORG>.<POLICY_NAME>.xlsx."""
    return f"bppol.{org}.{policy_name}.xlsx"


def bppol_file(db_source_root: Path | str, org: str, policy_name: str) -> Path:
    """Conventional path to a policy's bppol workbook (exact naming)."""
    return bppol_dir(db_source_root) / bppol_filename(org, policy_name)


def _strip_export_suffix(core: str) -> str:
    """Strip trailing export-tooling suffixes from a bppol filename core.

    Real exports truncate the policy id and append tokens, e.g.
      tx_screening.type_export_shi.1785727671.nrm
    Repeatedly drop a trailing `.nrm`-style token or a trailing `.<digits>`
    group so the core can be prefix-matched against the policy id.
    """
    prev = None
    while prev != core:
        prev = core
        core = re.sub(r"\.(nrm|\d+)$", "", core)
    return core


def find_bppol_file(
    db_source_root: Path | str, org: str, policy_name: str
) -> Path | None:
    """Locate a policy's bppol workbook, tolerating truncated/suffixed names.

    Export tooling may write `bppol.<ORG>.<truncated-policy>.<n>.nrm.xlsx`
    instead of the clean `bppol.<ORG>.<policy>.xlsx`. We match by:
      1. exact filename, else
      2. the file whose `bppol.<ORG>.` core (after stripping numeric/.nrm
         suffixes) is a prefix of — or equals — the policy id (longest wins).

    Returns the matched Path, or None if no candidate matches.
    """
    d = bppol_dir(db_source_root)
    if not d.is_dir():
        return None

    exact = d / bppol_filename(org, policy_name)
    if exact.exists():
        return exact

    prefix = f"bppol.{org}."
    best: Path | None = None
    best_len = -1
    for f in d.iterdir():
        if not f.is_file() or f.suffix.lower() != ".xlsx":
            continue
        if not f.name.startswith(prefix):
            continue
        core = f.name[len(prefix):]
        if core.lower().endswith(".xlsx"):
            core = core[: -len(".xlsx")]
        core = _strip_export_suffix(core)
        if not core:
            continue
        if core == policy_name or policy_name.startswith(core) or core.startswith(policy_name):
            if len(core) > best_len:
                best, best_len = f, len(core)
    return best
