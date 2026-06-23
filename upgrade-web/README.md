# upgrade-web

Next.js 14 (App Router) + Tailwind + shadcn-style components.
Talks to the FastAPI service in `upgrade_api/` for everything. This is the
project's UI.

---

## Run (dev)

In two terminals from the repo root:

```bash
# 1) FastAPI service on :8000
uvicorn upgrade_api.main:app --reload --port 8000

# 2) Next.js dev server on :3000
cd upgrade-web
npm install
npm run dev
```

Open <http://localhost:3000>.

---

## Configuration

```bash
cp .env.local.example .env.local
```

| Env var | Purpose | Default |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | Absolute base URL of the FastAPI service. **Browser** must be able to reach this — it's used directly for SSE streams and long-running calls (see below). | `http://localhost:8000` |

For local dev the default works. When deploying, set `NEXT_PUBLIC_API_BASE`
to the public FastAPI URL and make sure CORS on the FastAPI side allows the
browser origin (`upgrade_api/main.py:46` currently uses `cors_origins()`).

---

## API access patterns — read this before adding endpoints

Most calls go through the Next dev rewrite at `/api/*` → FastAPI.
Two cases must **bypass** the rewrite and hit FastAPI directly:

### 1. SSE streams — always use `STREAM_BASE`

The Next dev proxy gzips responses for any browser that sends
`Accept-Encoding: gzip` (which Chrome always does). Chrome's gzip decoder
holds SSE messages in a buffer until the stream closes — making the UI
look frozen for minutes during merges.

The fix is to point `EventSource` at FastAPI directly:

```ts
// lib/api.ts
const STREAM_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

mergeOneStreamUrl: (id: string, key: string) =>
  `${STREAM_BASE}/projects/${encodeURIComponent(id)}/merges/${encodeURIComponent(key)}/stream`,
```

All `*StreamUrl()` helpers in `lib/api.ts` follow this pattern. **If you
add a new SSE endpoint, use `STREAM_BASE` — do not use `/api/...`.**

Symptom if you forget: status code 200, `content-encoding: gzip` in the
response headers, EventStream tab in DevTools empty until the stream
closes.

### 2. Long-running fetches — use `requestDirect`

The Next dev proxy resets sockets at ~30s. Any endpoint that does a Claude
SDK call (narrative generation, JIRA match, anything in the 30s+ range)
must bypass the proxy too. Use `requestDirect<T>()` from `lib/api.ts`
instead of `request<T>()`:

```ts
regenerateNarrative: (id: string) =>
  requestDirect<NarrativeRecord>(
    `/projects/${encodeURIComponent(id)}/summary/narrative`,
    { method: "POST" },
  ),
```

Symptom if you forget: `ECONNRESET` / `socket hang up` errors in the
Next.js terminal at the 30-second mark, even though the FastAPI handler
is still running fine.

---

## Project structure

```
upgrade-web/
├── app/
│   ├── layout.tsx                    # Root layout + Toaster
│   ├── page.tsx                      # Landing — health check + project list
│   └── projects/
│       ├── new/page.tsx              # Create project
│       └── [id]/
│           ├── setup/page.tsx        # Configure source/target/baseline
│           ├── scan/page.tsx         # Scan & Compare (SSE stream)
│           ├── merges/page.tsx       # Merge queue (SSE stream)
│           ├── diff/page.tsx         # _diff.json side-by-side viewer
│           ├── jira/page.tsx         # JIRA match table
│           └── summary/page.tsx      # AI narrative + downloadable report
├── components/
│   ├── markdown.tsx                  # ReactMarkdown wrapper (no @tailwindcss/typography)
│   └── ui/                           # shadcn-style primitives (Button, Card, …)
├── lib/
│   └── api.ts                        # Fetch + SSE URL builders
├── next.config.mjs                   # /api/* → NEXT_PUBLIC_API_BASE rewrite
└── tailwind.config.ts
```

---

## SSE event reference

Pages subscribe to FastAPI's SSE streams and update local state per phase.
The full set of `phase` event names the UI currently understands:

### Scan & Compare (`/projects/{id}/compare/stream`)
- `resolve`, `scan`, `compare`, `rollup`, `risk`, `done`

### Merge stream (`/projects/{id}/merges/stream` and `/.../{key}/stream`)
Resolve phase:
- `resolve` — initial state
- `resolve_cache_hit` — used persisted `run_state/{id}.resolved.json`
- `resolve_cache_miss` — cache rejected; running git/artifactory full resolve
- `resolve_done` — resolve complete

Per-artifact phases:
- `reading` — staging customer + system + baseline files
- `claude_merge_start` / `claude_merge_done` — the actual Claude merge call
- `written` — merged files written to disk
- `quality_start` / `quality_done` — deterministic quality gate
- `diff_start` / `diff_done` / `diff_failed` — `_diff.json` regeneration
- `diff_skipped` — customer artifact had no `_diff.json`; regeneration skipped
  (rule: only regenerate when customer source already contains one)

If you add a new phase event in the backend, add the human-readable label
to the `PHASE_LABELS` map at the top of the page file that consumes it
(`merges/page.tsx` or `scan/page.tsx`). Unknown phases fall back to the
raw event name.

---

## Markdown rendering

`components/markdown.tsx` wraps `react-markdown` + `remark-gfm` with
Tailwind utility classes (no `@tailwindcss/typography` plugin). Use it
anywhere the backend hands back markdown content (narrative, review,
chat).

```tsx
import { Markdown } from "@/components/markdown";

<Markdown>{narrative.content}</Markdown>
```

Don't dump markdown content into `<pre>` — the raw `**bold**` / `#` syntax
will show literally instead of rendering.

---

## Status

Feature-complete:
- Setup, Scan & Compare, Merge (single + bulk with SSE progress),
  Diff Viewer, JIRA match, Summary narrative, downloadable Upgrade Report.

Not yet implemented:
- Chat tab
- Review & Edit tab (Claude markdown review + inline file editor)
- Manual JIRA ticket key override
- Persisted merge-failure list
