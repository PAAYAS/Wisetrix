"use client";

import Link from "next/link";
import * as React from "react";
import {
  ArrowLeft,
  Download,
  Eye,
  EyeOff,
  FileText,
  Loader2,
  RefreshCcw,
  Sparkles,
  Ticket,
} from "lucide-react";

import { Markdown } from "@/components/markdown";
import { toast } from "sonner";

import {
  api,
  ApiError,
  type NarrativeRecord,
  type SummaryMetrics,
  type SummaryResponse,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function SummaryPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);

  const [data, setData] = React.useState<SummaryResponse | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [generating, setGenerating] = React.useState(false);
  const [buildingReport, setBuildingReport] = React.useState(false);
  const [reportPath, setReportPath] = React.useState<string | null>(null);
  const [downloadingPdf, setDownloadingPdf] = React.useState(false);
  const [previewMd, setPreviewMd] = React.useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = React.useState(false);
  const [loadingPreview, setLoadingPreview] = React.useState(false);

  const downloadPdf = async () => {
    setDownloadingPdf(true);
    try {
      const res = await fetch(api.reportPdfUrl(id));
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(text || `${res.status} ${res.statusText}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `UPGRADE_REPORT_${id}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.success("PDF downloaded");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`PDF download failed: ${msg}`);
    } finally {
      setDownloadingPdf(false);
    }
  };

  const togglePreview = async () => {
    if (previewOpen) {
      setPreviewOpen(false);
      return;
    }
    if (previewMd) {
      setPreviewOpen(true);
      return;
    }
    setLoadingPreview(true);
    try {
      const res = await fetch(api.reportMarkdownUrl(id));
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(text || `${res.status} ${res.statusText}`);
      }
      setPreviewMd(await res.text());
      setPreviewOpen(true);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Couldn't load markdown: ${msg}`);
    } finally {
      setLoadingPreview(false);
    }
  };

  const reload = React.useCallback(async () => {
    setError(null);
    try {
      const r = await api.getSummary(id);
      setData(r);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }, [id]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const regenerate = async () => {
    setGenerating(true);
    try {
      const n = await api.regenerateNarrative(id);
      setData((d) =>
        d ? { ...d, narrative: n } : { metrics: emptyMetrics(), narrative: n },
      );
      toast.success("Narrative generated");
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setGenerating(false);
    }
  };

  const buildReport = async () => {
    setBuildingReport(true);
    try {
      const r = await api.buildReport(id);
      setReportPath(r.path);
      setPreviewMd(null); // invalidate cached preview
      toast.success(`Report saved to ${r.path}`);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setBuildingReport(false);
    }
  };

  const metrics = data?.metrics ?? emptyMetrics();
  const narrative = data?.narrative as NarrativeRecord | undefined;

  return (
    <main className="container max-w-6xl py-12">
      <Link
        href={`/projects/${encodeURIComponent(id)}/merges`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to merges
      </Link>

      <header className="mt-8 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Wisetrix — {id} · Summary
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Run metrics, AI narrative, and the downloadable Upgrade Report.
          </p>
        </div>
        <div className="flex gap-2">
          <Button asChild variant="outline">
            <Link href={`/projects/${encodeURIComponent(id)}/jira`}>
              <Ticket className="h-4 w-4" />
              JIRA
            </Link>
          </Button>
          <Button
            variant="outline"
            onClick={() => void reload()}
            disabled={generating || buildingReport}
          >
            <RefreshCcw className="h-4 w-4" />
            Reload
          </Button>
        </div>
      </header>

      {error && (
        <Card className="mt-8 border-destructive/40 bg-destructive/5">
          <CardHeader>
            <CardTitle className="text-destructive">
              Couldn&apos;t load summary
            </CardTitle>
            <CardDescription>{error}</CardDescription>
          </CardHeader>
        </Card>
      )}

      <section className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Total artifacts" value={metrics.total_artifacts} />
        <Metric
          label="Merge candidates"
          value={metrics.decision_counts.Merge ?? 0}
        />
        <Metric
          label="Retained"
          value={metrics.decision_counts.Retain ?? 0}
        />
        <Metric
          label="Removed"
          value={metrics.decision_counts.Remove ?? 0}
        />
      </section>

      <section className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="HIGH risk" value={metrics.risk_counts.HIGH ?? 0} tone="danger" />
        <Metric
          label="MEDIUM risk"
          value={metrics.risk_counts.MEDIUM ?? 0}
          tone="warning"
        />
        <Metric label="LOW risk" value={metrics.risk_counts.LOW ?? 0} tone="success" />
        <Metric label="Merged" value={metrics.merged_count} tone="info" />
      </section>

      <section className="mt-10">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              AI narrative
            </CardTitle>
            <CardDescription>
              {narrative?.generated_at
                ? `Last generated ${narrative.generated_at}`
                : "Not generated yet"}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Button onClick={regenerate} disabled={generating}>
              {generating ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Sparkles className="h-4 w-4" />
              )}
              {narrative?.content
                ? "Regenerate narrative"
                : "Generate narrative"}
            </Button>
            {narrative?.content && (
              <div className="rounded-lg border border-border/60 bg-card/40 p-4">
                <Markdown>{narrative.content}</Markdown>
              </div>
            )}
          </CardContent>
        </Card>
      </section>

      <section className="mt-8">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-primary" />
              Upgrade Report
            </CardTitle>
            <CardDescription>
              Builds <code className="rounded bg-muted px-1 text-xs">UPGRADE_REPORT.md</code>{" "}
              on disk and renders PDF on demand.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <Button onClick={buildReport} disabled={buildingReport}>
                {buildingReport ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <FileText className="h-4 w-4" />
                )}
                {buildingReport ? "Building…" : "Build report"}
              </Button>
              <Button
                variant="outline"
                onClick={() => void togglePreview()}
                disabled={loadingPreview}
              >
                {loadingPreview ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : previewOpen ? (
                  <EyeOff className="h-4 w-4" />
                ) : (
                  <Eye className="h-4 w-4" />
                )}
                {previewOpen ? "Hide preview" : "Preview"}
              </Button>
              <Button
                variant="outline"
                onClick={() => void downloadPdf()}
                disabled={downloadingPdf}
              >
                {downloadingPdf ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Download className="h-4 w-4" />
                )}
                Download PDF
              </Button>
              <Button asChild variant="outline">
                <a
                  href={api.reportMarkdownUrl(id)}
                  download={`UPGRADE_REPORT_${id}.md`}
                >
                  <Download className="h-4 w-4" />
                  Download Markdown
                </a>
              </Button>
              {reportPath && (
                <p className="w-full text-xs text-muted-foreground">
                  Saved to{" "}
                  <code className="rounded bg-muted px-1">{reportPath}</code>
                </p>
              )}
            </div>

            {previewOpen && previewMd && (
              <div className="max-h-[70vh] overflow-y-auto rounded-lg border border-border/60 bg-card/40 p-6">
                <Markdown>{previewMd}</Markdown>
              </div>
            )}
          </CardContent>
        </Card>
      </section>
    </main>
  );
}

function emptyMetrics(): SummaryMetrics {
  return {
    total_artifacts: 0,
    decision_counts: {},
    risk_counts: { HIGH: 0, MEDIUM: 0, LOW: 0 },
    merged_count: 0,
    verdict_counts: { PASS: 0, WARN: 0, FAIL: 0 },
  };
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "danger" | "warning" | "success" | "info";
}) {
  const ring =
    tone === "danger"
      ? "ring-rose-500/40"
      : tone === "warning"
        ? "ring-amber-500/40"
        : tone === "success"
          ? "ring-emerald-500/40"
          : tone === "info"
            ? "ring-primary/40"
            : "ring-border/60";
  return (
    <div className={`rounded-xl border border-border/60 bg-card/40 p-4 ring-1 ${ring}`}>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}
