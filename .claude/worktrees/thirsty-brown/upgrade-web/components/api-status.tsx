"use client";

import * as React from "react";

import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

type State =
  | { kind: "checking" }
  | { kind: "ok"; time: string }
  | { kind: "error"; message: string };

export function ApiStatus() {
  const [state, setState] = React.useState<State>({ kind: "checking" });

  React.useEffect(() => {
    let cancelled = false;
    api
      .health()
      .then((r) => {
        if (!cancelled) setState({ kind: "ok", time: r.time });
      })
      .catch((e: ApiError | Error) => {
        if (!cancelled) {
          setState({
            kind: "error",
            message:
              e instanceof ApiError
                ? `API ${e.status}`
                : e.message || "unreachable",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const dotClass = cn(
    "inline-block h-2 w-2 rounded-full",
    state.kind === "ok" && "bg-emerald-400 shadow-[0_0_12px_2px_rgba(52,211,153,0.6)]",
    state.kind === "checking" && "bg-amber-400 animate-pulse",
    state.kind === "error" && "bg-rose-500",
  );

  const label =
    state.kind === "ok"
      ? "API online"
      : state.kind === "checking"
        ? "Checking API…"
        : `API ${state.message}`;

  return (
    <div className="flex items-center gap-2 rounded-full border border-border/60 bg-card/40 px-3 py-1 text-xs text-muted-foreground backdrop-blur-sm">
      <span className={dotClass} />
      <span>{label}</span>
    </div>
  );
}
