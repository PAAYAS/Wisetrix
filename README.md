# Wisetrix — AI-assisted GTM upgrade agent

Wisetrix helps you move a customer's GTM customizations onto a new SYSTEM release. It scans the customer's source tree, decides per artifact whether to **Merge / Retain / Remove**, and lets Claude perform the actual 3-way merges using rules written in plain English — then produces a reviewed, reportable upgrade you can check into git and build.

It runs on your existing **Claude Code** login. No `ANTHROPIC_API_KEY` to manage.

---

## Why Wisetrix

- **3-way merges, done by AI** — customer + new SYSTEM + previous baseline, merged per `policies/merge_policy.md`. No hand-written merge code to maintain.
- **Deterministic where it counts** — comparison, large-JSON merges, and the `_diff.json` runtime delta are handled by fast, exact Python. The LLM is used only where semantic judgement earns its keep.
- **Pulls sources for you** — Git (Bitbucket), JFrog Artifactory (release JARs), or local paths.
- **Risk scoring & quality gates** — every artifact is risk-scored and passed through deterministic quality checks (valid JSON/XML, no conflict markers, resolvable Java symbols) before it's trusted.
- **DB seed-data reconciliation** — for `bizpolicydefs`, the app decision drives the matching `bppol` xlsx action in the DB repo.
- **JIRA & reporting** — match artifacts to tickets, track the run as an Epic + subtasks, and generate an `UPGRADE_REPORT.md` (with PDF export).
- **Add a customer with zero code** — projects are configured from the UI.

---

## How it works

```
   Customer source         New SYSTEM (26.2)       Baseline (prev SYSTEM)
        │                        │                        │
        └───────────┬────────────┴────────────────────────┘
                    ▼
          ┌────────────────┐
          │  1. Compare    │  Local Python — fast, deterministic
          │  (per artifact)│  → Merge / Retain / Remove + risk score
          └────────┬───────┘
                   ▼
          ┌────────────────┐
          │  2. Merge      │  Claude does the 3-way merge per merge_policy.md.
          │  (3-way)       │  Large JSON & _diff.json handled deterministically.
          └────────┬───────┘
                   ▼
          ┌────────────────┐
          │ 3. Quality gate│  Deterministic PASS / WARN / FAIL before review
          └────────┬───────┘
                   ▼
          ┌────────────────┐
          │ 4. Review &    │  AI review + executive narrative + UPGRADE_REPORT.md
          │    Report      │
          └────────────────┘
```

---

## Prerequisites

1. **Python 3.10+**
2. **Node.js 18+** — for the primary Next.js UI.
3. **Claude Code CLI** — the AI agents run through it, so no separate API key is needed:
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude login
   claude --version   # verify
   ```

---

## Install & run

```bash
pip install -r requirements.txt

# terminal 1 — API service
uvicorn upgrade_api.main:app --port 8000

# terminal 2 — web UI
cd upgrade-web && npm install && npm run dev
```

Open **<http://localhost:3000>**.

---

## First run

1. **Configure credentials.** Copy the template and fill in your Artifactory / JIRA / Git details:
   ```bash
   cp config.example.yml config.yaml
   ```
   `config.yaml` is gitignored — never commit credentials. See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

2. **Add a project.** In the UI, open **Add / Update Project** and provide the source (git/artifactory/local), the target SYSTEM release, and the baseline. No code changes — the customer bucket is auto-detected. Field reference: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

3. **Run the upgrade workflow:**
   | Step | Tab | What happens |
   |------|-----|--------------|
   | 1 | **Scan & Compare** | Resolves sources, compares every artifact, assigns Merge/Retain/Remove + a risk level. |
   | 2 | **Merges** | Merge one artifact or **Merge All**. Output lands under `output/<project>/`. |
   | 3 | **Diff Viewer** | Inspect `_diff.json` runtime deltas side by side. |
   | 4 | **JIRA** | Match merged artifacts to tickets; optionally create an Epic + subtasks. |
   | 5 | **Summary** | Generate the AI narrative and download `UPGRADE_REPORT.md` (Markdown + PDF). |

4. **Check in & build.** Commit the merged `output/` artifacts to the customer repo and run your Jenkins build.

---

## Key concepts

| Term | Meaning |
|------|---------|
| **Bucket** | A top-level folder under the source root. `SYSTEM` is special; everything else is a customer (AGCO, EMRSN, …). |
| **Artifact** | A directory of related files (e.g. `integration_def.json` + its `_diff.json` + `integration_logic.java`). |
| **Merge / Retain / Remove** | Per-artifact decision. *Merge* = combine customer + SYSTEM; *Retain* = keep the customer version; *Remove* = redundant. |
| **3-way merge** | Customer + new SYSTEM + baseline (the common ancestor that tells us who actually changed what). |
| **`_diff.json`** | A runtime delta re-applied on top of the SYSTEM base at load time. Format in [`policies/diff_format_spec.md`](policies/diff_format_spec.md). |

---

## Troubleshooting

**`claude` command not found / not logged in**
Install the Claude Code CLI and run `claude login` once.

**Scan shows 0 Merges**
The target SYSTEM must contain the same `{category}/{artifact}` layout as the source. The Scan & Compare tab shows which top-level folders were detected as buckets.

**A merge produced no output / a `.java` failed to compile after check-in**
Re-run the merge. Large JSON and `_diff.json` are handled deterministically and excluded from the AI step, so the AI focuses only on the code/small files — re-running a single artifact is fast and reliable.

**Web UI progress bar frozen, or `ECONNRESET` on long actions**
A dev-proxy quirk, not a real failure — see [`upgrade-web/README.md`](upgrade-web/README.md) for the SSE / long-request patterns.

**Git fetch fails with `non-fast-forward`**
Handled automatically (the targeted fetch uses `--force` on the tracking ref). If you still see it, your local cache under `~/.wisetrix/git_cache/` may be wedged — delete that repo's cache folder and re-scan.

More detail and developer-facing fixes live in [`docs/DEVELOPER.md`](docs/DEVELOPER.md).

---

## Documentation

| Doc | For |
|-----|-----|
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Project & credential configuration reference |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the engine is built (engineering reference) |
| [`docs/DEVELOPER.md`](docs/DEVELOPER.md) | Where to change things, extension points, checklist |
| [`policies/merge_policy.md`](policies/merge_policy.md) | The merge rules (plain-English source of truth) |
| [`policies/diff_format_spec.md`](policies/diff_format_spec.md) | `_diff.json` format |
| [`upgrade-web/README.md`](upgrade-web/README.md) | Next.js frontend dev guide |
