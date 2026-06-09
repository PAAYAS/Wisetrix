"use client";

import Link from "next/link";
import * as React from "react";
import {
  ArrowLeft,
  GitMerge,
  Loader2,
  Play,
  RefreshCcw,
  Search,
  ShieldAlert,
} from "lucide-react";
import { toast } from "sonner";

import {
  api,
  ApiError,
  type ArtifactListResponse,
  type ComparisonMap,
  type ComparisonResult,
  type Decision,
  type ProjectConfig,
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
import { Input } from "@/components/ui/input";

const DECISIONS: Decision[] = ["Merge", "Retain", "Remove", "ERROR"];
const RISKS: RiskLevel[] = ["HIGH", "MEDIUM", "LOW"];

/** Labels shown in the compare-run progress card */
const PHASE_LABELS: Record<string, string> = {
  resolve: "Resolving sources (git fetch / artifactory)…",
  scan: "Scanning source tree for artifacts…",
  compare: "Comparing against target version…",
  rollup: "Applying business rules…",
  risk: "Scoring risk for each artifact…",
  done: "Done",
};

/** Labels shown in the page-load artifact-stream card */
const LOAD_PHASE_LABELS: Record<string, string> = {
  cached:                "Using cached paths — ready instantly",
  git_clone:             "Cloning git repository (first time, shallow)…",
  git_fetch:             "Fetching latest commits from git…",
  artifactory_cached:             "Target Artifactory already extracted locally",
  artifactory_download:           "Downloading target Artifactory JAR…",
  artifactory_baseline_cached:    "Baseline Artifactory already extracted locally",
  artifactory_baseline_download:  "Downloading baseline Artifactory JAR…",
  scan:                  "Scanning artifact tree…",
};

interface ProgressState {
  running: boolean;
  phase: string;
  index: number;
  total: number;
  current: string;
  startedAt: number;
  doneSummary?: {
    decision_counts: Record<string, number>;
    risk_counts: Record<string, number>;
  };
}

function fmtElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
}

export default function ScanPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);

  const [meta, setMeta] = React.useState<ArtifactListResponse | null>(null);
  const [metaError, setMetaError] = React.useState<string | null>(null);
  /** True while the artifact SSE stream is in flight (page-load resolve phase) */
  const [metaLoading, setMetaLoading] = React.useState(false);
  /** Current phase emitted by the artifact SSE stream */
  const [metaPhase, setMetaPhase] = React.useState("");
  /** Human-readable note emitted alongside the phase */
  const [metaNote, setMetaNote] = React.useState("");
  /** Download progress from Artifactory — shown during JAR download */
  const [downloadProgress, setDownloadProgress] = React.useState<{
    msg: string;
    pct: number | null;
    downloaded_mb: number | null;
    total_mb: number | null;
  } | null>(null);

  const [comparison, setComparison] = React.useState<ComparisonMap>({});
  const [projectCfg, setProjectCfg] = React.useState<ProjectConfig | null>(null);
  const [progress, setProgress] = React.useState<ProgressState>({
    running: false,
    phase: "",
    index: 0,
    total: 0,
    current: "",
    startedAt: 0,
  });
  const [tick, setTick] = React.useState(0);

  // Tick once per second while compare is running so elapsed time rerenders.
  React.useEffect(() => {
    if (!progress.running) return;
    const timerId = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timerId);
  }, [progress.running]);

  const [decisionFilter, setDecisionFilter] = React.useState<Set<Decision>>(
    new Set(),
  );
  const [riskFilter, setRiskFilter] = React.useState<Set<RiskLevel>>(new Set());
  const [search, setSearch] = React.useState("");

  // Ref so we can close the EventSource on unmount or when we re-trigger load
  const metaEsRef = React.useRef<EventSource | null>(null);

  /**
   * Load the artifact list via the SSE stream.
   *
   * This replaces the old `listArtifacts` fetch: the stream emits phase events
   * (git_clone, artifactory_download, …) so the user can see what's happening
   * on a cold cache instead of staring at a blank spinner for 15 minutes.
   */
  const loadMeta = React.useCallback(() => {
    // Close any previous stream before starting a new one
    metaEsRef.current?.close();

    setMetaError(null);
    setMetaLoading(true);
    setMetaPhase("");
    setMetaNote("");
    setDownloadProgress(null);

    const es = new EventSource(api.artifactsStreamUrl(id));
    metaEsRef.current = es;

    es.addEventListener("phase", (ev) => {
      const d = JSON.parse((ev as MessageEvent).data) as {
        phase: string;
        note?: string;
      };
      setMetaPhase(d.phase ?? "");
      setMetaNote(d.note ?? "");
    });

    es.addEventListener("download_progress", (ev) => {
      const d = JSON.parse((ev as MessageEvent).data) as {
        phase: string;
        msg?: string;
        pct?: number;
        downloaded_mb?: number;
        total_mb?: number;
      };
      // Only show the progress bar for active download/extract phases
      if (
        d.phase === "download_start" ||
        d.phase === "download_progress" ||
        d.phase === "download_done" ||
        d.phase === "extract_start" ||
        d.phase === "extract_done"
      ) {
        setDownloadProgress({
          msg: d.msg ?? d.phase,
          pct: d.pct ?? null,
          downloaded_mb: d.downloaded_mb ?? null,
          total_mb: d.total_mb ?? null,
        });
      }
    });

    es.addEventListener("done", (ev) => {
      const d = JSON.parse((ev as MessageEvent).data) as ArtifactListResponse;
      setMeta(d);
      setMetaLoading(false);
      setMetaPhase("");
      setDownloadProgress(null);
      es.close();
      metaEsRef.current = null;
    });

    es.addEventListener("error", (ev) => {
      // SSE fires a generic Event on network error, or our custom error event
      // has a `data` field. Try to parse the data first.
      try {
        const d = JSON.parse((ev as MessageEvent).data) as { message?: string };
        setMetaError(d.message ?? "Failed to load artifacts");
      } catch {
        setMetaError("Failed to load artifacts — check the backend is running");
      }
      setMetaLoading(false);
      es.close();
      metaEsRef.current = null;
    });
  }, [id]);

  /** Reload comparison results + project config (fast — no external I/O). */
  const loadComparison = React.useCallback(() => {
    void Promise.all([api.getComparison(id), api.getProject(id)]).then(
      ([c, p]) => {
        setComparison(c);
        setProjectCfg(p.config);
      },
    ).catch(() => {
      // Non-fatal — new projects have no comparison yet
    });
  }, [id]);

  // On mount: start the artifact stream + load comparison in parallel.
  // On unmount: close the stream to avoid memory leaks.
  React.useEffect(() => {
    loadMeta();
    loadComparison();
    return () => {
      metaEsRef.current?.close();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const runCompare = () => {
    if (progress.running) return;
    setProgress({
      running: true,
      phase: "resolve",
      index: 0,
      total: 0,
      current: "",
      startedAt: Date.now(),
    });

    const es = new EventSource(api.compareStreamUrl(id));

    es.addEventListener("phase", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setProgress((p) => ({ ...p, phase: data.phase }));
    });
    es.addEventListener("scan", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setProgress((p) => ({ ...p, total: data.total }));
    });
    es.addEventListener("progress", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setProgress((p) => ({
        ...p,
        index: data.index,
        total: data.total,
        current: data.key,
      }));
    });
    es.addEventListener("error", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data);
        toast.error(data.message ?? "Compare failed");
      } catch {
        toast.error("Compare stream closed unexpectedly");
      }
      es.close();
      setProgress((p) => ({ ...p, running: false }));
    });
    es.addEventListener("done", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setProgress((p) => ({
        running: false,
        phase: "done",
        index: data.total,
        total: data.total,
        current: "",
        startedAt: p.startedAt,
        doneSummary: {
          decision_counts: data.decision_counts ?? {},
          risk_counts: data.risk_counts ?? {},
        },
      }));
      toast.success(`Compared ${data.total} artifacts`);
      es.close();
      // Reload artifact meta (cache is now warm — stream completes in seconds)
      loadMeta();
      loadComparison();
    });
  };

  const toggle = <T,>(set: Set<T>, value: T): Set<T> => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  };

  const rows = React.useMemo(() => {
    const list = Object.entries(comparison).map(([key, r]) => ({ key, ...r }));
    return list
      .filter((r) =>
        decisionFilter.size === 0 ? true : decisionFilter.has(r.decision),
      )
      .filter((r) =>
        riskFilter.size === 0
          ? true
          : r.risk_level
            ? riskFilter.has(r.risk_level)
            : false,
      )
      .filter((r) =>
        search.trim()
          ? r.source_rel.toLowerCase().includes(search.trim().toLowerCase())
          : true,
      )
      .sort((a, b) => a.source_rel.localeCompare(b.source_rel));
  }, [comparison, decisionFilter, riskFilter, search]);

  const counts = React.useMemo(() => countByDecision(comparison), [comparison]);
  const riskCounts = React.useMemo(() => countByRisk(comparison), [comparison]);

  const pct = progress.total > 0 ? (progress.index / progress.total) * 100 : 0;
  // Recompute elapsed when tick changes — keeps the timer fresh.
  void tick;
  const elapsedMs =
    progress.startedAt > 0 ? Date.now() - progress.startedAt : 0;

  return (
    <main className="container max-w-6xl py-12">
      <Link
        href={`/projects/${encodeURIComponent(id)}/setup`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to setup
      </Link>

      <header className="mt-8 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Wisetrix — {id} · Scan &amp; Compare
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Deterministic compare — runs locally with no AI calls. Each
            artifact gets a decision (Merge / Retain / Remove) and a risk level.
          </p>
        </div>
        <div className="flex gap-2">
          <Button asChild variant="outline">
            <Link href={`/projects/${encodeURIComponent(id)}/merges`}>
              <GitMerge className="h-4 w-4" />
              Merge queue
            </Link>
          </Button>
          <Button
            variant="outline"
            onClick={() => { loadMeta(); loadComparison(); }}
            disabled={progress.running || metaLoading}
          >
            <RefreshCcw className="h-4 w-4" />
            Reload
          </Button>
          <Button onClick={runCompare} disabled={progress.running}>
            {progress.running ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Play className="h-4 w-4" />
            )}
            {progress.running ? "Running…" : "Run compare"}
          </Button>
        </div>
      </header>

      {/* Artifact-load progress — shown while the SSE resolve stream is running */}
      {metaLoading && (
        <Card className="mt-8 border-primary/30 bg-primary/5">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm font-medium">
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
              {LOAD_PHASE_LABELS[metaPhase] ?? metaPhase ?? "Initializing…"}
            </CardTitle>
            {metaNote && (
              <CardDescription>{metaNote}</CardDescription>
            )}
          </CardHeader>

          {/* Artifactory download progress bar */}
          {downloadProgress && (
            <CardContent className="pt-0 pb-4">
              <p className="text-xs text-muted-foreground mb-2">
                {downloadProgress.msg}
              </p>
              {downloadProgress.pct !== null && (
                <div className="w-full bg-muted rounded-full h-2">
                  <div
                    className="bg-primary h-2 rounded-full transition-all duration-300"
                    style={{ width: `${downloadProgress.pct}%` }}
                  />
                </div>
              )}
              {downloadProgress.downloaded_mb !== null && (
                <p className="text-xs text-muted-foreground mt-1">
                  {downloadProgress.downloaded_mb} MB
                  {downloadProgress.total_mb
                    ? ` / ${downloadProgress.total_mb} MB`
                    : " downloaded"}
                  {downloadProgress.pct !== null
                    ? ` (${downloadProgress.pct}%)`
                    : ""}
                </p>
              )}
            </CardContent>
          )}
        </Card>
      )}

      {metaError && (
        <Card className="mt-8 border-destructive/40 bg-destructive/5">
          <CardHeader>
            <CardTitle className="text-destructive">
              Couldn&apos;t resolve project paths
            </CardTitle>
            <CardDescription>{metaError}</CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Open the Setup page and confirm the source/target are reachable.
              For Git sources, run a Test Git Connection first.
            </p>
          </CardContent>
        </Card>
      )}

      {meta && (() => {
        // Friendly labels: customer name from buckets, versions from project config.
        const sourceLabel =
          (meta.buckets || []).find((b) => b !== "SYSTEM") || id;
        const targetLabel = projectCfg?.target_version || "—";
        const baselineLabel =
          projectCfg?.baseline_type === "none"
            ? "(none)"
            : projectCfg?.baseline_version || "(none)";
        return (
          <section className="mt-8 grid gap-3 sm:grid-cols-3">
            <MetaCard label="Source" value={sourceLabel} />
            <MetaCard label="Target" value={targetLabel} />
            <MetaCard label="Baseline" value={baselineLabel} />
            <MetaCard
              label="Buckets"
              value={meta.buckets.join(", ") || "(none)"}
            />
            <MetaCard label="Artifacts in source" value={String(meta.count)} />
            {meta.git_metadata && (
              <MetaCard
                label="Git"
                value={`${String(meta.git_metadata.branch ?? "")} @ ${String(meta.git_metadata.commit ?? "").slice(0, 8)}`}
              />
            )}
          </section>
        );
      })()}

      {progress.running || progress.phase === "done" ? (
        <Card className="mt-8">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium">
              {progress.running ? (
                <span className="inline-flex items-center gap-2">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                  {PHASE_LABELS[progress.phase] ?? progress.phase}
                </span>
              ) : (
                "Last run"
              )}
            </CardTitle>
            <CardDescription>
              {progress.total > 0 ? (
                <span className="flex flex-wrap items-baseline gap-2">
                  <span className="font-mono text-base font-semibold text-foreground tabular-nums">
                    {progress.index}/{progress.total}
                  </span>
                  {progress.total > 0 && (
                    <span className="font-mono text-xs text-muted-foreground">
                      ({Math.round(pct)}%)
                    </span>
                  )}
                  {progress.current && (
                    <span className="truncate font-mono text-xs text-muted-foreground">
                      · {progress.current}
                    </span>
                  )}
                  {progress.running && progress.startedAt > 0 && (
                    <span className="ml-auto font-mono text-xs text-muted-foreground">
                      {fmtElapsed(elapsedMs)}
                    </span>
                  )}
                </span>
              ) : progress.running ? (
                <span className="inline-flex items-center gap-2">
                  <span>
                    {progress.phase === "resolve"
                      ? "Talking to git / artifactory…"
                      : progress.phase === "scan"
                        ? "Walking the source tree to count artifacts…"
                        : "Starting…"}
                  </span>
                  {progress.startedAt > 0 && (
                    <span className="font-mono text-muted-foreground">
                      · {fmtElapsed(elapsedMs)}
                    </span>
                  )}
                </span>
              ) : (
                "Starting…"
              )}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${pct}%` }}
              />
            </div>
            {progress.doneSummary && (
              <div className="mt-4 flex flex-wrap gap-2">
                {Object.entries(progress.doneSummary.decision_counts).map(
                  ([k, n]) => (
                    <DecisionBadge key={k} decision={k as Decision} count={n} />
                  ),
                )}
                {Object.entries(progress.doneSummary.risk_counts).map(
                  ([k, n]) => (
                    <RiskBadge key={k} level={k as RiskLevel} count={n} />
                  ),
                )}
              </div>
            )}
          </CardContent>
        </Card>
      ) : null}

      {/* Filters + results */}
      {Object.keys(comparison).length > 0 && (
        <section className="mt-8 space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-1.5">
              <span className="text-xs uppercase tracking-wider text-muted-foreground">
                Decision
              </span>
              {DECISIONS.map((d) => (
                <FilterChip
                  key={d}
                  active={decisionFilter.has(d)}
                  onClick={() =>
                    setDecisionFilter((s) => toggle(s, d))
                  }
                >
                  {d} <span className="opacity-60">· {counts[d] ?? 0}</span>
                </FilterChip>
              ))}
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-xs uppercase tracking-wider text-muted-foreground">
                Risk
              </span>
              {RISKS.map((r) => (
                <FilterChip
                  key={r}
                  active={riskFilter.has(r)}
                  onClick={() => setRiskFilter((s) => toggle(s, r))}
                >
                  {r} <span className="opacity-60">· {riskCounts[r] ?? 0}</span>
                </FilterChip>
              ))}
            </div>
            <div className="ml-auto flex w-full max-w-xs items-center gap-2 sm:w-auto">
              <Search className="h-4 w-4 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter by artifact path"
                className="h-9"
              />
            </div>
          </div>

          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 font-medium">Bucket</th>
                    <th className="px-4 py-2 font-medium">Artifact</th>
                    <th className="px-4 py-2 font-medium">Decision</th>
                    <th className="px-4 py-2 font-medium">Risk</th>
                    <th className="px-4 py-2 font-medium">Files</th>
                    <th className="px-4 py-2 font-medium">Notes</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/40">
                  {rows.map((r) => (
                    <tr key={r.key} className="hover:bg-muted/30">
                      <td className="px-4 py-2 text-muted-foreground">
                        {r.bucket}
                      </td>
                      <td className="px-4 py-2 font-mono text-xs">
                        {r.rel_path}
                      </td>
                      <td className="px-4 py-2">
                        <DecisionBadge decision={r.decision} />
                      </td>
                      <td className="px-4 py-2">
                        {r.risk_level ? (
                          <RiskBadge level={r.risk_level} />
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-muted-foreground">
                        {r.file_count ?? "—"}
                      </td>
                      <td className="px-4 py-2 text-muted-foreground">
                        {r.decision_note ?? r.error ?? r.analysis ?? ""}
                        {r.db_warning && (
                          <div className="mt-1 inline-flex items-center gap-1 rounded bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-950 dark:text-amber-300">
                            ⚠ DB action required before upgrade
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                  {rows.length === 0 && (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-4 py-6 text-center text-muted-foreground"
                      >
                        No matches.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        </section>
      )}

      {meta &&
        Object.keys(comparison).length === 0 &&
        !progress.running && (
          <Card className="mt-8 border-dashed">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldAlert className="h-4 w-4 text-primary" />
                No compare results yet
              </CardTitle>
              <CardDescription>
                Click <strong>Run compare</strong> to scan {meta.count}{" "}
                artifacts against the target version.
              </CardDescription>
            </CardHeader>
          </Card>
        )}
    </main>
  );
}

// ---------- Helpers ----------

function MetaCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/60 bg-card/40 p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 truncate font-mono text-xs">{value}</div>
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        active
          ? "rounded-full border border-primary/60 bg-primary/15 px-2.5 py-1 text-xs font-medium text-primary transition-colors"
          : "rounded-full border border-border bg-card/40 px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted/60"
      }
    >
      {children}
    </button>
  );
}

function DecisionBadge({
  decision,
  count,
}: {
  decision: Decision;
  count?: number;
}) {
  const variant =
    decision === "Merge"
      ? "default"
      : decision === "Retain"
        ? "secondary"
        : decision === "Remove"
          ? "muted"
          : "danger";
  return (
    <Badge variant={variant as never}>
      {decision}
      {count != null && <span className="ml-1 opacity-70">· {count}</span>}
    </Badge>
  );
}

function RiskBadge({ level, count }: { level: RiskLevel; count?: number }) {
  const variant =
    level === "HIGH" ? "danger" : level === "MEDIUM" ? "warning" : "success";
  return (
    <Badge variant={variant as never}>
      {level}
      {count != null && <span className="ml-1 opacity-70">· {count}</span>}
    </Badge>
  );
}

function countByDecision(comparison: ComparisonMap) {
  const out: Partial<Record<Decision, number>> = {};
  for (const r of Object.values(comparison) as ComparisonResult[]) {
    out[r.decision] = (out[r.decision] ?? 0) + 1;
  }
  return out;
}

function countByRisk(comparison: ComparisonMap) {
  const out: Partial<Record<RiskLevel, number>> = {};
  for (const r of Object.values(comparison) as ComparisonResult[]) {
    if (r.risk_level) out[r.risk_level] = (out[r.risk_level] ?? 0) + 1;
  }
  return out;
}
