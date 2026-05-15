"use client";

import * as React from "react";
import {
  ExternalLink,
  Link2,
  Loader2,
  RefreshCcw,
  Search,
} from "lucide-react";
import { toast } from "sonner";

import {
  api,
  ApiError,
  type JiraMatchResponse,
  type JiraStatus,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function JiraPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);

  const [status, setStatus] = React.useState<JiraStatus | null>(null);
  const [matched, setMatched] = React.useState<Record<string, string>>({});
  const [statusError, setStatusError] = React.useState<string | null>(null);
  const [matching, setMatching] = React.useState(false);
  const [filter, setFilter] = React.useState("");
  const [onlyMatched, setOnlyMatched] = React.useState(false);

  const reload = React.useCallback(async () => {
    setStatusError(null);
    try {
      const [s, m] = await Promise.all([
        api.jiraStatus(id),
        api.jiraMatchCached(id),
      ]);
      setStatus(s);
      setMatched(m.matched ?? {});
    } catch (e) {
      setStatusError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }, [id]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const handleMatch = async () => {
    setMatching(true);
    try {
      const r: JiraMatchResponse = await api.jiraMatch(id);
      setMatched(r.matched ?? {});
      const total = r.total ?? Object.keys(r.matched).length;
      toast.success(`Matched ${r.count}/${total}`);
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setMatching(false);
    }
  };

  const entries = Object.entries(matched).sort((a, b) =>
    a[0].localeCompare(b[0]),
  );
  const matchedCount = entries.filter(
    ([, v]) => v && v !== "Not Found",
  ).length;

  const filtered = entries.filter(([artifact, ticket]) => {
    if (onlyMatched && (!ticket || ticket === "Not Found")) return false;
    if (!filter) return true;
    const f = filter.toLowerCase();
    return (
      artifact.toLowerCase().includes(f) ||
      (ticket ?? "").toLowerCase().includes(f)
    );
  });

  return (
    <main className="container max-w-5xl py-10">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Yantrix — {id} · JIRA tickets
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            JIRA ticket numbers associated with each artifact in this upgrade.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void reload()}>
            <RefreshCcw className="h-4 w-4" />
            Reload
          </Button>
          <Button
            onClick={() => void handleMatch()}
            disabled={!status?.enabled || matching}
          >
            {matching ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Link2 className="h-4 w-4" />
            )}
            Find tickets
          </Button>
        </div>
      </header>

      {statusError && (
        <Card className="mt-6 border-destructive/40 bg-destructive/5">
          <CardHeader>
            <CardTitle className="text-destructive">
              Couldn&apos;t reach JIRA
            </CardTitle>
            <CardDescription>{statusError}</CardDescription>
          </CardHeader>
        </Card>
      )}

      {status && !status.enabled && (
        <Card className="mt-6 border-amber-500/40 bg-amber-500/5">
          <CardHeader>
            <CardTitle className="text-sm">JIRA is disabled</CardTitle>
            <CardDescription>
              Enable JIRA in <code className="rounded bg-muted px-1">config.yaml</code>{" "}
              (set <code className="rounded bg-muted px-1">jira.enabled: true</code>{" "}
              and provide base_url + credentials) to use this tab.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <section className="mt-6 grid gap-3 sm:grid-cols-3">
        <Field label="Project">
          <span className="font-mono text-xs">
            {status?.project_key || "—"}
          </span>
        </Field>
        <Field label="Artifacts checked">
          <span className="font-mono text-xs">{entries.length}</span>
        </Field>
        <Field label="With tickets">
          <span className="font-mono text-xs">
            {matchedCount}
            {entries.length > 0 ? ` (${Math.round((matchedCount / entries.length) * 100)}%)` : ""}
          </span>
        </Field>
      </section>

      <Card className="mt-6">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3 pb-3">
          <CardTitle className="text-sm font-medium">
            Artifact → Ticket
          </CardTitle>
          <div className="flex items-center gap-3">
            <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={onlyMatched}
                onChange={(e) => setOnlyMatched(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-border"
              />
              Only matched
            </label>
            <div className="flex items-center gap-1.5">
              <Search className="h-3.5 w-3.5 text-muted-foreground" />
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter…"
                className="h-8 w-48 rounded-md border border-input bg-background px-2 text-xs placeholder:text-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {filtered.length === 0 ? (
            <p className="px-6 py-8 text-center text-sm text-muted-foreground">
              {entries.length === 0
                ? 'No artifacts checked yet. Click "Find tickets" to search the JIRA project.'
                : "No artifacts match the current filter."}
            </p>
          ) : (
            <div className="max-h-[65vh] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-muted/60 text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 font-medium">Artifact</th>
                    <th className="px-4 py-2 font-medium">Ticket</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/40">
                  {filtered.map(([artifact, ticket]) => {
                    const isHit = ticket && ticket !== "Not Found";
                    const ticketKeys = isHit
                      ? ticket
                          .split(",")
                          .map((k) => k.trim())
                          .filter(Boolean)
                      : [];
                    return (
                      <tr key={artifact} className="hover:bg-muted/30">
                        <td className="px-4 py-2 font-mono text-xs">
                          {artifact}
                        </td>
                        <td className="px-4 py-2">
                          {isHit ? (
                            <div className="flex flex-wrap gap-1.5">
                              {ticketKeys.map((key) => (
                                <a
                                  key={key}
                                  className="inline-flex items-center gap-1 rounded-md border border-border/60 bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-primary hover:underline"
                                  href={
                                    status?.base_url
                                      ? `${status.base_url}/browse/${key}`
                                      : "#"
                                  }
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  {key}
                                  <ExternalLink className="h-3 w-3" />
                                </a>
                              ))}
                            </div>
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              Not Found
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border/60 bg-card/40 p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-sm">{children}</div>
    </div>
  );
}
