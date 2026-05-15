"""Provider test endpoints — mirror the Streamlit 'Test Connection' buttons."""

from __future__ import annotations

from fastapi import APIRouter

from upgrade_api.schemas import TestConnectionRequest, TestConnectionResponse
from upgrade_lib.sources.artifactory_provider import ArtifactoryProvider
from upgrade_lib.sources.git_provider import GitProvider


router = APIRouter(prefix="/providers", tags=["providers"])


@router.post("/git/test", response_model=TestConnectionResponse)
def test_git(req: TestConnectionRequest) -> TestConnectionResponse:
    try:
        gp = GitProvider()
        branches = gp.list_branches(req.url)
        return TestConnectionResponse(
            success=True,
            message=f"Connected. Found {len(branches)} branches.",
            details={"branches": branches[:20]},
        )
    except Exception as e:
        return TestConnectionResponse(success=False, message=f"Git connection failed: {e}")


@router.post("/artifactory/test", response_model=TestConnectionResponse)
def test_artifactory(req: TestConnectionRequest) -> TestConnectionResponse:
    try:
        ap = ArtifactoryProvider()
        result = ap.test_connection(req.url)
        return TestConnectionResponse(
            success=bool(result.get("success")),
            message=str(result.get("message", "")),
            details={k: v for k, v in result.items() if k not in ("success", "message")},
        )
    except Exception as e:
        return TestConnectionResponse(success=False, message=f"Artifactory test failed: {e}")
