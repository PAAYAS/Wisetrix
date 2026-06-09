"use client";

import Link from "next/link";
import * as React from "react";
import {
  ArrowLeft,
  Download,
  FileText,
  GitCompare,
  GitMerge,
  Loader2,
  Package,
  Play,
  RefreshCcw,
} from "lucide-react";
import { toast } from "sonner";

import {
  api,
  ApiError,
  type ComparisonResult,
  type MergeListResponse,
  type MergeRecord,
  type QualityFinding,
  type QualityVerdict,
  type RiskLevel,
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

interface BulkState {
  running: boolean;
  index: number;
  total: number;
  current: string;
  currentPhase: string;
  currentStartedAt: number;
  failures: { key: string; error: string }[];
  succeeded: number;
  batchStartedAt: number;    // epoch ms when Merge All was clicked
  batchDurationMs: number;   // filled in when batch completes
}

interface SingleState {
  phase: string;        // e.g. "claude_merge_start", "quality_done"
  startedAt: number;    // epoch ms
}

const PHASE_LABELS: Record<string, string> = {
  resolve: "Resolving sources (git fetch / artifactory)…",
  resolve_cache_hit: "Using cached paths — skipping resolve ✓",
  resolve_cache_miss:
    "No cached paths — running full resolve (git + artifactory)…",
  resolve_done: "Sources resolved · loading state…",
  reading: "Reading artifact files…",
  claude_merge_start: "Wisetrix is merging…",
  claude_merge_done: "Merge returned · saving files…",
  written: "Files written · running quality gate…",
  quality_start: "Quality gate…",
  quality_done: "Quality gate done · generating diff…",
  diff_start: "Generating _diff.json…",
  diff_done: "Diff done · finalizing…",
  diff_failed: "Diff failed · finalizing merge…",
  diff_skipped: "No _diff.json in customer artifact · skipping diff",
};

function fmtElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
}

export default function MergesPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);

  const [data, setData] = React.useState<MergeListResponse | null>(null);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [singleRunning, setSingleRunning] = React.useState<
    Record<string, SingleState>
  >({});
  const [tick, setTick] = React.useState(0);
  const [bulk, setBulk] = React.useState<BulkState>({
    running: false,
    index: 0,
    total: 0,
    current: "",
    currentPhase: "",
    currentStartedAt: 0,
    failures: [],
    succeeded: 0,
    batchStartedAt: 0,
    batchDurationMs: 0,
  });

  // Tick once per second while any merge is running so elapsed timers
  // rerender. Cheap — only mounted when needed.
  const anyRunning =
    Object.keys(singleRunning).length > 0 || bulk.running;
  React.useEffect(() => {
    if (!anyRunning) return;
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [anyRunning]);

  const reload = React.useCallback(async () => {
    setLoadError(null);
    try {
      const r = await api.listMerges(id);
      setData(r);
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }, [id]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const mergeOne = (key: string) => {
    if (singleRunning[key]) return;
    setSingleRunning((s) => ({
      ...s,
      [key]: { phase: "resolve", startedAt: Date.now() },
    }));

    const clear = () =>
      setSingleRunning((s) => {
        const { [key]: _drop, ...rest } = s;
        return rest;
      });

    const setPhase = (phase: string) =>
      setSingleRunning((s) =>
        s[key] ? { ...s, [key]: { ...s[key], phase } } : s,
      );

    const es = new EventSource(api.mergeOneStreamUrl(id, key));

    es.addEventListener("phase", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      if (d.phase) setPhase(d.phase);
    });
    es.addEventListener("progress", () => setPhase("reading"));
    es.addEventListener("merged", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      toast.success(`Merged ${d.key} (${d.verdict ?? "—"})`);
    });
    es.addEventListener("failed", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      toast.error(`Merge failed: ${d.error}`);
    });
    es.addEventListener("error", (e) => {
      try {
        const d = JSON.parse((e as MessageEvent).data);
        toast.error(d.message ?? "Merge stream errored");
      } catch {
        toast.error("Merge stream closed unexpectedly");
      }
      es.close();
      clear();
    });
    es.addEventListener("done", () => {
      es.close();
      clear();
      void reload();
    });
  };

  const mergeAll = () => {
    if (bulk.running) return;
    const batchStart = Date.now();
    setBulk({
      running: true,
      index: 0,
      total: 0,
      current: "",
      currentPhase: "",
      currentStartedAt: 0,
      failures: [],
      succeeded: 0,
      batchStartedAt: batchStart,
      batchDurationMs: 0,
    });
    const es = new EventSource(api.mergeAllStreamUrl(id));
    es.addEventListener("scan", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setBulk((b) => ({ ...b, total: d.total }));
    });
    es.addEventListener("progress", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setBulk((b) => ({
        ...b,
        index: d.index,
        total: d.total,
        current: d.key,
        currentPhase: "reading",
        currentStartedAt: Date.now(),
      }));
    });
    es.addEventListener("phase", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      if (d.phase) setBulk((b) => ({ ...b, currentPhase: d.phase }));
    });
    es.addEventListener("merged", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setBulk((b) => ({ ...b, succeeded: b.succeeded + 1 }));
      toast.success(`Merged ${d.key}`);
      // Refresh the metrics (Pending / Merged counts) after each artifact
      // so the cards update in real-time, not just at batch completion.
      void reload();
    });
    es.addEventListener("failed", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setBulk((b) => ({
        ...b,
        failures: [...b.failures, { key: d.key, error: d.error }],
      }));
    });
    es.addEventListener("error", (e) => {
      try {
        const d = JSON.parse((e as MessageEvent).data);
        toast.error(d.message ?? "Merge stream errored");
      } catch {
        toast.error("Merge stream closed unexpectedly");
      }
      es.close();
      setBulk((b) => ({ ...b, running: false }));
    });
    es.addEventListener("done", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      es.close();
      setBulk((b) => ({
        ...b,
        running: false,
        batchDurationMs: b.batchStartedAt ? Date.now() - b.batchStartedAt : 0,
      }));
      const failedN = d.failed?.length ?? 0;
      if (failedN > 0) {
        toast.warning(`Merged ${d.succeeded}/${d.total} — ${failedN} failed`);
      } else if (d.total > 0) {
        toast.success(`Merged all ${d.succeeded} pending artifacts`);
      } else {
        toast.message("Nothing pending to merge");
      }
      void reload();
    });
  };

  const pct = bulk.total > 0 ? (bulk.index / bulk.total) * 100 : 0;
  const pending = data?.pending ?? {};
  const done = data?.done ?? {};
  const pendingRows = Object.entries(pending).sort((a, b) =>
    a[0].localeCompare(b[0]),
  );
  const doneRows = Object.entries(done).sort((a, b) =>
    a[0].localeCompare(b[0]),
  );

  return (
    <main className="container max-w-6xl py-12">
      <Link
        href={`/projects/${encodeURIComponent(id)}/scan`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Scan &amp; Compare
      </Link>

      <header className="mt-8 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Wisetrix — {id} · Merge queue
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            AI runs the 3-way merge for each artifact decided
            &quot;Merge&quot;. Quality gates check the merged output
            deterministically.
          </p>
        </div>
        <div className="flex gap-2">
          <Button asChild variant="outline">
            <Link href={`/projects/${encodeURIComponent(id)}/diff`}>
              <GitCompare className="h-4 w-4" />
              Diff viewer
            </Link>
          </Button>
          <Button asChild variant="outline">
            <Link href={`/projects/${encodeURIComponent(id)}/summary`}>
              <FileText className="h-4 w-4" />
              Summary
            </Link>
          </Button>
          <Button
            variant="outline"
            onClick={() => void reload()}
            disabled={bulk.running}
          >
            <RefreshCcw className="h-4 w-4" />
            Reload
          </Button>
          <Button
            asChild
            variant="outline"
            disabled={(data?.done_count ?? 0) === 0}
          >
            <a href={api.mergeDownloadAllUrl(id)} download>
              <Package className="h-4 w-4" />
              Download all (.zip)
            </a>
          </Button>
          <Button
            onClick={mergeAll}
            disabled={bulk.running || pendingRows.length === 0}
          >
            {bulk.running ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Play className="h-4 w-4" />
            )}
            {bulk.running
              ? `Merging ${bulk.index}/${bulk.total}…`
              : `Merge all (${pendingRows.length})`}
          </Button>
        </div>
      </header>

      {loadError && (
        <Card className="mt-8 border-destructive/40 bg-destructive/5">
          <CardHeader>
            <CardTitle className="text-destructive">
              Couldn&apos;t load merge state
            </CardTitle>
            <CardDescription>{loadError}</CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Run Scan &amp; Compare first so there&apos;s a comparison file to
              feed the merge queue.
            </p>
          </CardContent>
        </Card>
      )}

      {data && (
        <section className="mt-8 grid gap-3 sm:grid-cols-3">
          <Metric label="Pending" value={pendingRows.length} />
          <Metric label="Merged" value={doneRows.length} />
          <Metric label="Total candidates" value={pendingRows.length + doneRows.length} />
        </section>
      )}

      {bulk.running || bulk.total > 0 ? (
        <Card className="mt-8">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium">
              {bulk.running ? "Merge All — in progress" : "Last bulk run"}
            </CardTitle>
            <CardDescription>
              {bulk.current
                ? `${bulk.index}/${bulk.total} · ${bulk.current}`
                : `${bulk.succeeded} merged · ${bulk.failures.length} failed`}
              {/* Overall elapsed / total time */}
              {bulk.running && bulk.batchStartedAt > 0 && (
                <span className="ml-2 font-mono text-xs text-muted-foreground">
                  · total {fmtElapsed(Date.now() - bulk.batchStartedAt)}
                </span>
              )}
              {!bulk.running && bulk.batchDurationMs > 0 && (
                <span className="ml-2 font-mono text-xs text-muted-foreground">
                  · completed in {fmtElapsed(bulk.batchDurationMs)}
                </span>
              )}
            </CardDescription>
            {bulk.running && bulk.current && (
              <div className="mt-1 inline-flex items-center gap-1.5 text-xs">
                <Loader2 className="h-3 w-3 animate-spin text-primary" />
                <span>
                  {PHASE_LABELS[bulk.currentPhase] ?? bulk.currentPhase}
                </span>
                {bulk.currentStartedAt > 0 && (
                  <span className="font-mono text-muted-foreground">
                    · {fmtElapsed(Date.now() - bulk.currentStartedAt)}
                  </span>
                )}
              </div>
            )}
          </CardHeader>
          <CardContent>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${pct}%` }}
              />
            </div>
            {bulk.failures.length > 0 && (
              <div className="mt-4 space-y-1 text-xs">
                <div className="font-medium text-destructive">Failures</div>
                {bulk.failures.map((f) => (
                  <div key={f.key} className="rounded bg-destructive/10 p-2">
                    <span className="font-mono">{f.key}</span> — {f.error}
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      ) : null}

      <section className="mt-10">
        <h2 className="text-lg font-semibold tracking-tight">
          Pending ({pendingRows.length})
        </h2>
        {pendingRows.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">
            Nothing pending. Either nothing was decided &quot;Merge&quot; in the
            last scan, or everything has been merged.
          </p>
        ) : (
          <Card className="mt-3">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 font-medium">Risk</th>
                    <th className="px-4 py-2 font-medium">Artifact</th>
                    <th className="px-4 py-2 font-medium">Bucket</th>
                    <th className="px-4 py-2 font-medium">Analysis</th>
                    <th className="px-4 py-2 font-medium text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/40">
                  {pendingRows.map(([key, entry]) => {
                    const s = singleRunning[key];
                    // tick is referenced so the elapsed timer rerenders
                    void tick;
                    return (
                      <PendingRow
                        key={key}
                        itemKey={key}
                        entry={entry as ComparisonResult}
                        running={!!s}
                        phase={s?.phase}
                        elapsedMs={s ? Date.now() - s.startedAt : 0}
                        disabled={bulk.running}
                        onMerge={() => mergeOne(key)}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold tracking-tight">
          Completed ({doneRows.length})
        </h2>
        {doneRows.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">
            Nothing merged yet.
          </p>
        ) : (
          <Card className="mt-3">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 font-medium">Verdict</th>
                    <th className="px-4 py-2 font-medium">Artifact</th>
                    <th className="px-4 py-2 font-medium">Files</th>
                    <th className="px-4 py-2 font-medium">Duration</th>
                    <th className="px-4 py-2 font-medium">Merged at</th>
                    <th className="px-4 py-2 font-medium text-right">Download</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/40">
                  {doneRows.map(([key, rec]) => (
                    <tr key={key} className="hover:bg-muted/30">
                      <td className="px-4 py-2">
                        <VerdictBadge
                          verdict={
                            (rec.quality_result?.verdict ??
                              "—") as QualityVerdict | "—"
                          }
                          findings={rec.quality_result?.findings}
                        />
                      </td>
                      <td className="px-4 py-2 font-mono text-xs">
                        {key}
                        {rec.db_warning && (
                          <div className="mt-1 inline-flex items-center gap-1 rounded bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-950 dark:text-amber-300">
                            ⚠ DB action required before upgrade
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-2 text-muted-foreground">
                        {rec.files.length}
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                        {rec.merge_duration_seconds != null
                          ? fmtElapsed(rec.merge_duration_seconds * 1000)
                          : "—"}
                      </td>
                      <td className="px-4 py-2 text-xs text-muted-foreground">
                        {rec.merged_at}
                      </td>
                      <td className="px-4 py-2 text-right">
                        <Button asChild variant="outline" size="sm">
                          <a href={api.mergeDownloadOneUrl(id, key)} download>
                            <Download className="h-3.5 w-3.5" />
                            ZIP
                          </a>
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </section>
    </main>
  );
}

// ---------- helpers ----------

function PendingRow({
  itemKey,
  entry,
  running,
  phase,
  elapsedMs,
  disabled,
  onMerge,
}: {
  itemKey: string;
  entry: ComparisonResult;
  running: boolean;
  phase?: string;
  elapsedMs: number;
  disabled: boolean;
  onMerge: () => void;
}) {
  const phaseLabel = phase ? (PHASE_LABELS[phase] ?? phase) : null;
  return (
    <tr className="hover:bg-muted/30">
      <td className="px-4 py-2">
        {entry.risk_level ? (
          <RiskBadge level={entry.risk_level} />
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-4 py-2 font-mono text-xs">{entry.rel_path}</td>
      <td className="px-4 py-2 text-muted-foreground">{entry.bucket}</td>
      <td className="px-4 py-2 text-xs text-muted-foreground">
        {running && phaseLabel ? (
          <span className="inline-flex items-center gap-1.5 text-foreground">
            <Loader2 className="h-3 w-3 animate-spin text-primary" />
            <span>{phaseLabel}</span>
            <span className="font-mono text-muted-foreground">
              · {fmtElapsed(elapsedMs)}
            </span>
          </span>
        ) : (
          (entry.analysis ?? "").slice(0, 120)
        )}
        {entry.db_warning && (
          <div className="mt-1 inline-flex items-center gap-1 rounded bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-950 dark:text-amber-300">
            ⚠ DB action required before upgrade
          </div>
        )}
      </td>
      <td className="px-4 py-2 text-right">
        <Button
          size="sm"
          onClick={onMerge}
          disabled={running || disabled}
        >
          {running ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <GitMerge className="h-3.5 w-3.5" />
          )}
          {running ? "Merging…" : "Merge"}
        </Button>
      </td>
    </tr>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border border-border/60 bg-card/40 p-4">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function RiskBadge({ level }: { level: RiskLevel }) {
  const variant =
    level === "HIGH" ? "danger" : level === "MEDIUM" ? "warning" : "success";
  return <Badge variant={variant as never}>{level}</Badge>;
}

const VERDICT_SUMMARY: Record<string, string> = {
  PASS: "Quality gate passed — no issues found in the merged output.",
  WARN: "Quality gate warning — merge succeeded but potential issues were found.",
  FAIL: "Quality gate failed — merge succeeded but errors were found in the output.\nReview findings below before using the merged files.",
  "—": "No quality gate result recorded.",
};

const SEVERITY_PREFIX: Record<string, string> = {
  ERROR: "✖",
  WARNING: "⚠",
  INFO: "ℹ",
};

function VerdictBadge({
  verdict,
  findings,
}: {
  verdict: QualityVerdict | "—";
  findings?: QualityFinding[];
}) {
  const variant =
    verdict === "PASS"
      ? "success"
      : verdict === "WARN"
        ? "warning"
        : verdict === "FAIL"
          ? "danger"
          : "muted";

  const lines: string[] = [VERDICT_SUMMARY[verdict] ?? verdict];
  if (findings && findings.length > 0) {
    lines.push("");
    lines.push("Findings:");
    for (const f of findings) {
      const prefix = SEVERITY_PREFIX[f.severity] ?? "•";
      const loc = f.line ? `:${f.line}` : "";
      lines.push(`${prefix} [${f.file}${loc}] ${f.message}`);
    }
  }
  const tooltip = lines.join("\n");

  return (
    <span title={tooltip} className="cursor-help">
      <Badge variant={variant as never}>{verdict}</Badge>
    </span>
  );
}
