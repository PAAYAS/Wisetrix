"""Health & metadata endpoints — used by the Next.js UI to verify the API is up."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Query

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "upgrade-api",
        "time": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
    }


@router.get("/")
def root() -> dict:
    return {
        "service": "upgrade-api",
        "docs": "/docs",
        "health": "/health",
        "router_status": "/router/status",
        "router_test": "/router/test?agent=merge&files=10",
    }


@router.get("/router/status")
def router_status() -> dict:
    """Show the Claude Router routing table and confirm it is active."""
    from upgrade_lib.claude_router import default_router
    return {
        "active": True,
        "routing_table": default_router._defaults,
        "escalation_rules": {
            "tool_using_file_threshold": 50,
            "large_prompt_chars": 100_000,
            "large_merge_file_count": 100,
        },
    }


@router.get("/router/test")
def router_test(
    agent: str = Query(..., description="Agent name: merge | diff | review | summary | chat | risk"),
    files: int = Query(0, description="Estimated file count (hint for escalation)"),
    prompt_len: int = Query(5000, description="Simulated prompt length in chars"),
    tools: bool = Query(False, description="Whether the call uses tools"),
) -> dict:
    """
    Simulate a routing decision without making a real Claude call.
    Use this to verify requests will be routed to the expected model.

    Examples:
      /router/test?agent=merge&files=10
      /router/test?agent=merge&files=110
      /router/test?agent=diff
      /router/test?agent=review&prompt_len=120000
    """
    from upgrade_lib.claude_router import default_router, RouteRequest
    req = RouteRequest(
        agent_name=agent,
        prompt_len=prompt_len,
        has_tools=tools,
        estimated_file_count=files,
    )
    decision = default_router.route(req)
    return {
        "request": {
            "agent": agent,
            "prompt_len": prompt_len,
            "has_tools": tools,
            "estimated_file_count": files,
        },
        "decision": {
            "model": decision.model,
            "reason": decision.reason,
        },
        "routed_through_claude_router": True,
    }
