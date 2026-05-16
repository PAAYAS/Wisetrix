"use client";

import Link from "next/link";
import * as React from "react";
import { ArrowLeft, Loader2, Search } from "lucide-react";

import { api, ApiError, type ProjectConfig } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ProjectForm } from "@/components/project-form";

type State =
  | { kind: "loading" }
  | { kind: "ok"; id: string; config: ProjectConfig }
  | { kind: "error"; message: string };

export default function ProjectSetupPage({
  params,
}: {
  params: { id: string };
}) {
  const id = decodeURIComponent(params.id);
  const [state, setState] = React.useState<State>({ kind: "loading" });

  React.useEffect(() => {
    let cancelled = false;
    api
      .getProject(id)
      .then((p) => {
        if (!cancelled) setState({ kind: "ok", id: p.id, config: p.config });
      })
      .catch((e: ApiError | Error) => {
        if (!cancelled)
          setState({ kind: "error", message: e.message ?? "Not found" });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  return (
    <main className="container max-w-4xl py-12">
      <Link
        href="/projects"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to projects
      </Link>

      <header className="mt-8 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Yantrix — {id}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Edit this project&apos;s source, target version, and baseline.
          </p>
        </div>
        <Button asChild variant="outline">
          <Link href={`/projects/${encodeURIComponent(id)}/scan`}>
            <Search className="h-4 w-4" />
            Scan &amp; Compare
          </Link>
        </Button>
      </header>

      <div className="mt-8">
        {state.kind === "loading" && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading project…
          </div>
        )}
        {state.kind === "error" && (
          <Card className="border-destructive/40 bg-destructive/5">
            <CardHeader>
              <CardTitle className="text-destructive">
                Could not load project
              </CardTitle>
              <CardDescription>{state.message}</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                It may have been deleted or the API is unreachable.
              </p>
            </CardContent>
          </Card>
        )}
        {state.kind === "ok" && (
          <ProjectForm
            mode="edit"
            initialId={state.id}
            initialConfig={state.config}
          />
        )}
      </div>
    </main>
  );
}
