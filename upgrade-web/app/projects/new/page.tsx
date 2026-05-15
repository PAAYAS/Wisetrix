"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { ProjectForm } from "@/components/project-form";

export default function NewProjectPage() {
  return (
    <main className="container max-w-4xl py-12">
      <Link
        href="/projects"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to projects
      </Link>

      <header className="mt-8">
        <h1 className="text-3xl font-semibold tracking-tight">
          Yantrix — New Project
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Pick a source (Git or local) and a target SYSTEM release. You can
          adjust everything later.
        </p>
      </header>

      <div className="mt-8">
        <ProjectForm mode="create" />
      </div>
    </main>
  );
}
