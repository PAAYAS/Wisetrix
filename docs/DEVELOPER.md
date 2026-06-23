# Developer guide

Where to make changes, the extension points, and the review checklist. For the
full engineering reference see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Guiding principle

**If you find yourself writing Python merge code, you're in the wrong file.**
Merge intelligence lives in `policies/merge_policy.md` and the merge agent —
not in algorithmic helpers. The deterministic Python paths (compare, large-JSON
merge, `_diff.json` carry-forward, quality gate) are the deliberate exceptions
where exactness beats judgement.

---

## Where to change things

| You want to change… | Edit this |
|---------------------|-----------|
| A merge rule (what wins, how arrays merge, primary keys) | `policies/merge_policy.md` |
| `_diff.json` output format | `policies/diff_format_spec.md` |
| The merge agent's instructions to Claude | `upgrade_lib/agents/merge_agent.py` (`_MERGE_TOOLS_PROMPT`) |
| A per-agent system prompt | `upgrade_lib/prompts/<agent>_system.md` |
| Prompt templates | `upgrade_lib/prompts.py` |
| Compare logic / business rules | `upgrade_lib/compare.py` |
| Deterministic JSON merge / large-file routing | `upgrade_lib/json_merge.py` |
| Merge orchestration & what is sent to the LLM | `upgrade_api/merge_util.py` |
| Quality gate checks | `upgrade_lib/quality/quality_gate.py` |
| Risk scoring factors | `upgrade_lib/agents/risk_agent.py` |
| Source resolution (git/artifactory/local) | `upgrade_lib/sources/*.py` |
| DB seed-data reconciliation | `upgrade_lib/db/*.py` |
| JIRA operations | `upgrade_lib/jira/*.py` |
| Report format / sections | `upgrade_lib/report/report_generator.py` |
| Model / max_turns / retries | `upgrade_lib/agents/base_agent.py` |
| FastAPI endpoint / SSE stream | `upgrade_api/routers/<tab>.py` |
| Next.js UI page | `upgrade-web/app/projects/[id]/<tab>/page.tsx` |
| SSE phase label shown to users | `PHASE_LABELS` in the consuming page (`merges/page.tsx`, `scan/page.tsx`) |
| Streamlit UI / tabs | `upgrade-frontend/app.py` |
| New customer | **No code change.** Use the Add / Update Project form. |

---

## What the LLM merges vs. what is deterministic

This split lives in `upgrade_api/merge_util.py` and is the key to reliable,
fast merges:

- **Large base JSON** (≥ 2 MB, e.g. `integration_def.json`) → **deterministic**
  3-way merge (`upgrade_lib/json_merge.py`). Never sent to the LLM.
- **`_diff.json` sidecars** → handled by the diff pipeline (carried forward for
  large artifacts, or regenerated from the merged base). **Excluded from the LLM
  inputs** — sending them wastes time and pressures the model into truncating
  the real code files it's merging alongside.
- **Everything else** (`.java`, `.js`, `.jsp`, small `.json`/`.xml`) → merged by
  the LLM with the full merge policy in context.

If you change what goes to the LLM, keep this invariant: the LLM should only see
files where semantic merging is actually required.

---

## Running the engine without the UI

```python
from pathlib import Path
from upgrade_lib import (
    UpgradeClient, compare_artifact_local, RiskAgent, MergeAgent, QualityGate,
)

# 1. Compare (no Claude)
result = compare_artifact_local(
    Path("repos/AGCO/integration_def/GPM_OUTBOUND_V2"),
    Path("system26.2/integration_def/GPM_OUTBOUND_V2"),
    "integration_def/GPM_OUTBOUND_V2",
)

# 2. Risk score (no Claude)
risk = RiskAgent().assess(result)

# 3. Merge via Claude
merged = MergeAgent().merge(customer_files, system_files, baseline_files, customer="AGCO")

# 4. Quality gate (no Claude)
qr = QualityGate().check(merged["merged_files"])

# Or use the facade
merged = UpgradeClient().merge_artifact(customer_files, system_files, baseline_files, customer="AGCO")
```

---

## Code-review checklist

- [ ] No algorithmic merge/diff/dedup logic added to the LLM path — rules go in `policies/merge_policy.md`.
- [ ] No hardcoded customer names (only `SYSTEM_BUCKET_NAME = "SYSTEM"` is special).
- [ ] Bucket-aware paths: `source_root / bucket / rel`.
- [ ] Quality-gate and risk checks stay deterministic (no Claude calls).
- [ ] Large JSON and `_diff.json` stay out of the LLM inputs.
- [ ] JIRA operations are safe when disabled (no-op, return `None`/`False`/`[]`).
- [ ] If a new agent uses tools, `allowed_tools=` is passed to `BaseAgent._call()` explicitly.
- [ ] Tested with at least one non-AGCO customer.
