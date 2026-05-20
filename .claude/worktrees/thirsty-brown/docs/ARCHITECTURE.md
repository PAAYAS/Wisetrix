# Architecture — Upgrade Agent v2 (Multi-Agent)

This document complements [`README.md`](../README.md) with the engineering detail a developer needs to extend the codebase confidently.

---

## 1. Design principles

| Principle | What it means in practice |
|-----------|---------------------------|
| **No algorithmic merge code.** | Don't reintroduce regex-based dedup, JSON deep-merge helpers, or hand-rolled diff. Merge intelligence lives in `policies/merge_policy.md`. |
| **Merge uses Claude with Read/Write tools.** | Python stages files into a temp working directory, hands Claude the paths + the merge policy, and reads back the merged output. Python makes zero merge decisions. |
| **Compare is local.** | Determinism + speed. Claude only handles operations where semantic understanding earns its keep (merge, review, summary, chat). |
| **Zero hardcoded customer names.** | A single constant — `SYSTEM_BUCKET_NAME = "SYSTEM"` — is the only special-cased string. Everything else is auto-detected. |
| **Streamlit is a thin shell.** | UI does file I/O + orchestration only. Every piece of intelligence lives in `upgrade_lib`. |
| **Quality gates are deterministic.** | Run before Claude review to save tokens. No Claude calls in quality or risk checks. |
| **Policies stay in `policies/`.** | Plain-English rules are the source of truth. Never embed policy logic in Python. |

---

## 2. Project layout

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
│   │   └── risk_scorer.py           #     Convenience wrapper over RiskAgent
│   │
│   ├── sources/                     #   Source providers
│   │   ├── base_provider.py         #     ABC for source resolution
│   │   ├── local_provider.py        #     Local filesystem paths (backward compat)
│   │   ├── git_provider.py          #     Bitbucket clone/pull + branch selection
│   │   └── artifactory_provider.py  #     JAR download + extraction from JFrog
│   │
│   ├── jira/                        #   JIRA integration
│   │   ├── jira_client.py           #     REST API wrapper (atlassian-python-api)
│   │   └── jira_tracker.py          #     Upgrade-to-JIRA event mapping
│   │
│   └── report/                      #   Report generation
│       └── report_generator.py      #     UPGRADE_REPORT.md with JIRA + risk context
│
├── upgrade_api/                     # FastAPI service driving the Next.js UI
│   ├── main.py                      #   App entry + CORS middleware
│   ├── merge_util.py                #   Merge orchestration + SSE phase emission
│   ├── scan_util.py                 #   Resolve + scan + persisted resolved.json cache
│   ├── paths.py                     #   run_state file paths
│   └── routers/                     #   One module per UI tab: scan, merges, diff, jira, summary, projects, providers, health
│
├── upgrade-web/                     # Next.js 14 UI (primary)
│   └── README.md                    #   Dev guide — covers SSE bypass + requestDirect patterns
│
├── upgrade-frontend/
│   └── app.py                       #   Streamlit UI (backup, still maintained)
│
├── docs/
│   └── ARCHITECTURE.md              #   This file
├── config.example.yaml              #   Credential template (Artifactory, JIRA, Git)
├── projects.json                    #   Per-customer config (git/artifactory/local)
├── requirements.txt                 #   Dependencies
├── run_state/                       #   Persisted state per project (gitignored)
└── output/                          #   Merged artifacts (gitignored)
```

---

## 3. Module dependency graph

```
upgrade-frontend/app.py
        │
        ├──► upgrade_lib.UpgradeClient           (claude_client.py — facade)
        │           │
        │           ├──► MergeAgent               (agents/merge_agent.py)
        │           ├──► DiffAgent                (agents/diff_agent.py)
        │           ├──► ReviewAgent              (agents/review_agent.py)
        │           └──► SummaryAgent             (agents/summary_agent.py)
        │                   │
        │                   └──► BaseAgent         (agents/base_agent.py)
        │                           │
        │                           ├──► claude CLI (subprocess, -p print mode)
        │                           └──► prompts/*.md (system prompts)
        │
        ├──► upgrade_lib.prompts                  (prompts.py)
        │           │
        │           └──► policies/*.md            (loaded at prompt-time)
        │
        ├──► upgrade_lib.compare                  (compare.py — pure stdlib)
        │
        ├──► upgrade_lib.quality                  (quality/)
        │           ├──► QualityGate              (quality_gate.py)
        │           └──► RiskScorer               (risk_scorer.py)
        │                   └──► RiskAgent        (agents/risk_agent.py — no Claude)
        │
        ├──► upgrade_lib.sources                  (sources/)
        │           ├──► GitProvider              (git_provider.py)
        │           ├──► ArtifactoryProvider       (artifactory_provider.py)
        │           └──► LocalProvider            (local_provider.py)
        │
        ├──► upgrade_lib.jira                     (jira/)
        │           ├──► JiraClient               (jira_client.py)
        │           └──► JiraTracker              (jira_tracker.py)
        │
        └──► upgrade_lib.report                   (report/)
                    └──► ReportGenerator          (report_generator.py)
```

No circular imports. `upgrade_lib` knows nothing about Streamlit. `compare.py` knows nothing about Claude.

---

## 4. Agents

### Overview

| Agent | File | Claude? | Input | Output |
|-------|------|---------|-------|--------|
| **MergeAgent** | `agents/merge_agent.py` | Yes | customer + SYSTEM + baseline files | `{merged_files, explanation}` |
| **DiffAgent** | `agents/diff_agent.py` | Yes | merged JSON + SYSTEM JSON | `{diff_json, explanation}` |
| **ReviewAgent** | `agents/review_agent.py` | Yes | merged + customer + SYSTEM files | `{verdict, findings, recommendations}` |
| **RiskAgent** | `agents/risk_agent.py` | No | comparison result + metadata | `{level, score, factors}` |
| **SummaryAgent** | `agents/summary_agent.py` | Yes | all results + risk + quality | Markdown narrative |

### BaseAgent (`agents/base_agent.py`)

Shared transport for every agent:

- `_call(prompt, system=None, *, allowed_tools=..., add_dirs=..., max_turns_override=..., cwd=..., timeout_override=...)` — invokes the `claude` CLI as a subprocess via `claude -p` (print mode). Reliable inside Streamlit (no async event-loop conflicts). Optional kwargs enable tool-using agentic calls.
- `_find_claude_exe()` — locates `claude.exe` directly to bypass shell-script wrappers on Windows.
- Built-in retry with backoff for transient errors. `"Prompt is too long"` is non-retryable.
- `extract_json()` — tolerant JSON parser (strips fences, finds outermost `{}`).
- `UsageStats` — tracks calls across agents.
- Each agent sets `agent_name` and `system_prompt_file` to load from `prompts/*.md`.

### UpgradeClient (`claude_client.py`)

Backward-compatible facade that delegates to individual agents. Creates `_merge`, `_diff`, `_review`, `_summary` agent instances. The `usage` property aggregates stats across all agents.

| Method | Delegates to | Returns |
|--------|-------------|---------|
| `merge_artifact()` | `MergeAgent.merge()` | `{merged_files, explanation}` |
| `generate_diff_json()` | `DiffAgent.generate()` | `{diff_json, explanation}` |
| `review_merge()` | `ReviewAgent.review_markdown()` | `str` (markdown) |
| `review_merge_structured()` | `ReviewAgent.review()` | `{verdict, findings, recommendations}` |
| `chat()` | `MergeAgent.chat()` | `str` |
| `generate_summary()` | `SummaryAgent.summarize_legacy()` | `str` (markdown) |
| `generate_summary_enhanced()` | `SummaryAgent.summarize()` | `str` (markdown with risk/quality) |

---

## 5. Pipeline

```
ScanAgent (local Python) → RiskScorer → MergeAgent → QualityGate → DiffAgent → ReviewAgent → SummaryAgent → ReportGenerator
```

1. **Scan & Compare** — `compare_artifact_local()` does deterministic compare, `RiskAgent.assess_all()` scores risk
2. **Quality Gate** — deterministic checks (conflict markers, JSON/XML validity, duplicate methods) before Claude
3. **Merge** — `MergeAgent` does 3-way merge with policy context from `policies/merge_policy.md`
4. **Diff** — `DiffAgent` regenerates `_diff.json` runtime deltas per `policies/diff_format_spec.md`. Gated in `upgrade_api/merge_util.py`: regeneration only runs when the customer artifact source already contains a `_diff.json`. Bulk "Merge All" sets `skip_diff=True` to keep batch latency low; single-artifact merges default to `skip_diff=False`.
5. **Review** — `ReviewAgent` produces structured PASS/WARN/FAIL with typed findings
6. **Summary** — `SummaryAgent` creates executive narrative with risk distribution + quality stats

---

## 6. Source providers

| Provider | Config Field | Resolves To | Cache |
|----------|-------------|-------------|-------|
| **GitProvider** | `source_type: "git"`, `git_url`, `git_branch` | Cloned repo → `{subpath}/repos` | `~/.wisetrix/git_cache/` |
| **ArtifactoryProvider** | `target_type: "artifactory"`, `artifactory_url` | Downloaded JAR → `app_root/repos/SYSTEM` | `~/.wisetrix/artifactory_cache/` |
| **LocalProvider** | `source_root`, `target_system` | Local paths as-is | None |

### Provider ABC (`sources/base_provider.py`)

```python
class SourceProvider(ABC):
    def resolve(config: dict) -> ResolvedSource: ...
    def validate(config: dict) -> list[str]: ...

@dataclass
class ResolvedSource:
    source_root: Path
    target_system: Path
    baseline_system: Path | None
    metadata: dict  # commit hash, version, timestamps
```

### Backward compatibility

If `source_type` is missing in `projects.json`, the system checks for a `source_root` key and uses `LocalProvider`. Existing local-path projects continue to work without config changes.

---

## 7. Quality gates & risk scoring

### QualityGate (`quality/quality_gate.py`)

Deterministic checks — no Claude calls, runs before `ReviewAgent`:

1. **Conflict markers** — `<<<<<<<`, `=======`, `>>>>>>>>`
2. **JSON validity** — parse all `.json` files
3. **XML validity** — parse all `.xml` files
4. **Duplicate methods** — regex scan for same Java method signature
5. **Import-usage mismatch** — basic regex for unused Java imports

Output: `QualityResult(verdict="PASS"|"WARN"|"FAIL", findings=[], blocking=bool)`

If `FAIL`: blocks merge, skips `ReviewAgent` (saves Claude tokens).

### RiskAgent (`agents/risk_agent.py`)

Purely deterministic scoring factors:

| Factor | Weight | Trigger |
|--------|--------|---------|
| File count | +0.2 | > 5 files |
| Change volume | +0.2 | > 50% files differ |
| High-risk category | +0.2 | `actions`, `custom_privilages`, `datasets` |
| Code files | +0.2 | `.java`, `.js`, `.jsp` present |
| No target in SYSTEM | +0.2 | Source-only artifact |

Score → level: ≥0.5 **HIGH**, ≥0.25 **MEDIUM**, else **LOW**

---

## 8. Data shapes

### Artifact descriptor (from `scan_artifacts`)

```python
{
    "bucket":      "ALDI",
    "category":    "validationsets",
    "subcategory": "REPORT",
    "name":        "BrokerPacketGeneralSearch",
    "rel_path":    "datasets/REPORT/BrokerPacketGeneralSearch",
    "source_rel":  "ALDI/datasets/REPORT/BrokerPacketGeneralSearch",
    "abs_path":    "/full/path/to/dir",
}
```

### Comparison result (one entry in `{project_id}.comparison.json`)

```python
{
    "bucket":       "ALDI",
    "category":     "validationsets",
    "name":         "PostValidationSet",
    "rel_path":     "validationsets/PostValidationSet",
    "source_rel":   "ALDI/validationsets/PostValidationSet",
    "decided_at":   "2026-04-26T12:34:56+00:00",
    "decision":     "Merge" | "Retain" | "Remove",
    "file_decisions": { "validationset.json": "Merge", ... },
    "file_details":   [ { file, decision, target_exists, [compare_result] } ],
    "target_exists":  true,
    "target_path":    "/full/path/to/target",
    "file_count":     2,
    "analysis":       "2 file(s) compared · 1 Merge, 1 No Changes",
    "engine":         "local",
    "decision_note":  "Auto-removed: adhoc_windowdefs counterpart exists in source"
}
```

### Risk assessment (persisted in `{project_id}.risks.json`)

```python
{
    "ALDI/validationsets/PostValidationSet": {
        "level": "MEDIUM",
        "score": 0.4,
        "factors": ["change_volume", "high_risk_category"]
    }
}
```

### Merge record (one entry in `{project_id}.merges.json`)

```python
{
    "bucket":         "ALDI",
    "rel_path":       "validationsets/PostValidationSet",
    "merged_at":      "2026-04-26T12:35:01+00:00",
    "files":          ["validationset.json", "validationset_diff.json"],
    "explanation":    "• Adopted SYSTEM 26.2 baseline\n• Preserved ALDI custom validations...",
    "diff_generated": true,
    "diff_error":     null,
    "out_dir":        "/abs/path/to/output/ALDI/ALDI/validationsets/PostValidationSet"
}
```

---

## 9. The compare engine (`upgrade_lib/compare.py`)

### Configuration constants

```python
INCLUDE_EXTS       = {".json", ".xml", ".java", ".js", ".jsp"}
EXCLUDE_NAMES      = {"component.info", "security.txt"}
RETAIN_CATEGORIES  = {"custom_privilages"}    # path segments that short-circuit to Retain
```

### Per-file comparators

| Function | Strategy |
|----------|----------|
| `_compare_json` | `json.loads` both sides → equality (semantic — key order ignored). Fallback to stripped-text equality on parse error. |
| `_compare_xml`  | Recursively normalize the tree (sorted attributes, stripped text/tail) → equality. Fallback to non-empty-line list equality. |
| `_compare_code` | Normalize line endings + strip → equality. Then `_is_comment_only_change` for diff that's purely `//` / `/*` markers. |

### `apply_business_rules`

Mutates the results dict in place. Two rules — both **bucket-scoped**:

1. `windowdefs/{name}` → `Remove` if `adhoc_windowdefs/{name}` exists in same bucket.
2. `datasets/SEARCH/ADHOC_SEARCH_{X}` → `Remove` if `datasets/REPORT/{X}` exists in same bucket.

---

## 10. The prompts layer (`upgrade_lib/prompts.py`)

### Policy loader

`_load_doc(name)` loads from `policies/` directory, cached in `_POLICY_CACHE`. To pick up edits without restarting Streamlit, restart the process.

### Per-agent system prompts

Each agent loads its system prompt from `upgrade_lib/prompts/{agent_name}_system.md` via `BaseAgent._load_system_prompt()`. These are focused, per-task prompts — not the shared `SYSTEM_BASE` from v1.

### Prompt templates

| Constant | Used by | Required keys |
|----------|---------|---------------|
| `DIFF_JSON_PROMPT` | DiffAgent | diff_format_spec, merged_json, system_json, artifact_name, merged_name, system_name |
| `REVIEW_PROMPT` | ReviewAgent (legacy markdown mode) | customer, merged_files, aldi_files, system_files |
| `CHAT_PROMPT` | MergeAgent.chat() | merge_policy, context, question |
| `SUMMARY_PROMPT` | SummaryAgent (legacy mode) | all_results |

The merge agent does **not** use a constant in `prompts.py`. Its prompt is defined inline in `merge_agent.py` as `_MERGE_TOOLS_PROMPT` because it instructs Claude to use the Read/Write tools against a working directory — different shape from the inline-content templates above.

---

## 11. The Claude transport

### Subprocess, not async SDK

`BaseAgent._call()` invokes the `claude` CLI directly via `subprocess.run`:

```
claude -p --model <model> --output-format text \
       --system-prompt-file <path> --max-turns <n> \
       --permission-mode bypassPermissions \
       [--allowedTools Read,Write,...] [--add-dir <workdir>]
```

Why subprocess instead of `claude_agent_sdk.query()`:
- The SDK is async and conflicted with Streamlit's event-loop on Windows.
- `subprocess.run` is reliable in any context (Streamlit, CLI, threads).
- The prompt is passed via stdin to avoid Windows command-line length limits (`WinError 206`).

### Tool-using calls (merge agent only)

When `_call()` is invoked with `allowed_tools`, `add_dirs`, and elevated `max_turns_override`, Claude can use Read/Write/Glob/LS against the working directory. This is the path the merge agent uses to handle artifacts that exceed the inline-prompt size limit.

### `extract_json` — tolerant response parser

Strips ``` ```json ``` fences and finds the outermost `{ ... }` substring. Tolerant of preamble/postamble prose Claude sometimes emits.

---

## 12. The UI layer

There are two UIs against the same `upgrade_lib` engine. The Next.js +
FastAPI stack is primary; Streamlit is preserved as a backup.

### 12a. FastAPI service (`upgrade_api/`) + Next.js (`upgrade-web/`)

The Next.js UI is a thin client over a FastAPI service. FastAPI sits
between the UI and `upgrade_lib`, exposing a REST + SSE surface per UI tab.

```
Browser  ──HTTP/SSE──▶  FastAPI (uvicorn :8000)  ──Python calls──▶  upgrade_lib
```

#### Routers (one per UI tab)

| Router | File | Endpoints |
|---|---|---|
| Projects | `routers/projects.py` | CRUD on `projects.json` |
| Providers | `routers/providers.py` | Test git/artifactory connections |
| Scan | `routers/scan.py` | `/artifacts`, `/comparison`, `/compare/stream` (SSE) |
| Merges | `routers/merges.py` | List merges, `/{key}/stream` (SSE), `/stream` (Merge All SSE), downloads |
| Diff | `routers/diff.py` | Serve `_diff.json` side-by-side data |
| Summary | `routers/summary.py` | Aggregates, narrative regen, report build, PDF download |
| JIRA | `routers/jira.py` | Status, issues, start, sync, match, finalize |
| Health | `routers/health.py` | Liveness |

#### Caches and performance

- **Persisted resolved paths** — `run_state/{project_id}.resolved.json`
  caches the output of `resolve_project_paths()` (a usable
  `source_root` / `target_system` / `baseline_system` triple).
  Subsequent merges and `/artifacts` calls reuse it via the
  `_resolved_paths_are_usable()` validator. The merge SSE emits
  `resolve_cache_hit` / `resolve_cache_miss` with diagnostic fields so
  the UI can show exactly which path was taken.
- **GitProvider `skip_pull` fast-path** — when the clone already exists,
  resolve returns immediately with no network call. Falls back to the
  full pull when a refresh is needed.
- **GitProvider targeted fetch** — uses `git fetch origin <branch>
  --no-tags` + `reset --hard` instead of `fetch --all --prune` + `pull`,
  cutting two network roundtrips to one.
- **ArtifactoryProvider sidecar** — `cache_dir/.wisetrix_system_path`
  caches the result of the `app_root/repos/SYSTEM` lookup so we never
  `rglob` the extracted JAR more than once.

#### SSE phase events

`upgrade_api/merge_util.py` calls a `progress_cb(phase, extra)` at each
step of the merge pipeline. The SSE generator pushes those onto an
`asyncio.Queue` from a worker thread via `loop.call_soon_threadsafe`,
and yields them to the client as `phase` events. See
`upgrade-web/README.md` for the full label set.

#### Threading model

Blocking work (Claude calls, git/artifactory I/O, filesystem walks) runs
in worker threads via `anyio.to_thread.run_sync` so the FastAPI event
loop stays responsive and can stream progress events concurrently.

#### Browser ↔ FastAPI quirks

- **Next dev rewrites gzip** SSE responses; Chrome buffers the gzip
  stream and the UI looks frozen. All SSE URLs in `upgrade-web/lib/api.ts`
  use `STREAM_BASE` (absolute FastAPI URL) instead of `/api/...` for this
  reason.
- **Next dev rewrites reset sockets at ~30s.** Long-running endpoints
  (narrative generation, anything with a Claude call > 30s) use
  `requestDirect()` in `upgrade-web/lib/api.ts` to bypass the rewrite.

Full developer notes live in `upgrade-web/README.md`.

### 12b. Streamlit UI (`upgrade-frontend/app.py`)

Still maintained for parity / fallback. Same engine, same 8-tab layout.

#### 8 tabs

| Tab | Purpose |
|-----|---------|
| **Summary** | Risk distribution, quality gate stats, UPGRADE_REPORT.md generation + download |
| **Setup** | Git/Artifactory/local config, test connections, save/delete projects |
| **Scan & Compare** | Run compare + risk scoring, filter by decision/risk level, JIRA column |
| **Merge Queue** | Risk badges per artifact, quality gate blocking, merge-all |
| **Diff Viewer** | Side-by-side `_diff.json` inspection |
| **Review & Edit** | Structured PASS/WARN/FAIL + legacy markdown review |
| **JIRA** | Epic management, ticket lookup, bulk subtask creation, sync to JIRA |
| **Chat** | Free-form Q&A with risk context |

### Source resolution

`resolve_project_paths()` dispatches to providers based on config:
- `source_type: "git"` → `GitProvider`
- `target_type: "artifactory"` → `ArtifactoryProvider`
- Missing `source_type` + has `source_root` → `LocalProvider`

### Persistence layer

```
run_state/{project_id}.comparison.json
run_state/{project_id}.merges.json
run_state/{project_id}.summary.json
run_state/{project_id}.risks.json
run_state/{project_id}.jira_tracker.json
run_state/{project_id}.jira_tickets.json
```

Read via `load_json(path, default)`, written via `save_json(path, data)`. UTF-8, indent=2, `ensure_ascii=False`.

---

## 13. Lifecycle of a single merge

```
1. User clicks "Merge" on row "ALDI/validationsets/PostValidationSet"

2. _perform_merge():
   a. Read aldi_files     from source_root/ALDI/validationsets/PostValidationSet/
   b. Read system_files   from target_root/validationsets/PostValidationSet/
   c. Read baseline_files from baseline_root/validationsets/PostValidationSet/ (if exists)
   d. MergeAgent.merge(customer_files, system_files, baseline_files, customer="ALDI")
        ├── Stages files into a temp workdir: customer/, system/, baseline/, merged/
        ├── Loads merge_system.md as system prompt
        ├── Formats _MERGE_TOOLS_PROMPT with policy + working-dir paths
        ├── Invokes claude CLI (subprocess) with Read/Write/Glob/LS tools allowed
        ├── Claude reads each file set, applies merge_policy.md, writes to merged/
        └── Reads merged/ back into a dict → returns {merged_files, explanation}
   e. Write merged files to output/ALDI/validationsets/PostValidationSet/
   f. QualityGate.check(merged_files)
        └── If FAIL → block, flag in UI
   g. For JSON files: DiffAgent.generate(merged_json, system_json, artifact_name)
        └── Write _diff.json
   h. Save merge record to merges.json

3. UI reruns → row moves from Pending to Completed
```

---

## 14. Running without the UI

```python
from pathlib import Path
from upgrade_lib import (
    UpgradeClient, compare_artifact_local, apply_business_rules,
    MergeAgent, QualityGate, RiskAgent,
)

# 1. Compare (no Claude)
result = compare_artifact_local(
    Path("repos/ALDI/validationsets/PostValidationSet"),
    Path("system26.2/validationsets/PostValidationSet"),
    "validationsets/PostValidationSet",
)

# 2. Risk score (no Claude)
risk = RiskAgent().assess(result)
print(risk.level, risk.score, risk.factors)

# 3. Merge via Claude (direct agent)
agent = MergeAgent()
merged = agent.merge(aldi_files, system_files, baseline_files, customer="ALDI")

# 4. Quality gate (no Claude)
qr = QualityGate().check(merged["merged_files"])
print(qr.verdict, qr.findings)

# 5. Or use the facade
client = UpgradeClient()
merged = client.merge_artifact(aldi_files, system_files, baseline_files, customer="ALDI")
```

---

## 15. JIRA integration (`upgrade_lib/jira/`)

### JiraClient (`jira/jira_client.py`)

Thin wrapper over `atlassian-python-api`'s `Jira` class. Config from `config.yaml`:

```yaml
jira:
  enabled: true
  base_url: "https://jira.dev.e2open.com/jira"
  username: "your_username"
  password: "your_password"
```

All methods are safe when JIRA is disabled — they return `None`/`False`/`[]`.

Key operations:
- `test_connection()` — verify credentials
- `get_issue()` / `search()` — fetch issues by key or JQL
- `create_issue()` — create tasks, epics, or subtasks
- `add_comment()` / `transition_issue()` — update existing issues
- `find_tickets_for_artifacts()` — batch-lookup JIRA tickets by artifact name
- `match_issues_to_artifacts()` — smart keyword scoring against issue summary+description. Returns **all** issues with score ≥ 1 as a comma-separated string per artifact, sorted by score desc then key asc. Keyword extraction expands GTM abbreviations (PCE→Pre Customs Entry, BP→Broker Packet, etc.) from `_GTM_ABBREVIATIONS`.

### JiraTracker (`jira/jira_tracker.py`)

Maps pipeline events to JIRA operations:

| Event | JIRA Action |
|-------|-------------|
| `start_run()` | Create/find Epic, add metadata comment |
| `log_scan()` | Comment with scan summary (decision + risk counts) |
| `log_merge()` | Create subtask under Epic, add merge explanation |
| `log_review()` | Comment on subtask with review findings |
| `finalize()` | Final comment with report, optionally transition Epic |
| `create_bulk_subtasks()` | Create subtasks for all Merge artifacts at once |

State persisted in `run_state/{project_id}.jira_tracker.json`.

### Artifact ↔ JIRA mapping

The JIRA tab provides "Lookup JIRA Tickets" which searches JIRA for existing tickets matching each artifact name. Results are shown:
- In the Scan & Compare table (JIRA column)
- In UPGRADE_REPORT.md (Artifact Overview table + Merge Details)

---

## 16. Report generation (`upgrade_lib/report/`)

`ReportGenerator.generate()` produces `UPGRADE_REPORT.md` with these sections:

| Section | Content |
|---------|---------|
| Header | Project ID, generation date, run metadata |
| Artifact Overview | Full table: artifact, bucket, decision, risk, JIRA ticket |
| Decision Summary | Merge/Retain/Remove counts with percentages |
| Risk Distribution | HIGH/MEDIUM/LOW counts with percentages |
| Quality Gate Results | PASS/WARN/FAIL counts, findings from problem merges |
| High-Risk Merges | Table of HIGH-risk merged artifacts for manual review |
| Merge Details | Per-artifact: bucket, JIRA, files, _diff.json status, explanation |
| AI Recommendations | SummaryAgent narrative (if generated) |
| JIRA Tracking | Epic key + subtask mapping table |

`ReportGenerator.save()` writes to `output/{project_id}/UPGRADE_REPORT.md`.

The Summary tab provides a "Generate UPGRADE_REPORT.md" button + download.

---

## 17. Extending the system

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

---

## 18. Code-review checklist

- [ ] No algorithmic merge/diff/dedup code in Python.
- [ ] No hardcoded customer names.
- [ ] Bucket-aware paths: `source_root / bucket / rel`.
- [ ] Quality gate checks are deterministic (no Claude calls).
- [ ] Policy docs stay in `policies/`, not embedded in Python.
- [ ] Tested with at least one non-ALDI customer.
- [ ] JIRA operations are safe when disabled (no-op, returns None/False).
- [ ] If a new agent uses tools, `allowed_tools=` is passed to `BaseAgent._call()` explicitly.

---

## 19. Why no MCP server?

v1 used a Model Context Protocol server with ~2400 lines of Python implementing JSON deep-merge, comment-aware code diff, dedup, etc. Every bug was a semantic-misunderstanding bug.

v2 deletes all of that. Compare is the one place where determinism wins. Everything else goes to Claude with the policy doc inline.

| | v1 | v2 |
|---|----|----|
| Python LOC | ~2400 | ~1600 |
| Agents | 1 (monolithic) | 5 (specialized) + facade |
| Source integration | Local paths only | Git + Artifactory + local |
| Risk scoring | None | Deterministic per-artifact |
| Quality gates | None | 5 checks, pre-Claude |
| JIRA integration | None | Epic/subtask tracking, ticket lookup |
| Upgrade report | None | UPGRADE_REPORT.md with risk, quality, JIRA |
| Merge-rule changes | Code edit + redeploy | One-line edit in `policies/merge_policy.md` |
| API key required | ANTHROPIC_API_KEY | None (Claude Code OAuth) |
| Customer onboarding | Code changes | Setup tab, zero code |
