"use client";

import Link from "next/link";
import * as React from "react";
import { ArrowLeft, Plus, FolderGit2, Boxes, GitBranch } from "lucide-react";

import { api, ApiError, type ProjectSummary } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

type State =
  | { kind: "loading" }
  | { kind: "ok"; projects: ProjectSummary[] }
  | { kind: "error"; message: string };

export default function ProjectsPage() {
  const [state, setState] = React.useState<State>({ kind: "loading" });

  React.useEffect(() => {
    let cancelled = false;
    api
      .listProjects()
      .then((projects) => {
        if (!cancelled) setState({ kind: "ok", projects });
      })
      .catch((e: ApiError | Error) =>
        !cancelled &&
        setState({ kind: "error", message: e.message ?? "Failed to load projects" }),
      );
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="container max-w-6xl py-12">
      <div className="flex items-center justify-between">
        <Link
          href="/"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back
        </Link>
        <Button asChild>
          <Link href="/projects/new">
            <Plus className="h-4 w-4" />
            New project
          </Link>
        </Button>
      </div>

      <header className="mt-8">
        <h1 className="text-3xl font-semibold tracking-tight">
          Wisetrix · Projects
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Each project tracks one customer's source repo, a target version
          to upgrade to, and an optional previous version for 3-way merges.
        </p>
      </header>

      <section className="mt-8">
        {state.kind === "loading" && <SkeletonGrid />}
        {state.kind === "error" && (
          <Card className="border-destructive/40 bg-destructive/5">
            <CardHeader>
              <CardTitle className="text-destructive">
                Could not reach the API
              </CardTitle>
              <CardDescription>{state.message}</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Make sure the FastAPI server is running:
                <code className="ml-1 rounded bg-muted px-1.5 py-0.5 text-xs">
                  uvicorn upgrade_api.main:app --reload --port 8000
                </code>
              </p>
            </CardContent>
          </Card>
        )}
        {state.kind === "ok" && state.projects.length === 0 && (
          <EmptyState />
        )}
        {state.kind === "ok" && state.projects.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {state.projects.map((p) => (
              <ProjectCard key={p.id} project={p} />
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

function ProjectCard({ project }: { project: ProjectSummary }) {
  const { id, config } = project;
  const sourceLabel =
    config.source_type === "git"
      ? config.git_url
        ? `${config.git_branch ?? "main"} @ ${shortUrl(config.git_url)}`
        : "git (unset)"
      : config.source_root || "local (unset)";
  const targetLabel =
    config.target_type === "artifactory"
      ? config.target_version
        ? `Artifactory · ${config.target_version}`
        : "Artifactory"
      : config.target_system || "local";
  const baselineLabel =
    config.baseline_type === "none"
      ? "none"
      : config.baseline_type === "artifactory"
        ? config.baseline_version
          ? `Artifactory · ${config.baseline_version}`
          : "Artifactory"
        : config.baseline_system || "local";

  return (
    <Link
      href={`/projects/${encodeURIComponent(id)}/setup`}
      className="group block rounded-xl outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
    >
      <Card className="h-full transition-colors group-hover:border-primary/40 group-hover:bg-card/70">
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between">
            <CardTitle className="text-base">{id}</CardTitle>
            <Badge variant="muted">{config.target_type}</Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-2 text-xs">
          <Row icon={<GitBranch className="h-3.5 w-3.5" />} label="Source" value={sourceLabel} />
          <Row icon={<Boxes className="h-3.5 w-3.5" />} label="Target" value={targetLabel} />
          <Row icon={<FolderGit2 className="h-3.5 w-3.5" />} label="Baseline" value={baselineLabel} />
          <div className="pt-2 text-[11px] text-muted-foreground/80">
            Setup · Scan &amp; Compare
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

function Row({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-2 text-muted-foreground">
      <span className="text-primary/70">{icon}</span>
      <span className="w-16 shrink-0 text-[10px] uppercase tracking-wider">
        {label}
      </span>
      <span className="truncate text-foreground/80">{value}</span>
    </div>
  );
}

function shortUrl(url: string): string {
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname.replace(/\.git$/, "")}`;
  } catch {
    return url;
  }
}

function SkeletonGrid() {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="h-36 animate-pulse rounded-xl border border-border/40 bg-card/30"
        />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <Card className="border-dashed">
      <CardHeader>
        <CardTitle>No projects yet</CardTitle>
        <CardDescription>
          Create your first project to scan a customer source against a target
          upgrade version.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button asChild>
          <Link href="/projects/new">
            <Plus className="h-4 w-4" />
            New project
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
}
