---
name: upgrade-agent
description: GTM Docker-to-Docker upgrade agent. Multi-agent Claude SDK architecture with 5 specialized agents, Git/Artifactory source providers, quality gates, risk scoring, JIRA integration, and report generation. Handles scan, compare, merge, _diff.json, review, and chat for any customer (ALDI, EMRSN, ABT, ...).
---

# Upgrade Agent Skill (v2 — Multi-Agent Architecture)

Migrates customer customizations onto a newer SYSTEM baseline (currently SYSTEM 26.2 vs baseline 24.4.11). Five specialized Claude agents replace the original monolithic UpgradeClient, each with a focused system prompt and input/output contract. Uses Claude Code's OAuth session — **no `ANTHROPIC_API_KEY` required**.

The codebase is **customer-agnostic**: ALDI, EMRSN, ABT, CEVA, etc. all flow through the same code paths.

## Invariants

- **No algorithmic merge code.** Merge rules live in `policies/merge_policy.md`. Don't reintroduce Python merge helpers.
- **No customer name hardcoding.** Customer names are auto-detected from `source_root` subdirectories.
- **Compare is local.** `upgrade_lib/compare.py` is deterministic Python. Don't push it to Claude.
- **Merge uses Claude with Read/Write tools.** Python stages files into a temp working directory, hands Claude the paths + the merge policy, and reads back the merged output. Other agents (diff, review, summary) pass content inline — they don't need tools.
- **Quality gates are deterministic.** Run before Claude review to save tokens.

## Project Layout

```
ta-ai-docker-upgrade-v2/
├── policies/                        # Plain-English rules (source of truth)
│   ├── merge_policy.md              #   Merge rules, 3-way logic, business rules
│   └── diff_format_spec.md          #   _diff.json runtime delta format
│
├── upgrade_lib/                     # Core Python engine
│   ├── __init__.py                  #   Public API exports
│   ├── compare.py                   #   Deterministic local compare (from v1)
│   ├── claude_client.py             #   UpgradeClient facade (backward compat)
│   ├── prompts.py                   #   Prompt templates + policy loaders
│   │
│   ├── agents/                      #   Specialized Claude agents
│   │   ├── base_agent.py            #     Shared SDK transport, async, usage tracking
│   │   ├── merge_agent.py           #     3-way merge with policy context
│   │   ├── diff_agent.py            #     _diff.json generation
│   │   ├── review_agent.py          #     Quality review (PASS/WARN/FAIL)
│   │   ├── risk_agent.py            #     Deterministic risk scoring (no Claude)
│   │   └── summary_agent.py         #     Executive narrative
│   │
│   ├── prompts/                     #   Per-agent system prompts
│   │   ├── merge_system.md
│   │   ├── diff_system.md
│   │   ├── review_system.md
│   │   └── summary_system.md
│   │
│   ├── quality/                     #   Quality gates + risk scoring
│   │   ├── quality_gate.py          #     Deterministic PASS/WARN/FAIL checks
│   │   └── risk_scorer.py           #     Per-artifact HIGH/MEDIUM/LOW scoring
│   │
│   ├── sources/                     #   Source providers
│   │   ├── base_provider.py         #     ABC for source resolution
│   │   ├── local_provider.py        #     Local filesystem paths (backward compat)
│   │   ├── git_provider.py          #     Bitbucket clone/pull + branch selection
│   │   └── artifactory_provider.py  #     JAR download + extraction from JFrog
│   │
│   ├── jira/                        #   JIRA integration
│   │   ├── jira_client.py           #     REST API wrapper (atlassian-python-api)
│   │   └── jira_tracker.py          #     Upgrade-to-JIRA mapping (epic/subtasks)
│   │
│   └── report/                      #   Report generation
│       └── report_generator.py      #     UPGRADE_REPORT.md with JIRA + risk context
│
├── upgrade-frontend/
│   └── app.py                       #   Streamlit UI (8 tabs)
│
├── docs/
│   └── ARCHITECTURE.md              #   Engineering reference
├── config.example.yaml              #   Credential template (Artifactory, JIRA, Git)
├── projects.json                    #   Per-customer config (git/artifactory/local)
├── requirements.txt                 #   Dependencies
├── run_state/                       #   Persisted state per project (gitignored)
└── output/                          #   Merged artifacts (gitignored)
```

## Agents

| Agent | File | Claude? | Input | Output |
|-------|------|---------|-------|--------|
| **MergeAgent** | `merge_agent.py` | Yes | customer + SYSTEM + baseline files | `{merged_files, explanation}` |
| **DiffAgent** | `diff_agent.py` | Yes | merged JSON + SYSTEM JSON | `{diff_json, explanation}` |
| **ReviewAgent** | `review_agent.py` | Yes | merged + customer + SYSTEM files | `{verdict, findings, recommendations}` |
| **RiskAgent** | `risk_agent.py` | No | comparison result + metadata | `{level, score, factors}` |
| **SummaryAgent** | `summary_agent.py` | Yes | all results + risk + quality | Markdown narrative |

`UpgradeClient` is a backward-compatible facade that delegates to these agents.

## Pipeline

```
ScanAgent (local Python) → RiskScorer → MergeAgent → QualityGate → DiffAgent → ReviewAgent → SummaryAgent → ReportGenerator
```

1. **Scan & Compare** — local deterministic compare, risk scoring applied
2. **Quality Gate** — deterministic checks (conflict markers, JSON/XML validity, duplicate methods) before Claude
3. **Merge** — Claude 3-way merge with policy context
4. **Diff** — Claude generates `_diff.json` runtime deltas
5. **Review** — Claude structured quality review (PASS/WARN/FAIL)
6. **Summary** — Claude executive narrative with risk + quality context
7. **Report** — UPGRADE_REPORT.md with JIRA tickets, risk, quality, merge details

## Source Providers

| Provider | Config Field | Resolves To |
|----------|-------------|-------------|
| **GitProvider** | `source_type: "git"`, `git_url`, `git_branch` | Cloned repo → `{subpath}/repos` |
| **ArtifactoryProvider** | `target_type: "artifactory"`, `artifactory_url` | Downloaded JAR → `app_root/repos/SYSTEM` |
| **LocalProvider** | `source_root`, `target_system` | Local paths as-is |

Caches: `~/.wisetrix/git_cache/`, `~/.wisetrix/artifactory_cache/`

## JIRA Integration

| Component | File | Purpose |
|-----------|------|---------|
| **JiraClient** | `jira/jira_client.py` | REST API wrapper — CRUD issues, search, transitions |
| **JiraTracker** | `jira/jira_tracker.py` | Maps pipeline events to JIRA ops (epic, subtasks, comments) |

Features:
- Link/create upgrade Epics per project
- Auto-create subtasks for Merge artifacts
- Lookup JIRA tickets by artifact name (shown in scan results + report)
- Log scan results, merge explanations, review findings to JIRA comments
- Finalize run with report attachment
- Auth from `config.yaml` (username + password/API key)

## Report Generation

**ReportGenerator** (`report/report_generator.py`) produces `UPGRADE_REPORT.md` with:
- Run metadata (date, customer, source, target version)
- Artifact overview table with Decision, Risk, and JIRA columns
- Decision summary (Merge/Retain/Remove counts)
- Risk distribution (HIGH/MEDIUM/LOW)
- Quality gate results (PASS/WARN/FAIL with findings)
- High-risk merge details (manual review recommended)
- Per-artifact merge details with JIRA ticket references
- AI recommendations (from SummaryAgent)
- JIRA tracking links (epic + subtasks)

## When Asked to Modify Behaviour

| Change | Where to edit |
|--------|--------------|
| Merge rules | `policies/merge_policy.md` |
| `_diff.json` format | `policies/diff_format_spec.md` |
| Agent system prompts | `upgrade_lib/prompts/*.md` |
| Prompt templates | `upgrade_lib/prompts.py` |
| Compare logic / business rules | `upgrade_lib/compare.py` |
| Quality gate checks | `upgrade_lib/quality/quality_gate.py` |
| Risk scoring factors | `upgrade_lib/agents/risk_agent.py` |
| Source resolution | `upgrade_lib/sources/*.py` |
| JIRA operations | `upgrade_lib/jira/*.py` |
| Report format/sections | `upgrade_lib/report/report_generator.py` |
| Model / max_turns | `upgrade_lib/agents/base_agent.py` |
| New customer | **No code change.** Use the Setup tab. |

## Code-review checklist

- [ ] No algorithmic merge/diff/dedup code in Python.
- [ ] No hardcoded customer names.
- [ ] Bucket-aware paths: `source_root / bucket / rel`.
- [ ] Quality gate checks are deterministic (no Claude calls).
- [ ] Policy docs stay in `policies/`, not embedded in Python.
- [ ] JIRA operations are safe when disabled (no-op, returns None/False).
- [ ] Tested with at least one non-ALDI customer.
