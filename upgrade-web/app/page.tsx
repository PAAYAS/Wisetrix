import Link from "next/link";
import { ArrowRight, Activity, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiStatus } from "@/components/api-status";

export default function Home() {
  return (
    <main className="relative min-h-screen overflow-hidden">
      {/* Background gradient + grid */}
      <div className="pointer-events-none absolute inset-0 -z-10">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_left,hsl(217_91%_60%/0.25),transparent_45%),radial-gradient(circle_at_bottom_right,hsl(280_91%_60%/0.2),transparent_45%)]" />
        <div className="absolute inset-0 bg-[linear-gradient(to_right,hsl(var(--border))_1px,transparent_1px),linear-gradient(to_bottom,hsl(var(--border))_1px,transparent_1px)] bg-[size:64px_64px] opacity-[0.04]" />
      </div>

      <div className="container max-w-6xl py-20">
        <header className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
            <Sparkles className="h-4 w-4 text-primary" />
            <span>Yantrix</span>
            <span className="rounded-full border border-border bg-card/50 px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
              v0.1 · preview
            </span>
          </div>
          <ApiStatus />
        </header>

        <section className="mt-24 max-w-3xl">
          <h1 className="text-balance bg-gradient-to-br from-foreground via-foreground to-foreground/60 bg-clip-text text-5xl font-semibold leading-tight tracking-tight text-transparent sm:text-6xl">
            GTM Docker-to-Docker upgrades,
            <br />
            <span className="bg-gradient-to-r from-primary to-fuchsia-400 bg-clip-text text-transparent">
              with AI automation ready.
            </span>
          </h1>
          <p className="mt-6 max-w-2xl text-lg leading-relaxed text-muted-foreground">
            Scan customer customizations, diff them against the target upgrade
            version, run quality-gated 3-way merges with AI, and ship a
            polished upgrade report — all from a single console.
          </p>

          <div className="mt-10 flex flex-wrap gap-3">
            <Button asChild size="lg" className="group">
              <Link href="/projects">
                Open projects
                <ArrowRight className="ml-1 h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </Link>
            </Button>
            <Button asChild variant="outline" size="lg">
              <a
                href={
                  process.env.NEXT_PUBLIC_API_BASE
                    ? `${process.env.NEXT_PUBLIC_API_BASE}/docs`
                    : "http://localhost:8000/docs"
                }
                target="_blank"
                rel="noreferrer"
              >
                <Activity className="h-4 w-4" />
                API docs
              </a>
            </Button>
          </div>
        </section>

        <section className="mt-24 grid gap-4 sm:grid-cols-3">
          {[
            {
              title: "Multi-agent core",
              body: "Compare · Risk · Merge · Review · Summary — each handled by a dedicated AI step.",
            },
            {
              title: "3-way merges",
              body: "Customer Artifacts × Previous Version × Target Version — intelligent 3-way merge with no manual coding.",
            },
            {
              title: "Ship-ready reports",
              body: "Quality gate verdicts, JIRA links, and a structured PDF for the upgrade committee.",
            },
          ].map((card) => (
            <div
              key={card.title}
              className="rounded-xl border border-border/60 bg-card/40 p-5 backdrop-blur-sm transition-colors hover:border-primary/40 hover:bg-card/60"
            >
              <h3 className="text-sm font-semibold text-foreground">{card.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                {card.body}
              </p>
            </div>
          ))}
        </section>
      </div>
    </main>
  );
}
