"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import * as React from "react";
import {
  ArrowLeft,
  Loader2,
  RefreshCcw,
  Search,
  ShieldCheck,
} from "lucide-react";
import { toast } from "sonner";

import {
  api,
  ApiError,
  type DiffResponse,
  type MergeListResponse,
  type ReviewResponse,
  type QualityVerdict,
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

// Monaco is heavy + browser-only — import dynamically with SSR off.
const DiffEditor = dynamic(
  () => import("@monaco-editor/react").then((m) => m.DiffEditor),
  { ssr: false, loading: () => <EditorPlaceholder /> },
);

type Side = "source" | "system";

const LANG_BY_EXT: Record<string, string> = {
  json: "json",
  xml: "xml",
  java: "java",
  js: "javascript",
  jsp: "html",
  yaml: "yaml",
  yml: "yaml",
  md: "markdown",
  html: "html",
  css: "css",
};

function languageFor(filename: string): string {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  return LANG_BY_EXT[ext] ?? "plaintext";
}

export default function DiffPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);

  const [mergeList, setMergeList] = React.useState<MergeListResponse | null>(
    null,
  );
  const [listError, setListError] = React.useState<string | null>(null);

  const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
  const [selectedFile, setSelectedFile] = React.useState<string | null>(null);
  const [side, setSide] = React.useState<Side>("system");

  const [diff, setDiff] = React.useState<DiffResponse | null>(null);
  const [diffLoading, setDiffLoading] = React.useState(false);

  const [review, setReview] = React.useState<ReviewResponse | null>(null);
  const [reviewing, setReviewing] = React.useState(false);
  const [filter, setFilter] = React.useState("");

  React.useEffect(() => {
    api
      .listMerges(id)
      .then((r) => {
        setMergeList(r);
        const firstDone = Object.keys(r.done)[0];
        if (firstDone) setSelectedKey(firstDone);
      })
      .catch((e) =>
        setListError(e instanceof ApiError ? e.message : (e as Error).message),
      );
  }, [id]);

  React.useEffect(() => {
    if (!selectedKey) return;
    setDiff(null);
    setReview(null);
    setSelectedFile(null);
    setDiffLoading(true);
    api
      .getDiff(id, selectedKey)
      .then((d) => {
        setDiff(d);
        const first = d.files[0];
        if (first) setSelectedFile(first);
      })
      .catch((e) => {
        const msg = e instanceof ApiError ? e.message : (e as Error).message;
        toast.error(msg);
      })
      .finally(() => setDiffLoading(false));
  }, [id, selectedKey]);

  const mergedKeys = Object.keys(mergeList?.done ?? {});
  const filteredKeys = filter
    ? mergedKeys.filter((k) => k.toLowerCase().includes(filter.toLowerCase()))
    : mergedKeys;

  const original =
    diff && selectedFile
      ? (side === "system" ? diff.system : diff.aldi)[selectedFile] ?? ""
      : "";
  const modified =
    diff && selectedFile ? diff.merged[selectedFile] ?? "" : "";
  const language = selectedFile ? languageFor(selectedFile) : "plaintext";

  const runReview = async () => {
    if (!selectedKey) return;
    setReviewing(true);
    try {
      const r = await api.runReview(id, selectedKey);
      setReview(r);
      toast.success(`Review: ${r.verdict}`);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setReviewing(false);
    }
  };

  return (
    <main className="container max-w-7xl py-8">
      <Link
        href={`/projects/${encodeURIComponent(id)}/merges`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to merges
      </Link>

      <header className="mt-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Wisetrix — {id} · Diff &amp; Review
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Side-by-side diffs of each merged file plus the AI-generated
            review.
          </p>
        </div>
      </header>

      {listError && (
        <Card className="mt-6 border-destructive/40 bg-destructive/5">
          <CardHeader>
            <CardTitle className="text-destructive">
              Couldn&apos;t load merge list
            </CardTitle>
            <CardDescription>{listError}</CardDescription>
          </CardHeader>
        </Card>
      )}

      {mergeList && mergedKeys.length === 0 && (
        <Card className="mt-6 border-dashed">
          <CardHeader>
            <CardTitle>No merged artifacts yet</CardTitle>
            <CardDescription>
              Run the merge queue first, then come back to inspect diffs.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {mergedKeys.length > 0 && (
        <div className="mt-6 grid gap-4 lg:grid-cols-[280px_1fr]">
          {/* Sidebar */}
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Search className="h-4 w-4 text-muted-foreground" />
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter artifacts…"
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <div className="rounded-lg border border-border/60 bg-card/40 p-1 text-sm">
              <div className="max-h-[60vh] overflow-y-auto">
                {filteredKeys.map((k) => (
                  <button
                    key={k}
                    onClick={() => setSelectedKey(k)}
                    className={
                      k === selectedKey
                        ? "block w-full truncate rounded-md bg-primary/15 px-2 py-1.5 text-left font-mono text-xs text-primary"
                        : "block w-full truncate rounded-md px-2 py-1.5 text-left font-mono text-xs text-muted-foreground hover:bg-muted/40"
                    }
                    title={k}
                  >
                    {k}
                  </button>
                ))}
                {filteredKeys.length === 0 && (
                  <p className="px-2 py-3 text-xs text-muted-foreground">
                    No matches.
                  </p>
                )}
              </div>
            </div>
          </div>

          {/* Main */}
          <div className="space-y-4">
            {diffLoading && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading file contents…
              </div>
            )}

            {diff && (
              <>
                <Card>
                  <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3 space-y-0 pb-3">
                    <div>
                      <CardTitle className="text-sm font-medium">
                        {diff.rel_path}
                      </CardTitle>
                      <CardDescription className="text-xs">
                        bucket: {diff.bucket} · {diff.files.length} file
                        {diff.files.length === 1 ? "" : "s"}
                      </CardDescription>
                    </div>
                    <div className="flex items-center gap-2">
                      <select
                        value={selectedFile ?? ""}
                        onChange={(e) => setSelectedFile(e.target.value)}
                        className="h-9 rounded-md border border-input bg-background px-2 text-xs"
                      >
                        {diff.files.map((f) => (
                          <option key={f} value={f}>
                            {f}
                          </option>
                        ))}
                      </select>
                      <div className="inline-flex rounded-md border border-border bg-card/40 p-0.5 text-xs">
                        {(["source", "system"] as Side[]).map((s) => (
                          <button
                            key={s}
                            onClick={() => setSide(s)}
                            className={
                              s === side
                                ? "rounded px-2 py-1 font-medium text-primary"
                                : "rounded px-2 py-1 text-muted-foreground hover:text-foreground"
                            }
                          >
                            vs {s === "source" ? diff.bucket : diff.target_label}
                          </button>
                        ))}
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="p-0">
                    <div className="flex border-t border-border/40 bg-muted/30 px-4 py-1.5 text-xs font-medium text-muted-foreground">
                      <span className="flex-1">
                        {side === "source" ? diff.bucket : diff.target_label}
                      </span>
                      <span className="flex-1 text-right text-primary/80">
                        Merged output
                      </span>
                    </div>
                    <div className="h-[60vh] w-full overflow-hidden rounded-b-lg border-t border-border/40">
                      <DiffEditor
                        height="100%"
                        theme="vs-dark"
                        language={language}
                        original={original}
                        modified={modified}
                        options={{
                          readOnly: true,
                          renderSideBySide: true,
                          minimap: { enabled: false },
                          fontSize: 12,
                          wordWrap: "on",
                          scrollBeyondLastLine: false,
                        }}
                      />
                    </div>
                  </CardContent>
                </Card>

                {/* Review */}
                <Card>
                  <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
                    <div>
                      <CardTitle className="text-sm font-medium">
                        AI review
                      </CardTitle>
                      <CardDescription className="text-xs">
                        Structured PASS / WARN / FAIL verdict + findings.
                      </CardDescription>
                    </div>
                    <div className="flex items-center gap-2">
                      {review && (
                        <VerdictBadge
                          verdict={review.verdict as QualityVerdict}
                        />
                      )}
                      <Button
                        size="sm"
                        onClick={runReview}
                        disabled={reviewing || !diff.has_merge}
                      >
                        {reviewing ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <ShieldCheck className="h-3.5 w-3.5" />
                        )}
                        {reviewing ? "Reviewing…" : "Run review"}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          if (selectedKey)
                            void api.getDiff(id, selectedKey).then(setDiff);
                        }}
                      >
                        <RefreshCcw className="h-3.5 w-3.5" />
                        Reload
                      </Button>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3 pt-0">
                    {!review && (
                      <p className="text-xs text-muted-foreground">
                        Not reviewed yet.
                      </p>
                    )}
                    {review && (
                      <>
                        {review.summary && (
                          <p className="text-sm leading-relaxed text-foreground/80">
                            {review.summary}
                          </p>
                        )}
                        {review.findings && review.findings.length > 0 ? (
                          <div className="space-y-2">
                            {review.findings.map((f, i) => (
                              <div
                                key={i}
                                className="rounded-md border border-border/60 bg-card/40 p-3 text-xs"
                              >
                                <div className="flex items-center gap-2">
                                  <SeverityBadge severity={f.severity} />
                                  <span className="font-medium">
                                    {f.category}
                                  </span>
                                  <span className="text-muted-foreground">
                                    {f.file}
                                  </span>
                                </div>
                                <p className="mt-1 text-foreground/80">
                                  {f.message}
                                </p>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="text-xs text-muted-foreground">
                            No findings.
                          </p>
                        )}
                      </>
                    )}
                  </CardContent>
                </Card>
              </>
            )}
          </div>
        </div>
      )}
    </main>
  );
}

function EditorPlaceholder() {
  return (
    <div className="flex h-[60vh] items-center justify-center bg-card/40 text-sm text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
      Loading editor…
    </div>
  );
}

function VerdictBadge({ verdict }: { verdict: QualityVerdict | string }) {
  const variant =
    verdict === "PASS"
      ? "success"
      : verdict === "WARN"
        ? "warning"
        : verdict === "FAIL"
          ? "danger"
          : "muted";
  return <Badge variant={variant as never}>{verdict}</Badge>;
}

function SeverityBadge({ severity }: { severity: string }) {
  const variant =
    severity === "ERROR"
      ? "danger"
      : severity === "WARNING"
        ? "warning"
        : "muted";
  return <Badge variant={variant as never}>{severity}</Badge>;
}
