"""
test_router.py — verify that all agent requests go through the Claude Router.

Run from the project root (no server needed):
    py test_router.py

What it checks:
  1. Router is importable and active
  2. Each agent resolves to the expected model
  3. Escalation rules fire correctly
  4. BaseAgent._call() picks up the router (not hardcoded model)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
HEAD = "\033[1m\033[36m"
RESET = "\033[0m"

results = []

def check(label: str, got: str, expected: str) -> None:
    ok = got == expected
    status = PASS if ok else FAIL
    results.append(ok)
    print(f"  {status}  {label}")
    if not ok:
        print(f"         expected : {expected}")
        print(f"         got      : {got}")


# ---------------------------------------------------------------------------
print(f"\n{HEAD}=== Claude Router — routing verification ==={RESET}\n")

# 1. Import check
print("1. Import & singleton")
try:
    from upgrade_lib.claude_router import (
        ClaudeRouter, RouteRequest, RouteDecision,
        default_router, HAIKU, SONNET, OPUS,
    )
    results.append(True)
    print(f"  {PASS}  upgrade_lib.claude_router imports OK")
    print(f"         default_router = {default_router}")
except Exception as exc:
    results.append(False)
    print(f"  {FAIL}  import failed: {exc}")
    sys.exit(1)

# 2. Default routing table
print("\n2. Default model per agent")
cases = [
    ("merge",   SONNET),
    ("diff",    HAIKU),
    ("review",  SONNET),
    ("summary", HAIKU),
    ("chat",    HAIKU),
    ("risk",    HAIKU),
]
for agent, expected_model in cases:
    d = default_router.route(RouteRequest(agent_name=agent, prompt_len=1000))
    check(f"agent={agent:<10} -> {expected_model.split('claude-')[1]}", d.model, expected_model)

# 3. Escalation rules
print("\n3. Escalation rules")

# tool-using + many files -> Sonnet (if default was Haiku)
# diff is Haiku by default, give it tools + 60 files
d = default_router.route(RouteRequest(agent_name="diff", prompt_len=1000, has_tools=True, estimated_file_count=60))
check("diff  + tools + 60 files -> Sonnet (escalate from Haiku)", d.model, SONNET)

# large prompt -> Sonnet
d = default_router.route(RouteRequest(agent_name="summary", prompt_len=110_000))
check("summary + 110K prompt   -> Sonnet (escalate from Haiku)", d.model, SONNET)

# large merge -> Opus
d = default_router.route(RouteRequest(agent_name="merge", prompt_len=5000, has_tools=True, estimated_file_count=110))
check("merge  + 110 files      -> Opus   (escalate from Sonnet)", d.model, OPUS)

# small merge stays Sonnet
d = default_router.route(RouteRequest(agent_name="merge", prompt_len=5000, has_tools=True, estimated_file_count=10))
check("merge  + 10 files       -> Sonnet (no escalation)", d.model, SONNET)

# override always wins
d = default_router.route(RouteRequest(agent_name="diff", prompt_len=1000, model_override=OPUS))
check("diff   + override=Opus  -> Opus   (caller override)", d.model, OPUS)

# 4. BaseAgent picks up the router
print("\n4. BaseAgent wired to router")
try:
    from upgrade_lib.agents.merge_agent import MergeAgent
    from upgrade_lib.agents.diff_agent import DiffAgent
    from upgrade_lib.agents.review_agent import ReviewAgent
    from upgrade_lib.agents.summary_agent import SummaryAgent

    for AgentClass, name in [
        (MergeAgent,   "merge"),
        (DiffAgent,    "diff"),
        (ReviewAgent,  "review"),
        (SummaryAgent, "summary"),
    ]:
        agent = AgentClass()
        has_router = isinstance(agent._router, ClaudeRouter)
        results.append(has_router)
        status = PASS if has_router else FAIL
        print(f"  {status}  {name} agent._router = {agent._router!r}"[:80])
except Exception as exc:
    results.append(False)
    print(f"  {FAIL}  agent import failed: {exc}")

# 5. UpgradeClient wires router into all agents
print("\n5. UpgradeClient propagates router")
try:
    from upgrade_lib.claude_client import UpgradeClient
    client = UpgradeClient()
    for attr, name in [("_merge","merge"),("_diff","diff"),("_review","review"),("_summary","summary")]:
        agent = getattr(client, attr)
        has_router = isinstance(agent._router, ClaudeRouter)
        results.append(has_router)
        status = PASS if has_router else FAIL
        print(f"  {status}  UpgradeClient.{attr}._router active")

    # router=False disables routing
    client2 = UpgradeClient(router=False)
    disabled = client2._merge._router is None
    results.append(disabled)
    status = PASS if disabled else FAIL
    print(f"  {status}  UpgradeClient(router=False) -> router disabled")
except Exception as exc:
    results.append(False)
    print(f"  {FAIL}  UpgradeClient import failed: {exc}")

# ---------------------------------------------------------------------------
passed = sum(results)
total  = len(results)
print()
if all(results):
    print(f"{HEAD}All {total}/{total} checks passed — every agent request goes through the Claude Router.{RESET}\n")
else:
    print(f"\033[31m{passed}/{total} checks passed — see FAIL lines above.\033[0m\n")
    sys.exit(1)
