"""Provider test endpoints — mirror the Streamlit 'Test Connection' buttons."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter

from upgrade_api.schemas import TestConnectionRequest, TestConnectionResponse
from upgrade_lib.sources.artifactory_provider import ArtifactoryProvider
from upgrade_lib.sources.git_provider import GitProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])


def _friendly_git_error(exc: Exception) -> str:
    """Extract a readable message from a GitCommandError or other git exception."""
    raw = str(exc)

    # gitpython.GitCommandError exposes stderr as an attribute
    stderr = getattr(exc, "stderr", None)
    if stderr:
        stderr = stderr.strip()
        # Extract just the fatal/error line from git's stderr output
        for line in stderr.splitlines():
            line = line.strip()
            if line.lower().startswith(("fatal:", "error:")):
                return line

    # Parse stderr out of the string representation: "stderr: 'fatal: ...'"
    match = re.search(r"stderr:\s*['\"](.+?)['\"]", raw, re.DOTALL)
    if match:
        stderr_text = match.group(1).strip()
        for line in stderr_text.splitlines():
            line = line.strip()
            if line.lower().startswith(("fatal:", "error:")):
                return line
        return stderr_text[:200]

    # Classify common patterns into actionable messages
    low = raw.lower()
    if "authentication failed" in low or "could not read username" in low:
        return "Authentication failed — check your SSH key or HTTPS credentials."
    if "repository" in low and "not found" in low:
        return "Repository not found — verify the URL and that you have access."
    if "could not resolve host" in low or "name or service not known" in low:
        return "Network error — cannot reach the host. Check the URL and your network."
    if "timeout" in low or "timed out" in low:
        return "Connection timed out — the server is unreachable or very slow."
    if "permission denied" in low:
        return "Permission denied — your SSH key may not be authorised for this repo."
    if "invalid url" in low or "not a git repository" in low:
        return "Invalid URL — does not point to a valid git repository."

    # Fall back to the first non-empty line of the raw message (avoids the
    # full multi-line gitpython traceback flooding the toast)
    for line in raw.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return "Unknown git error."


@router.post("/git/test", response_model=TestConnectionResponse)
def test_git(req: TestConnectionRequest) -> TestConnectionResponse:
    logger.info("test_git: url=%s", req.url)
    try:
        gp = GitProvider()
        branches = gp.list_branches(req.url)
        logger.info("test_git: success, %d branches found for %s", len(branches), req.url)
        return TestConnectionResponse(
            success=True,
            message=f"Connected. Found {len(branches)} branches.",
            details={"branches": branches[:20]},
        )
    except Exception as e:
        friendly = _friendly_git_error(e)
        logger.error("test_git: failed for %s: %s", req.url, e, exc_info=True)
        return TestConnectionResponse(success=False, message=friendly)


@router.post("/artifactory/test", response_model=TestConnectionResponse)
def test_artifactory(req: TestConnectionRequest) -> TestConnectionResponse:
    logger.info("test_artifactory: url=%s", req.url)
    try:
        ap = ArtifactoryProvider()
        result = ap.test_connection(req.url)
        success = bool(result.get("success"))
        message = str(result.get("message", ""))
        if success:
            logger.info("test_artifactory: success for %s", req.url)
        else:
            logger.warning("test_artifactory: returned failure for %s: %s", req.url, message)
        return TestConnectionResponse(
            success=success,
            message=message,
            details={k: v for k, v in result.items() if k not in ("success", "message")},
        )
    except Exception as e:
        logger.error("test_artifactory: exception for %s: %s", req.url, e, exc_info=True)
        return TestConnectionResponse(success=False, message=f"Artifactory test failed: {e}")
