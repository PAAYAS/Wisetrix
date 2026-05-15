"""
JiraClient — REST API wrapper for JIRA operations.

Uses the atlassian-python-api library for JIRA REST API calls.
Auth credentials come from config.yaml (base_url, username, password).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class JiraConfig:
    enabled: bool
    base_url: str
    username: str
    password: str

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> JiraConfig:
        """Load JIRA config from config.yaml / config.yml."""
        if path is None:
            for name in ("config.yaml", "config.yml"):
                candidate = _PROJECT_ROOT / name
                if candidate.exists():
                    path = candidate
                    break
            else:
                path = _PROJECT_ROOT / "config.yaml"
        if not path.exists():
            return cls(enabled=False, base_url="", username="", password="")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        jira = raw.get("jira", {})
        return cls(
            enabled=bool(jira.get("enabled", False)),
            base_url=str(jira.get("base_url", "")).rstrip("/"),
            username=str(jira.get("username", "")),
            password=str(jira.get("password", "")),
        )


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str
    issue_type: str
    assignee: str | None = None
    url: str = ""


class JiraClient:
    """
    Thin wrapper over atlassian-python-api's Jira class.

    All methods are safe to call even if JIRA is disabled — they return
    empty results and log a warning.
    """

    def __init__(self, config: JiraConfig | None = None) -> None:
        self._config = config or JiraConfig.from_yaml()
        self._jira = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def _connect(self):
        """Lazy-connect to JIRA."""
        if self._jira is not None:
            return self._jira
        if not self._config.enabled:
            logger.warning("JIRA is disabled in config.yaml")
            return None
        try:
            from atlassian import Jira
            self._jira = Jira(
                url=self._config.base_url,
                username=self._config.username,
                password=self._config.password,
            )
            return self._jira
        except ImportError:
            logger.error("atlassian-python-api is not installed. Run: pip install atlassian-python-api")
            return None
        except Exception as e:
            logger.error(f"JIRA connection failed: {e}")
            return None

    def test_connection(self) -> dict[str, Any]:
        """Test JIRA connectivity. Returns {success, message, user}."""
        jira = self._connect()
        if jira is None:
            return {"success": False, "message": "JIRA not configured or disabled", "user": None}
        try:
            myself = jira.myself()
            if isinstance(myself, dict):
                name = myself.get("displayName", myself.get("name", "unknown"))
                return {"success": True, "message": f"Connected as {name}", "user": name}
            # Server returned HTML or non-JSON — still connected if no exception
            return {"success": True, "message": "Connected successfully", "user": None}
        except Exception as e:
            msg = str(e)
            # Don't dump HTML pages into error messages
            if "<html" in msg.lower() or len(msg) > 200:
                msg = "Connection failed — check base_url and credentials in config"
            return {"success": False, "message": msg, "user": None}

    @staticmethod
    def _parse_issue(data: Any, base_url: str) -> JiraIssue | None:
        """Safely parse a JIRA issue response (dict or unexpected type)."""
        if not isinstance(data, dict):
            logger.warning("Unexpected issue response type: %s", type(data))
            return None
        fields = data.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        key = data.get("key", "")
        if not key:
            return None
        status = fields.get("status")
        status_name = status.get("name", "Unknown") if isinstance(status, dict) else str(status or "Unknown")
        itype = fields.get("issuetype")
        itype_name = itype.get("name", "Unknown") if isinstance(itype, dict) else str(itype or "Unknown")
        assignee = fields.get("assignee")
        assignee_name = assignee.get("displayName") if isinstance(assignee, dict) else None
        return JiraIssue(
            key=key,
            summary=fields.get("summary", "") or "",
            status=status_name,
            issue_type=itype_name,
            assignee=assignee_name,
            url=f"{base_url}/browse/{key}",
        )

    def get_issue(self, issue_key: str) -> JiraIssue | None:
        """Fetch a single JIRA issue by key."""
        jira = self._connect()
        if jira is None:
            return None
        try:
            data = jira.issue(issue_key)
            return self._parse_issue(data, self._config.base_url)
        except Exception as e:
            logger.error(f"Failed to fetch {issue_key}: {e}")
            return None

    def get_issue_comments(self, issue_key: str) -> list[dict[str, str]]:
        """
        Fetch comments for a JIRA issue.

        Returns list of {author, body, created} dicts.
        """
        jira = self._connect()
        if jira is None:
            return []
        try:
            data = jira.issue(issue_key, fields="comment")
            if not isinstance(data, dict):
                return []
            fields = data.get("fields") or {}
            if not isinstance(fields, dict):
                return []
            comment_block = fields.get("comment", {})
            if not isinstance(comment_block, dict):
                return []
            comments = comment_block.get("comments", [])
            result = []
            for c in comments:
                if not isinstance(c, dict):
                    continue
                author = c.get("author", {})
                author_name = author.get("displayName", author.get("name", "unknown")) if isinstance(author, dict) else "unknown"
                result.append({
                    "author": author_name,
                    "body": c.get("body", ""),
                    "created": c.get("created", ""),
                })
            return result
        except Exception as e:
            logger.error(f"Failed to fetch comments for {issue_key}: {e}")
            return []

    def get_issue_details(self, issue_key: str) -> dict[str, Any] | None:
        """
        Fetch issue with full details: summary, status, description, and comments.

        Returns dict with issue info + comments, or None on failure.
        """
        issue = self.get_issue(issue_key)
        if not issue:
            return None
        comments = self.get_issue_comments(issue_key)
        return {
            "key": issue.key,
            "summary": issue.summary,
            "status": issue.status,
            "issue_type": issue.issue_type,
            "assignee": issue.assignee,
            "url": issue.url,
            "comments": comments,
        }

    def search(self, jql: str, max_results: int = 50) -> list[JiraIssue]:
        """Search JIRA with JQL. Returns list of JiraIssue."""
        jira = self._connect()
        if jira is None:
            return []
        try:
            results = jira.jql(jql, limit=max_results)
            if not isinstance(results, dict):
                logger.warning("Unexpected JQL response type: %s", type(results))
                return []
            issues = []
            for item in results.get("issues", []):
                issue = self._parse_issue(item, self._config.base_url)
                if issue:
                    issues.append(issue)
            return issues
        except Exception as e:
            logger.error(f"JIRA search failed: {e}")
            return []

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        parent_key: str | None = None,
        labels: list[str] | None = None,
    ) -> JiraIssue | None:
        """Create a JIRA issue. If parent_key is set, creates a subtask."""
        jira = self._connect()
        if jira is None:
            return None
        try:
            fields: dict[str, Any] = {
                "project": {"key": project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": "Sub-task" if parent_key else issue_type},
            }
            if parent_key:
                fields["parent"] = {"key": parent_key}
            if labels:
                fields["labels"] = labels

            result = jira.create_issue(fields=fields)
            if isinstance(result, dict):
                key = result.get("key", result.get("id", ""))
            else:
                key = str(result)
            return JiraIssue(
                key=key,
                summary=summary,
                status="To Do",
                issue_type="Sub-task" if parent_key else issue_type,
                url=f"{self._config.base_url}/browse/{key}",
            )
        except Exception as e:
            logger.error(f"Failed to create issue: {e}")
            return None

    def add_comment(self, issue_key: str, body: str) -> bool:
        """Add a comment to an issue."""
        jira = self._connect()
        if jira is None:
            return False
        try:
            jira.issue_add_comment(issue_key, body)
            return True
        except Exception as e:
            logger.error(f"Failed to add comment to {issue_key}: {e}")
            return False

    def transition_issue(self, issue_key: str, status: str) -> bool:
        """Transition an issue to a new status (e.g., 'In Progress', 'Done')."""
        jira = self._connect()
        if jira is None:
            return False
        try:
            transitions = jira.get_issue_transitions(issue_key)
            target = None
            for t in transitions:
                if t.get("name", "").lower() == status.lower():
                    target = t["id"]
                    break
            if target is None:
                logger.warning(f"No transition to '{status}' found for {issue_key}")
                return False
            jira.set_issue_status(issue_key, status)
            return True
        except Exception as e:
            logger.error(f"Failed to transition {issue_key}: {e}")
            return False

    def get_project_issues(
        self, project_key: str, max_results: int = 200,
    ) -> list[JiraIssue]:
        """Get all issues for a project."""
        jql = f'project = "{project_key}" ORDER BY created DESC'
        return self.search(jql, max_results=max_results)

    @staticmethod
    def extract_project_key_from_url(url: str) -> str | None:
        """
        Extract JIRA project key from a project URL.

        Handles:
          https://jira.dev.e2open.com/jira/projects/ALDIBR1/issues
          https://jira.dev.e2open.com/jira/browse/ALDIBR1-123
          ALDIBR1
        """
        import re
        # /projects/KEY/...
        m = re.search(r"/projects/([A-Z][A-Z0-9_]+)", url)
        if m:
            return m.group(1)
        # /browse/KEY-123
        m = re.search(r"/browse/([A-Z][A-Z0-9_]+)-\d+", url)
        if m:
            return m.group(1)
        # Bare key
        m = re.match(r"^([A-Z][A-Z0-9_]+)$", url.strip())
        if m:
            return m.group(1)
        return None

    def get_project_issues_with_descriptions(
        self,
        project_key: str,
        max_results: int = 500,
    ) -> list[dict[str, str]]:
        """
        Fetch issues from a JIRA project including descriptions.

        Returns list of {key, summary, description} dicts.
        """
        jira = self._connect()
        if jira is None:
            return []
        try:
            results = jira.jql(
                f'project = "{project_key}" ORDER BY created DESC',
                limit=max_results,
                fields="summary,description",
            )
            if not isinstance(results, dict):
                return []
            issues = []
            for item in results.get("issues", []):
                if not isinstance(item, dict):
                    continue
                fields = item.get("fields") or {}
                if not isinstance(fields, dict):
                    fields = {}
                issues.append({
                    "key": item.get("key", ""),
                    "summary": fields.get("summary", "") or "",
                    "description": fields.get("description", "") or "",
                })
            return issues
        except Exception as e:
            logger.error(f"Failed to fetch project issues with descriptions: {e}")
            return []

    def match_issues_to_artifacts(
        self,
        project_key: str,
        artifact_keys: list[str],
    ) -> dict[str, str]:
        """
        Fetch all issues from a JIRA project and smart-match to artifacts.

        For each artifact (e.g. "ti.tx.edit.PCEEdit"), extracts keywords by:
          1. Splitting on dots and camelCase boundaries
          2. Expanding known GTM abbreviations (PCE → Pre Customs Entry, etc.)
          3. Including the category path (windowdefs, validationsets, etc.)

        Then searches issue summaries + descriptions for those keywords and
        returns *every* issue with at least one keyword hit, sorted by score
        descending (then by issue key for stable ordering).

        Returns dict mapping artifact_key → comma-separated JIRA issue keys
        (or "Not Found" when nothing matches).
        """
        if not self.enabled:
            return {k: "Not Found" for k in artifact_keys}

        all_issues = self.get_project_issues_with_descriptions(project_key)
        if not all_issues:
            return {k: "Not Found" for k in artifact_keys}

        result: dict[str, str] = {}
        for art_key in artifact_keys:
            keywords = _extract_keywords(art_key)
            matches: list[tuple[int, str]] = []

            for issue in all_issues:
                text = f"{issue['summary']} {issue['description']}".lower()
                score = sum(1 for kw in keywords if kw in text)
                if score >= 1:
                    matches.append((score, issue["key"]))

            if not matches:
                result[art_key] = "Not Found"
                continue

            # Sort by score desc, then key asc for deterministic output.
            matches.sort(key=lambda m: (-m[0], m[1]))
            result[art_key] = ", ".join(key for _score, key in matches)

        return result


# ---------------------------------------------------------------------------
# Keyword extraction for smart artifact ↔ JIRA matching
# ---------------------------------------------------------------------------

# Known GTM abbreviations → expanded forms
_GTM_ABBREVIATIONS: dict[str, list[str]] = {
    "pce": ["pre customs entry", "pre-customs"],
    "pci": ["pre customs invoice", "pre-customs invoice"],
    "ce": ["customs entry"],
    "ci": ["commercial invoice"],
    "bl": ["bill of lading"],
    "po": ["purchase order"],
    "so": ["sales order"],
    "hts": ["harmonized tariff", "tariff schedule"],
    "fta": ["free trade agreement", "trade agreement"],
    "lp": ["license permit", "license"],
    "bom": ["bill of material", "bill of materials"],
    "asn": ["advance ship notice", "advance shipment"],
    "gi": ["goods issue"],
    "gr": ["goods receipt"],
    "sd": ["self determination"],
    "sli": ["shipper letter of instruction"],
    "isn": ["internal shipment"],
    "esn": ["external shipment"],
    "tx": ["transaction", "trade"],
    "ti": ["trade intelligence"],
    "bp": ["broker packet"],
    "vs": ["validation set"],
    "ws": ["workspace"],
    "wd": ["window def", "windowdef"],
    "ds": ["dataset"],
    "rpt": ["report"],
    "srch": ["search"],
    "gen": ["general"],
    "mgmt": ["management"],
    "config": ["configuration"],
    "admin": ["administration", "admin"],
    "imp": ["import"],
    "exp": ["export"],
    "decl": ["declaration"],
    "proc": ["processing"],
}


def _split_camel_case(name: str) -> list[str]:
    """Split camelCase/PascalCase into words.

    PCEEdit → [PCE, Edit]
    BrokerPacketGeneralSearch → [Broker, Packet, General, Search]
    """
    import re
    # Split on transitions: lowercase→uppercase, uppercase→uppercase+lowercase
    parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return [p for p in parts.split() if len(p) >= 2]


def _extract_keywords(artifact_key: str) -> list[str]:
    """
    Extract search keywords from an artifact key.

    Input:  "ALDI/windowdefs/ti.tx.edit.PCEEdit"
    Output: ["windowdefs", "ti", "tx", "transaction", "trade",
             "edit", "pce", "pre customs entry", "pceedit"]
    """
    keywords: list[str] = []

    parts = artifact_key.split("/")

    # Include category (windowdefs, validationsets, datasets, actions, etc.)
    if len(parts) >= 2:
        category = parts[-2] if len(parts) >= 2 else ""
        # Skip bucket names like "ALDI", "SYSTEM"
        if category and category.lower() not in ("system",) and not category.isupper():
            keywords.append(category.lower())

    # Artifact name is last segment
    name = parts[-1] if parts else artifact_key

    # Split on dots: ti.tx.edit.PCEEdit → [ti, tx, edit, PCEEdit]
    dot_parts = name.split(".")

    for part in dot_parts:
        part_lower = part.lower()
        # Add the part itself
        if len(part_lower) >= 2:
            keywords.append(part_lower)

        # Expand abbreviations
        if part_lower in _GTM_ABBREVIATIONS:
            keywords.extend(_GTM_ABBREVIATIONS[part_lower])

        # Split camelCase: PCEEdit → [PCE, Edit]
        camel_parts = _split_camel_case(part)
        for cp in camel_parts:
            cp_lower = cp.lower()
            if cp_lower not in keywords and len(cp_lower) >= 2:
                keywords.append(cp_lower)
            # Expand abbreviations from camelCase parts
            if cp_lower in _GTM_ABBREVIATIONS:
                for expanded in _GTM_ABBREVIATIONS[cp_lower]:
                    if expanded not in keywords:
                        keywords.append(expanded)

    # Add full name without dots as well
    full_name_lower = name.replace(".", "").lower()
    if full_name_lower not in keywords and len(full_name_lower) >= 3:
        keywords.append(full_name_lower)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    return unique
