"""
CatClassifier — deterministic CAT 1-5 configuration-severity classification.

No Claude calls. Pure Python, driven by a category->rule table plus a few
heuristics. Mirrors the style of ``risk_agent.RiskAgent`` but answers a
different question: not *how hard is this to merge* (that is Risk) but *how
severe / upgrade-hostile is this customization, and who must own it* on
E2open's GTM CAT 1-5 scale.

CAT scale (from the "GTM Product Configuration Category (CAT 1-5)" and
"GTM Modification Categorization - PS" Confluence pages):

    CAT1  Self-Service Settings          customer admin/user   upgrade: none
    CAT2  Pre-Defined Configurations     Solution Architect    upgrade: minimal
    CAT3  Extension Configurations       IE / EL               upgrade: modest
    CAT4  Complex Model Configurations   EL + R&D/Product       upgrade: significant
    CAT5  Custom Application Development  R&D / roadmap only    upgrade: significant

Each artifact also carries an ``upgrade_friendly`` flag (the PS matrix's
"Upgrade Friendly Y/N" column) — whether the config is expected to survive a
product upgrade cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatRule:
    """Base classification for a category before heuristics."""

    level: int              # 1-5
    upgrade_friendly: bool
    label: str              # human-readable CAT name


@dataclass
class CatAssessment:
    level: int                          # 1-5
    upgrade_friendly: bool
    label: str                          # e.g. "Complex Model Configuration"
    factors: list[str] = field(default_factory=list)


# CAT level -> canonical name (the "Config Category" column).
_CAT_LABELS: dict[int, str] = {
    1: "Self-Service Setting",
    2: "Pre-Defined Configuration",
    3: "Extension Configuration",
    4: "Complex Model Configuration",
    5: "Custom Application Development",
}


# --------------------------------------------------------------------------- #
# Base map: artifact top-level ``category`` -> CatRule.
# Reviewed with the user against the GTM CAT 1-5 + PS modification matrices.
# --------------------------------------------------------------------------- #
def _rule(level: int, upgrade_friendly: bool) -> CatRule:
    return CatRule(level=level, upgrade_friendly=upgrade_friendly, label=_CAT_LABELS[level])


CAT_MAP: dict[str, CatRule] = {
    # ── CAT1 — Self-Service ─────────────────────────────────────────────────
    "custom_privilages":          _rule(1, True),   # Security Privileges
    "menus":                      _rule(1, False),  # UI Security – Menu / Label

    # ── CAT2 — Pre-Defined ──────────────────────────────────────────────────
    "integration_def":            _rule(2, True),   # Integration Definition (existing); →4 if new
    "integration_def_config":     _rule(2, True),   # Transport / property config
    "triggerdefs":                _rule(2, True),   # Trigger Configuration
    "configDefs":                 _rule(2, True),
    "app_config_mapping":         _rule(2, True),
    "jobdef":                     _rule(2, True),    # Scheduler jobs

    # ── CAT3 — Extension ────────────────────────────────────────────────────
    "bizruledefs":                _rule(3, True),    # Business Rule; →4/5 if code
    "admissibility_rules":        _rule(3, True),    # Business Rule
    "validationdefs":             _rule(3, False),   # New validations
    "validationsets":             _rule(3, False),   # IMPL rules / validation-set policy
    "verificationdefs":           _rule(3, False),
    "entity_alert_defs":          _rule(3, False),   # New Alerts
    "datasets":                   _rule(3, True),    # Report / ad-hoc search; →4 if new
    "excel_report_defs":          _rule(3, False),   # Report changes
    "adhoc_windowdefs":           _rule(3, True),    # Ad-hoc search / Universe
    "data_inheritance_config":    _rule(3, False),
    "reference_resolutions":      _rule(3, False),
    "TX_Consolidation":           _rule(3, True),    # Consolidation configuration

    # ── CAT4 — Complex Model ────────────────────────────────────────────────
    "windowdefs":                 _rule(4, False),   # Window Definition
    "windowdef_templates":        _rule(4, False),
    "windowdef_tiles":            _rule(4, False),
    "dgs":                        _rule(4, False),   # DGS – Template; →5 if code/schema
    "bizpolicydefs":              _rule(4, False),   # Policies / complex business rules
    "contentupdatehandlerdefs":   _rule(4, False),   # Content-update handlers (ETL/schema)
    "transmappingdefs":           _rule(4, False),   # Transformation mapping + ETL
    "mappings":                   _rule(4, False),
    "resolution_processing_defs": _rule(4, False),
    "MultiLegResolver":           _rule(4, False),   # Resolver logic

    # ── CAT5 — Custom Application Development ────────────────────────────────
    "app-extensions":             _rule(5, False),   # Custom java source
    "normalized_codeHooks":       _rule(5, False),   # Code hooks
    "plugindefs":                 _rule(5, False),   # Plugins = custom code
}

# Conservative fallback for categories not in the map (logged via a factor).
_FALLBACK_RULE = _rule(3, False)

# WEB-INF/lib rule-12 java sources: bucket label + the "<artifactId>-<ver>.jar"
# category naming. Always core library source code -> CAT5.
_WEB_INF_LIB_BUCKET = "WEB-INF/lib"

# NOTE on code files: GTM config artifacts routinely ship .js/.jsp expression
# scripts and .java rule classes as *normal* content (every bizruledef has a
# BusinessRule.java; window/integration defs carry .js). So mere presence of a
# code file does NOT distinguish a simple config (CAT3) from a complex one
# (CAT4) — that nuance needs human/LLM review and is deliberately out of scope
# for this deterministic v1. Genuinely custom-code categories (app-extensions,
# WEB-INF/lib, normalized_codeHooks, plugindefs) are already CAT5 by base rule.

# Categories whose "new" (source-only) variant is a CAT4 per the matrix
# ("New Integration Definition", "New Universe / Report").
_NEW_BUMPS_TO_CAT4 = {"integration_def", "datasets"}


class CatClassifier:
    """Deterministic CAT 1-5 classifier — no Claude calls."""

    agent_name = "cat"

    def classify(self, comparison_result: dict) -> CatAssessment:
        """Classify a single artifact's comparison result into CAT 1-5."""
        category = (comparison_result.get("category") or "").strip()
        bucket = (comparison_result.get("bucket") or "").strip()
        factors: list[str] = []

        # ── Base lookup ─────────────────────────────────────────────────────
        # WEB-INF/lib rule-12 rows (bucket == "WEB-INF/lib" or a "*.jar"
        # category) are extracted core library sources -> CAT5.
        if bucket == _WEB_INF_LIB_BUCKET or category.endswith(".jar"):
            rule = _rule(5, False)
            factors.append("Core library source (WEB-INF/lib)")
        else:
            rule = CAT_MAP.get(category)
            if rule is None:
                rule = _FALLBACK_RULE
                factors.append(f"Unknown category '{category or '?'}' — fallback CAT3")
            else:
                factors.append(f"Category '{category}' → CAT{rule.level}")

        level = rule.level
        upgrade_friendly = rule.upgrade_friendly

        # ── Heuristic: "new" (source-only) integration_def / datasets → CAT4 ─
        # A source-only artifact with no core counterpart is a genuinely NEW
        # definition ("New Integration Definition" / "New Universe" = CAT4),
        # as opposed to a modification of an existing core one (CAT2/CAT3).
        if (
            category in _NEW_BUMPS_TO_CAT4
            and comparison_result.get("target_exists") is False
            and level < 4
        ):
            level = 4
            upgrade_friendly = False
            factors.append("New artifact (no core counterpart) → CAT4")

        # ── Clamp ───────────────────────────────────────────────────────────
        level = max(1, min(5, level))
        return CatAssessment(
            level=level,
            upgrade_friendly=upgrade_friendly,
            label=_CAT_LABELS[level],
            factors=factors,
        )

    def classify_all(self, comparison_results: dict) -> dict[str, CatAssessment]:
        """Classify every artifact in a comparison run (keyed by source_rel)."""
        return {
            key: self.classify(result)
            for key, result in comparison_results.items()
        }

    @staticmethod
    def summary(assessments: dict[str, CatAssessment]) -> dict[str, int]:
        """Return counts keyed 'CAT1'..'CAT5'."""
        counts = {f"CAT{i}": 0 for i in range(1, 6)}
        for ca in assessments.values():
            counts[f"CAT{ca.level}"] = counts.get(f"CAT{ca.level}", 0) + 1
        return counts


__all__ = ["CatClassifier", "CatAssessment", "CatRule", "CAT_MAP"]
