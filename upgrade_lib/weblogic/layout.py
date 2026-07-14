"""WebLogic source-layout constants and the scan/routing table.

The source tree is a WebLogic customer deployment. All paths here are relative
to the resolved ``source_root``, which for WebLogic mode is the repo's
``plugins/IMPLEMENTATION`` directory (set via ``source_subpath`` in the project
config).

Everything that is customer-specific to naming lives here, not in ``scan.py`` —
the scanner is generic and driven by these constants. The one reserved string
is ``SYSTEM_BUCKET`` (mirrors the engine's ``SYSTEM_BUCKET_NAME``); the customer
bucket name is auto-detected, never hardcoded.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# repos_components — the per-customer + SYSTEM artifact buckets
# --------------------------------------------------------------------------- #

# Directory (under IMPLEMENTATION) holding the artifact buckets.
REPOS_COMPONENTS = "repos_components"

# Reserved bucket name. Every other child of repos_components is a customer
# bucket whose name is auto-detected (e.g. "DOOSAN"). Never hardcode it.
SYSTEM_BUCKET = "SYSTEM"

# Categories inside a repos_components bucket that are dropped entirely (rule 11).
SKIP_CATEGORIES = {"testscenario", "testsuite"}


# --------------------------------------------------------------------------- #
# Environment overlays — _ENV_SPECIFIC/{ENV}/repos_components/{CUST}/{category}
# --------------------------------------------------------------------------- #

ENV_SPECIFIC_DIR = "_ENV_SPECIFIC"

# Under an environment overlay this category is copied verbatim and always
# Retain (rule 4). Every other overlay category is compared 3-way as normal.
ENV_COPY_CATEGORY = "integration_def_config"


# --------------------------------------------------------------------------- #
# Whole-tree special cases
# --------------------------------------------------------------------------- #

# Plugin definitions — always Removed, never compared (rule 6).
PLUGINDEF_DIR = "_plugindef"

# App-extension Java sources — copied verbatim, always Retain (rule 5).
# Source segments (under IMPLEMENTATION) and the merged-output location
# (relative to the Docker ``app_root``).
APP_EXTENSIONS_SRC_SEGMENTS = ("app-extensions", "src")
APP_EXTENSIONS_OUTPUT_REL = "app-extensions/src"


# --------------------------------------------------------------------------- #
# Merged output layout
# --------------------------------------------------------------------------- #

# The WebLogic merge writes a Docker-format delivery tree. Every artifact's
# ``output_rel`` is relative to this prefix (itself relative to the project's
# merge_output_dir). Normal artifacts land at repos/<BUCKET>/<rel>.
OUTPUT_APP_ROOT_PREFIX = "client_delivery/src/main/resources/app_root"


# --------------------------------------------------------------------------- #
# WEB-INF/lib source jars (rule 12)
# --------------------------------------------------------------------------- #

# Location of the exploded ``<artifactId>-<ver>.jar`` source folders — relative
# to the REPO ROOT (not IMPLEMENTATION, so it needs the clone dir, not source_root).
WEB_INF_LIB_REL = "app_root/install/app_war_src/WEB-INF/lib"

# Output location (relative to the Docker app_root) for lib sources that are
# retained pending Core review.
WEB_INF_LIB_OUTPUT_PREFIX = "install/app_war_src/WEB-INF/lib"

# Bucket label for lib artifacts (displayed in the UI). Distinct from the repos
# buckets, so it never triggers the windowdefs/datasets business rules.
WEB_INF_LIB_BUCKET = "WEB-INF/lib"


# --------------------------------------------------------------------------- #
# Redirects — source folders that map to a different SYSTEM target category
# (rules 7, 9, 10). Keyed by source path segments relative to IMPLEMENTATION.
# --------------------------------------------------------------------------- #

REDIRECTS: dict[tuple[str, ...], str] = {
    ("business_process", "policy"): "bizpolicydefs",
    ("business_process", "rules"): "bizruledefs",
    ("de_config",): "windowdefs",
}


# --------------------------------------------------------------------------- #
# Ignored content
# --------------------------------------------------------------------------- #

# Spreadsheet artifacts are DB seed-data, never part of the app compare
# (rules 7/9/10 "ignore xls", plus the xls-only sibling folders).
IGNORE_SUFFIXES = {".xls", ".xlsx", ".xlsm"}


# --------------------------------------------------------------------------- #
# Path normalization
# --------------------------------------------------------------------------- #

def dollar_to_slash(rel: str) -> str:
    """Translate WebLogic ``$``-delimited names into real folder paths (rule 2).

    ``custom_privilages$menus/TRADE_DEFAULT`` -> ``custom_privilages/menus/TRADE_DEFAULT``

    ``$`` only ever appears in top-level category folder names, but replacing all
    occurrences is safe because it never appears in leaf/file names.
    """
    return rel.replace("$", "/")
