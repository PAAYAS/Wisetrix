"""
QualityGate — deterministic quality checks on merged output.

Runs BEFORE the Claude-powered ReviewAgent to catch obvious issues
without spending API tokens. If the gate returns FAIL, the merge
is blocked and ReviewAgent is skipped.

Checks:
  1. Conflict markers
  2. Valid JSON
  3. Valid XML
  4. Duplicate Java methods
  5. Import-usage mismatches (basic)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


@dataclass
class Finding:
    severity: str       # "ERROR" | "WARNING" | "INFO"
    category: str       # e.g. "conflict_markers", "invalid_json", "duplicate_method"
    file: str           # filename
    line: int | None    # line number or None
    message: str        # human-readable description


@dataclass
class QualityResult:
    verdict: str                         # "PASS" | "WARN" | "FAIL"
    findings: list[Finding] = field(default_factory=list)
    blocking: bool = False               # True if merge should be blocked

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "file": f.file,
                    "line": f.line,
                    "message": f.message,
                }
                for f in self.findings
            ],
            "blocking": self.blocking,
        }


# Conflict marker patterns
_CONFLICT_RE = re.compile(r"^(<{7}|={7}|>{7})", re.MULTILINE)

# Java method signature pattern (simplified)
_JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?"
    r"(?:synchronized\s+)?(?:[\w<>\[\],\s]+)\s+(\w+)\s*\(([^)]*)\)",
    re.MULTILINE,
)

# Java import pattern
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)


class QualityGate:
    """Deterministic quality checks — no Claude calls."""

    def check(self, merged_files: dict[str, str]) -> QualityResult:
        """
        Run all quality checks on merged files.

        Args:
            merged_files: dict mapping filename → content

        Returns:
            QualityResult with verdict, findings, and blocking flag.
        """
        findings: list[Finding] = []

        for filename, content in merged_files.items():
            ext = Path(filename).suffix.lower()

            # Check 1: Conflict markers (all file types)
            findings.extend(self._check_conflict_markers(filename, content))

            # Check 2: Valid JSON
            if ext == ".json":
                findings.extend(self._check_json(filename, content))

            # Check 3: Valid XML
            if ext == ".xml":
                findings.extend(self._check_xml(filename, content))

            # Check 4 & 5: Java-specific checks
            if ext in (".java", ".js", ".jsp"):
                findings.extend(self._check_duplicate_methods(filename, content))
                if ext == ".java":
                    findings.extend(self._check_imports(filename, content))

        # Determine verdict
        has_error = any(f.severity == "ERROR" for f in findings)
        has_warning = any(f.severity == "WARNING" for f in findings)

        if has_error:
            verdict = "FAIL"
            blocking = True
        elif has_warning:
            verdict = "WARN"
            blocking = False
        else:
            verdict = "PASS"
            blocking = False

        return QualityResult(verdict=verdict, findings=findings, blocking=blocking)

    # ---- individual checks ---------------------------------------------------

    @staticmethod
    def _check_conflict_markers(filename: str, content: str) -> list[Finding]:
        findings = []
        for i, line in enumerate(content.splitlines(), 1):
            if _CONFLICT_RE.match(line):
                findings.append(Finding(
                    severity="ERROR",
                    category="conflict_markers",
                    file=filename,
                    line=i,
                    message=f"Conflict marker found: {line.strip()[:40]}",
                ))
        return findings

    @staticmethod
    def _check_json(filename: str, content: str) -> list[Finding]:
        try:
            json.loads(content)
            return []
        except json.JSONDecodeError as e:
            return [Finding(
                severity="ERROR",
                category="invalid_json",
                file=filename,
                line=e.lineno,
                message=f"Invalid JSON: {e.msg}",
            )]

    @staticmethod
    def _check_xml(filename: str, content: str) -> list[Finding]:
        try:
            ET.fromstring(content)
            return []
        except ET.ParseError as e:
            line = e.position[0] if e.position else None
            return [Finding(
                severity="ERROR",
                category="invalid_xml",
                file=filename,
                line=line,
                message=f"Invalid XML: {e}",
            )]

    @staticmethod
    def _check_duplicate_methods(filename: str, content: str) -> list[Finding]:
        """Check for duplicate method signatures at class level."""
        findings = []
        seen: dict[str, int] = {}  # signature → first line

        for match in _JAVA_METHOD_RE.finditer(content):
            name = match.group(1)
            params = match.group(2).strip()
            # Normalize param types (strip names, keep types)
            param_types = []
            if params:
                for p in params.split(","):
                    parts = p.strip().split()
                    if parts:
                        param_types.append(parts[0])
            sig = f"{name}({','.join(param_types)})"

            line = content[:match.start()].count("\n") + 1
            if sig in seen:
                findings.append(Finding(
                    severity="WARNING",
                    category="duplicate_method",
                    file=filename,
                    line=line,
                    message=f"Duplicate method signature: {sig} (first at line {seen[sig]})",
                ))
            else:
                seen[sig] = line

        return findings

    @staticmethod
    def _check_imports(filename: str, content: str) -> list[Finding]:
        """Basic check for unused imports in Java files."""
        findings = []
        imports = _JAVA_IMPORT_RE.findall(content)

        for imp in imports:
            # Get the short class name (last segment)
            short_name = imp.rsplit(".", 1)[-1]
            if short_name == "*":
                continue  # wildcard imports can't be checked

            # Count occurrences (excluding the import line itself)
            # Simple heuristic: if the short name appears only once
            # (in the import), it's likely unused.
            occurrences = len(re.findall(r"\b" + re.escape(short_name) + r"\b", content))
            if occurrences <= 1:
                findings.append(Finding(
                    severity="INFO",
                    category="import_mismatch",
                    file=filename,
                    line=None,
                    message=f"Possibly unused import: {imp}",
                ))

        return findings
