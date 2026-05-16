# Upgrade Agent v2

An AI-powered tool that helps customers (ALDI, EMRSN, ABT, …) move their GTM customizations onto a new SYSTEM release.

It scans the customer's source tree, decides per-artifact whether to **Merge / Retain / Remove**, and lets Claude do the actual 3-way merges using rules written in plain English. No `ANTHROPIC_API_KEY` needed — it uses Claude Code's existing OAuth session.

---

## How it works (high level)

```
   Customer source         SYSTEM 26.2          Baseline 24.4.11
        │                       │                       │
        └──────────┬────────────┴───────────────────────┘
                   ▼
          ┌────────────────┐
          │   1. Compare   │  Local Python — fast, deterministic
          │ (decide each   │  Outputs: Merge / Retain / Remove per artifact
          │  artifact)     │
          └────────┬───────┘
                   ▼
          ┌────────────────┐
          │   2. Merge     │  Claude reads files, applies merge_policy.md,
          │  (3-way)       │  writes merged output. NO Python merge logic.
          └────────┬───────┘
                   ▼
          ┌────────────────┐
          │ 3. _diff.json  │  Claude regenerates the runtime delta file
          └────────┬───────┘
                   ▼
          ┌────────────────┐
          │  4. Review     │  PASS / WARN / FAIL quality check
          │  & Summary     │  Markdown narrative of the run
          └────────────────┘
```

The split is intentional:
- **Comparison** is mechanical (JSON / XML / code equality) → plain Python.
- **Merging, review, summarization** need semantic understanding → Claude.

---

## Prerequisites

1. **Node.js 18+** — required for the Next.js frontend and the Claude Code CLI.
2. **Claude Code CLI** — install globally, then log in:
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude login
   ```
   Verify with `claude --version`. The AI merge/review/summary agents run through Claude Code — no separate `ANTHROPIC_API_KEY` needed once logged in.
3. **Python 3.10+**
4. That's it — no Docker.

---

## Quick start

There are two UIs against the same engine. Pick one:

### Next.js + FastAPI (primary)

```bash
pip install -r requirements.txt

# terminal 1 — FastAPI service
uvicorn upgrade_api.main:app --reload --port 8000

# terminal 2 — Next.js dev server
cd upgrade-web && npm install && npm run dev
```

Open <http://localhost:3000>. See [`upgrade-web/README.md`](upgrade-web/README.md)
for the SSE bypass + long-running call patterns developers need to know.

### Streamlit (backup, still supported)

```bash
pip install -r requirements.txt
streamlit run upgrade-frontend/app.py
```

Browser opens at <http://localhost:8501>.

### Workflow (either UI)

1. **Setup** / Sidebar → fill in source, target, baseline.
2. **Scan & Compare** → **Run Compare** (SSE-streamed progress).
3. **Merges** → **Merge** one row, or **Merge All**.
4. **Diff Viewer** for side-by-side comparison of `_diff.json` files.
5. **JIRA** tab → match merged artifacts to tickets.
6. **Summary** → generate the AI narrative and download the Upgrade Report (Markdown + PDF).

---

## Project layout

```
ta-ai-docker-upgrade-v2/
├── README.md                    ← you are here
├── requirements.txt
├── projects.json                ← per-customer configs (managed via the UI)
│
├── policies/                    ← plain-English rules (the source of truth)
│   ├── merge_policy.md          ←   how files get merged
│   └── diff_format_spec.md      ←   _diff.json format
│
├── upgrade_lib/                 ← Python engine
│   ├── compare.py               ←   deterministic compare
│   ├── claude_client.py         ←   facade over the agents
│   ├── prompts.py               ←   prompt templates + policy loader
│   ├── agents/                  ←   one agent per task (merge, diff, review, …)
│   ├── prompts/                 ←   per-agent system prompts
│   ├── quality/                 ←   deterministic quality gate + risk scoring
│   ├── sources/                 ←   git / artifactory / local source providers
│   ├── jira/                    ←   JIRA integration
│   └── report/                  ←   markdown report generator
│
├── upgrade_api/                 ← FastAPI service (drives the Next.js UI)
│   ├── main.py                  ←   app entry + CORS
│   ├── routers/                 ←   one module per UI tab
│   └── merge_util.py            ←   merge orchestration + SSE phase emission
│
├── upgrade-web/                 ← Next.js 14 UI (primary)
├── upgrade-frontend/app.py      ← Streamlit UI (backup)
├── docs/ARCHITECTURE.md         ← deeper engineering reference
├── .claude/skills/              ← Claude Code skill manifest
├── run_state/                   ← persisted run state (gitignored)
└── output/                      ← merged artifacts (gitignored)
```

---

## Configuration

A project entry in `projects.json` looks like:

```json
{
  "ALDI": {
    "source_root":      "C:/.../ALDI/.../app_root/repos",
    "target_system":    "C:/.../26.2 Jars/.../app_root/repos/SYSTEM",
    "baseline_system":  "C:/.../24.4.11 Jars/.../app_root/repos/SYSTEM",
    "merge_output_dir": "C:/.../output/ALDI"
  }
}
```

| Field | Purpose |
|-------|---------|
| `source_root` | Root of the customer's customizations |
| `target_system` | The new SYSTEM release (e.g. 26.2) |
| `baseline_system` | The previous SYSTEM the customer was on (enables 3-way merge) |
| `merge_output_dir` | Where merged artifacts get written |

The UI's **Add / Update Project** form manages this file for you.

### Adding a new customer

1. Sidebar → **Add / Update Project**.
2. Pick a project ID (e.g. `EMRSN`), paste the four paths, save.
3. Run Scan & Compare, then Merge.

No code changes — bucket detection finds the customer's top-level folder automatically.

---

## How merging actually works

When the merge button is pressed for an artifact:

1. The Python side stages the three file sets to a temp working directory:
   - `customer/` — customer's files
   - `system/`   — SYSTEM 26.2 files
   - `baseline/` — baseline files (common ancestor, optional)
   - `merged/`   — empty; Claude writes here
2. Claude is invoked with `Read`, `Write`, `Glob`, `LS` tools.
3. Claude follows `policies/merge_policy.md` to do the 3-way merge and writes each merged file under `merged/`.
4. Python reads the `merged/` directory back into a dict and persists it.

**Important:** all merge logic lives in Claude + `merge_policy.md`. Python only stages files in and reads files out — it makes no merge decisions.

---

## Where to make changes

| You want to change… | Edit this |
|---------------------|-----------|
| A merge rule (what wins, how arrays merge, primary keys) | `policies/merge_policy.md` |
| `_diff.json` output format | `policies/diff_format_spec.md` |
| When `_diff.json` regeneration runs | `upgrade_api/merge_util.py` (currently: only when customer artifact already contains a `_diff.json`) |
| The merge agent's instructions to Claude | `upgrade_lib/agents/merge_agent.py` (`_MERGE_TOOLS_PROMPT`) |
| A per-agent system prompt | `upgrade_lib/prompts/<agent>_system.md` |
| Compare logic / business rules | `upgrade_lib/compare.py` |
| FastAPI endpoint / SSE stream | `upgrade_api/routers/<tab>.py` |
| Next.js UI page | `upgrade-web/app/projects/[id]/<tab>/page.tsx` |
| SSE phase label shown to users | `PHASE_LABELS` at the top of the consuming page (`merges/page.tsx`, `scan/page.tsx`) |
| Streamlit UI / tabs / persistence | `upgrade-frontend/app.py` |
| New artifact extensions in scan | `upgrade-frontend/app.py` (`ARTIFACT_INCLUDE_EXTS`) |
| JIRA artifact-to-ticket matching | `upgrade_lib/jira/jira_client.py` (`match_issues_to_artifacts` — currently returns all keyword-scored tickets, comma-separated) |

**Rule of thumb:** if you find yourself writing Python merge code, you're in the wrong file. Edit `merge_policy.md` instead.

---

## Troubleshooting

**`claude` command not found**
Install the Claude Code CLI and run `claude` once to log in.

**Merge produces no files**
The agent's response is parsed loosely, but if `merged/` is empty something went wrong on Claude's side. Re-run the merge — the prompt asks Claude to write each file with the `Write` tool.

**Compare shows 0 Merges**
Verify `target_system` actually contains the same `{category}/{artifact}` layout as `source_root`. The Scan & Compare tab shows which top-level directories were detected as buckets.

**Streamlit cache holding stale results**
Hit "R" in the browser, or restart `streamlit run`.

**Next.js UI: SSE progress bar frozen at "Resolving sources…" for minutes**
The Next dev rewrites proxy gzips streamed responses, and Chrome buffers
gzip until the stream ends. Make sure `EventSource` URLs use `STREAM_BASE`
(absolute FastAPI URL) — see `upgrade-web/lib/api.ts` and the README in
`upgrade-web/` for the full pattern.

**Next.js UI: `ECONNRESET` / `socket hang up` on narrative or JIRA match**
The Next dev proxy resets sockets at ~30s. Long-running endpoints that
call Claude must use `requestDirect()` in `upgrade-web/lib/api.ts`
instead of `request()`.

**Merge says "No `_diff.json` in customer artifact · skipping diff"**
That's intentional — `_diff.json` regeneration only runs when the
customer source for an artifact already contains a `_diff.json`. If you
want it regenerated for every artifact regardless, change the gate in
`upgrade_api/merge_util.py`.

---

## Glossary

| Term | Meaning |
|------|---------|
| **Bucket** | Top-level directory under `source_root`. `SYSTEM` is special; everything else is a customer (ALDI, EMRSN, …). |
| **Artifact** | A directory of related files (e.g. `validationset.json` + its `_diff.json`). |
| **Merge / Retain / Remove** | Per-artifact decision. Merge = needs combining; Retain = keep customer version; Remove = redundant. |
| **3-way merge** | Customer + new SYSTEM + baseline (common ancestor) — the baseline tells us who actually changed what. |
| **`_diff.json`** | Runtime delta applied on top of the SYSTEM base at load time. Format in `policies/diff_format_spec.md`. |

---

## See also

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — engineering reference
- [`policies/merge_policy.md`](policies/merge_policy.md) — merge rules
- [`policies/diff_format_spec.md`](policies/diff_format_spec.md) — `_diff.json` format
