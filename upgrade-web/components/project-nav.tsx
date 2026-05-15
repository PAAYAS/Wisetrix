"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  FileText,
  GitCompare,
  GitMerge,
  Search,
  Settings2,
  Ticket,
} from "lucide-react";

interface Tab {
  href: (id: string) => string;
  label: string;
  match: string;
  icon: React.ComponentType<{ className?: string }>;
}

const TABS: Tab[] = [
  {
    href: (id) => `/projects/${encodeURIComponent(id)}/setup`,
    label: "Setup",
    match: "setup",
    icon: Settings2,
  },
  {
    href: (id) => `/projects/${encodeURIComponent(id)}/scan`,
    label: "Scan & Compare",
    match: "scan",
    icon: Search,
  },
  {
    href: (id) => `/projects/${encodeURIComponent(id)}/merges`,
    label: "Merges",
    match: "merges",
    icon: GitMerge,
  },
  {
    href: (id) => `/projects/${encodeURIComponent(id)}/diff`,
    label: "Diff & Review",
    match: "diff",
    icon: GitCompare,
  },
  {
    href: (id) => `/projects/${encodeURIComponent(id)}/summary`,
    label: "Summary",
    match: "summary",
    icon: FileText,
  },
  {
    href: (id) => `/projects/${encodeURIComponent(id)}/jira`,
    label: "JIRA",
    match: "jira",
    icon: Ticket,
  },
];

export function ProjectNav({ projectId }: { projectId: string }) {
  const pathname = usePathname();
  const id = decodeURIComponent(projectId);
  const active = TABS.find((t) => pathname?.includes(`/${t.match}`))?.match;

  return (
    <nav className="sticky top-0 z-30 -mx-4 mb-4 border-b border-border/60 bg-background/80 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="container max-w-7xl">
        <div className="flex flex-wrap items-center gap-1 text-sm">
          <Link
            href="/projects"
            className="mr-2 text-xs text-muted-foreground hover:text-foreground"
          >
            ← All projects
          </Link>
          <span className="mr-3 truncate font-mono text-xs text-muted-foreground">
            {id}
          </span>
          <div className="flex flex-wrap gap-0.5">
            {TABS.map(({ href, label, match, icon: Icon }) => {
              const isActive = active === match;
              return (
                <Link
                  key={match}
                  href={href(id)}
                  className={
                    isActive
                      ? "inline-flex items-center gap-1.5 rounded-md bg-primary/15 px-2.5 py-1 text-xs font-medium text-primary"
                      : "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
                  }
                >
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </Link>
              );
            })}
          </div>
        </div>
      </div>
    </nav>
  );
}
