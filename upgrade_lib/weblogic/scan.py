"""WebLogic artifact scanner.

Walks a WebLogic customer deployment (rooted at ``plugins/IMPLEMENTATION``) and
emits artifact descriptors that plug into the existing 3-way compare/merge loop.

Each descriptor is the same shape the Docker scanner produces, plus three
WebLogic-only fields the compare loop reads when present (Docker descriptors
never set them, so Docker behaviour is unchanged):

    source_abs       real source directory on disk (the customer artifact)
    system_rel       normalized, target-relative path used to locate the SYSTEM
                     target and baseline counterparts (after $->/ and redirects)
    forced_decision  "Retain" | "Remove" | None — short-circuits the compare
    copy_as_is       True  -> at merge time, copy the source tree verbatim
    output_rel       location of this artifact in the merged output tree

The scanner is driven entirely by ``layout.py``; it hardcodes no customer name.
"""

from __future__ import annotations

import os
from pathlib import Path

from upgrade_lib.compare import EXCLUDE_NAMES, INCLUDE_EXTS
from upgrade_lib.weblogic import layout as L


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _has_artifact_file(filenames: list[str]) -> bool:
    """True if a directory directly contains at least one comparable artifact
    file (``.json``/``.xml``/``.java``/``.js``/``.jsp``), ignoring the excluded
    housekeeping files and spreadsheets."""
    for f in filenames:
        suffix = Path(f).suffix.lower()
        if suffix in L.IGNORE_SUFFIXES:
            continue
        if suffix in INCLUDE_EXTS and f not in EXCLUDE_NAMES:
            return True
    return False


def _iter_artifact_dirs(root: Path):
    """Yield ``(rel_posix, abs_dir)`` for every directory under *root* that
    directly holds a comparable artifact file. ``rel_posix`` is POSIX-style and
    relative to *root*."""
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        if _has_artifact_file(filenames):
            rel = Path(dirpath).relative_to(root).as_posix()
            yield rel, Path(dirpath)


def _detect_customer(repos_components: Path) -> str:
    """The customer bucket is the sole non-SYSTEM child of repos_components.
    Auto-detected so no customer name is ever hardcoded."""
    if repos_components.is_dir():
        for child in sorted(repos_components.iterdir(), key=lambda p: p.name):
            if child.is_dir() and child.name != L.SYSTEM_BUCKET:
                return child.name
    return "CUSTOMER"


def _descriptor(
    *,
    bucket: str,
    norm_rel: str,
    abs_dir: Path,
    system_rel: str,
    source_rel: str,
    forced_decision: str | None = None,
    copy_as_is: bool = False,
    output_rel: str | None = None,
) -> dict:
    parts = norm_rel.split("/")
    return {
        "bucket": bucket,
        "category": parts[0] if parts else "",
        "subcategory": parts[1] if len(parts) > 2 else "",
        "name": parts[-1] if parts else "",
        "rel_path": norm_rel,
        "source_rel": source_rel,
        "abs_path": str(abs_dir),
        # WebLogic-only fields (read by the compare loop when present):
        "source_abs": str(abs_dir),
        "system_rel": system_rel,
        "forced_decision": forced_decision,
        "copy_as_is": copy_as_is,
        "output_rel": output_rel or norm_rel,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def scan_weblogic_artifacts(source_root, project_id: str = "SOURCE") -> list[dict]:
    """Scan a WebLogic deployment rooted at ``plugins/IMPLEMENTATION``.

    Returns a list of artifact descriptors (see module docstring). Applies the
    full routing table from ``layout.py``: ``$``->``/`` mapping, category
    redirects, skips, forced Retain/Remove, and environment overlays.
    """
    impl = Path(source_root)
    artifacts: list[dict] = []
    seen: set[str] = set()

    def add(desc: dict) -> None:
        if desc["source_rel"] in seen:
            return
        seen.add(desc["source_rel"])
        artifacts.append(desc)

    repos_components = impl / L.REPOS_COMPONENTS
    customer = _detect_customer(repos_components)

    # 1. repos_components/{CUST} and repos_components/SYSTEM — 3-way compare.
    if repos_components.is_dir():
        for bucket_dir in sorted(repos_components.iterdir(), key=lambda p: p.name):
            if not bucket_dir.is_dir():
                continue
            bucket = bucket_dir.name  # customer name or "SYSTEM"
            for rel, abs_dir in _iter_artifact_dirs(bucket_dir):
                top = rel.split("/", 1)[0]  # raw category folder (may contain $)
                if top in L.SKIP_CATEGORIES:  # testscenario / testsuite (rule 11)
                    continue
                norm_rel = L.dollar_to_slash(rel)  # rule 2
                if len(norm_rel.split("/")) < 2:
                    continue  # need at least category + name
                add(_descriptor(
                    bucket=bucket,
                    norm_rel=norm_rel,
                    abs_dir=abs_dir,
                    system_rel=norm_rel,
                    source_rel=f"{L.REPOS_COMPONENTS}/{bucket}/{rel}",
                    output_rel=f"repos/{bucket}/{norm_rel}",
                ))

    # 2. Redirected source folders (rules 7, 9, 10):
    #    business_process/policy -> bizpolicydefs
    #    business_process/rules  -> bizruledefs
    #    de_config               -> windowdefs
    for src_segs, target_cat in L.REDIRECTS.items():
        base = impl.joinpath(*src_segs)
        for rel, abs_dir in _iter_artifact_dirs(base):
            norm_rel_in = L.dollar_to_slash(rel)
            target_rel = f"{target_cat}/{norm_rel_in}"
            add(_descriptor(
                bucket=customer,
                norm_rel=target_rel,
                abs_dir=abs_dir,
                system_rel=target_rel,
                source_rel="/".join(src_segs) + "/" + rel,
                output_rel=f"repos/{customer}/{target_rel}",
            ))

    # 3. Environment overlays — _ENV_SPECIFIC/{ENV}/repos_components/{CUST}/...
    #    integration_def_config -> Retain + copy; everything else -> compare.
    env_root = impl / L.ENV_SPECIFIC_DIR
    if env_root.is_dir():
        for env_dir in sorted(env_root.iterdir(), key=lambda p: p.name):
            if not env_dir.is_dir():
                continue
            env = env_dir.name  # DEV / PROD / UAT
            env_rc = env_dir / L.REPOS_COMPONENTS
            if not env_rc.is_dir():
                continue
            for cust_dir in sorted(env_rc.iterdir(), key=lambda p: p.name):
                if not cust_dir.is_dir():
                    continue
                for rel, abs_dir in _iter_artifact_dirs(cust_dir):
                    norm_rel = L.dollar_to_slash(rel)
                    parts = norm_rel.split("/")
                    if len(parts) < 2:
                        continue
                    is_copy = parts[0] == L.ENV_COPY_CATEGORY  # rule 4
                    add(_descriptor(
                        bucket=f"__env_specific/{env}",
                        norm_rel=norm_rel,
                        abs_dir=abs_dir,
                        system_rel=norm_rel,
                        source_rel=(
                            f"{L.ENV_SPECIFIC_DIR}/{env}/{L.REPOS_COMPONENTS}/"
                            f"{cust_dir.name}/{rel}"
                        ),
                        forced_decision="Retain" if is_copy else None,
                        copy_as_is=is_copy,
                        output_rel=f"repos/__env_specific/{env}/{norm_rel}",
                    ))

    # 4. app-extensions/src — copy verbatim, always Retain (rule 5).
    app_ext = impl.joinpath(*L.APP_EXTENSIONS_SRC_SEGMENTS)
    if app_ext.is_dir():
        add(_descriptor(
            bucket=customer,
            norm_rel=L.APP_EXTENSIONS_OUTPUT_REL,
            abs_dir=app_ext,
            system_rel=L.APP_EXTENSIONS_OUTPUT_REL,
            source_rel="/".join(L.APP_EXTENSIONS_SRC_SEGMENTS),
            forced_decision="Retain",
            copy_as_is=True,
            output_rel=L.APP_EXTENSIONS_OUTPUT_REL,
        ))

    # 5. _plugindef — always Removed, never compared (rule 6).
    plugindef = impl / L.PLUGINDEF_DIR
    if plugindef.is_dir():
        add(_descriptor(
            bucket=customer,
            norm_rel=L.PLUGINDEF_DIR,
            abs_dir=plugindef,
            system_rel=L.PLUGINDEF_DIR,
            source_rel=L.PLUGINDEF_DIR,
            forced_decision="Remove",
            copy_as_is=False,
        ))

    artifacts.sort(key=lambda a: (a["bucket"], a["rel_path"]))
    return artifacts
