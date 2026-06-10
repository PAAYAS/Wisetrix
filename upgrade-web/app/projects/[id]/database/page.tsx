"use client";

import Link from "next/link";
import * as React from "react";
import {
  Database,
  Download,
  Loader2,
  Play,
  RefreshCcw,
} from "lucide-react";
import { toast } from "sonner";

import {
  api,
  ApiError,
  type DbAction,
  type DbMergeRecord,
  type DbScanResponse,
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

function ActionBadge({ type }: { type: DbAction["action_type"] }) {
  const map: Record<DbAction["action_type"], { label: string; cls: string }> = {
    merge: { label: "Merge", cls: "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300" },
    retain: { label: "Retain", cls: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300" },
    remove: { label: "Remove", cls: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300" },
    remove_absent: { label: "Remove", cls: "bg-muted text-muted-foreground" },
    skip: { label: "—", cls: "bg-muted text-muted-foreground" },
  };
  const { label, cls } = map[type];
  return <span className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${cls}`}>{label}</span>;
}

export default function DatabasePage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);

  const [scan, setScan] = React.useState<DbScanResponse | null>(null);
  const [merges, setMerges] = React.useState<Record<string, DbMergeRecord>>({});
  const [enabled, setEnabled] = React.useState<boolean | null>(null);
  const [appScanned, setAppScanned] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [scanning, setScanning] = React.useState(false);
  const [busyKey, setBusyKey] = React.useState<string | null>(null);
  const [mergingAll, setMergingAll] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [status, cached, mg] = await Promise.all([
        api.dbStatus(id),
        api.dbScanGet(id).catch(() => ({ actions: [] }) as DbScanResponse),
        api.dbMerges(id).catch(() => ({ done: {} })),
      ]);
      setEnabled(status.enabled);
      setAppScanned(status.app_scanned);
      setScan(cached);
      setMerges(mg.done ?? {});
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  }, [id]);

  React.useEffect(() => {
    load();
  }, [load]);

  const runScan = async () => {
    setScanning(true);
    try {
      const res = await api.dbScan(id);
      setScan(res);
      if (res.resolve_error) toast.error(`DB repo: ${res.resolve_error}`);
      else toast.success(`Found ${res.actions.length} bizpolicydefs artifact(s)`);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setScanning(false);
    }
  };

  const mergeOne = async (key: string) => {
    setBusyKey(key);
    try {
      const rec = await api.dbMergeOne(id, key);
      setMerges((m) => ({ ...m, [key]: rec }));
      toast.success(`Reconciled ${rec.name} — ${rec.total_added} row(s) added`);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setBusyKey(null);
    }
  };

  const mergeAll = async () => {
    setMergingAll(true);
    try {
      const res = await api.dbMergeAll(id);
      toast.success(`Reconciled ${res.merged.length}/${res.total}`);
      if (res.failed.length) toast.error(`${res.failed.length} failed`);
      await load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setMergingAll(false);
    }
  };

  const actions = scan?.actions ?? [];
  const mergeActions = actions.filter((a) => a.action_type === "merge");
  const readyCount = mergeActions.filter((a) => a.ready).length;
  const stale = !!scan?.stale;
  const pending = scan?.pending_app_merges ?? 0;

  if (loading) {
    return (
      <div className="container max-w-7xl py-6">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="container max-w-7xl space-y-6 py-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <Database className="h-6 w-6" /> Database
        </h1>
        <p className="text-sm text-muted-foreground">
          Reconcile bizpolicydefs into the customer&apos;s <code>bppol.*.xlsx</code> seed data.
        </p>
      </div>

      {/* Gating banners */}
      {enabled === false && (
        <Card>
          <CardHeader>
            <CardTitle>Database handling is off</CardTitle>
            <CardDescription>
              Enable it in <Link className="underline" href={`/projects/${encodeURIComponent(id)}/setup`}>Setup → Database</Link> and
              provide the <code>*_db</code> git repo to use this tab.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {enabled && !appScanned && (
        <Card>
          <CardHeader>
            <CardTitle>Run the app comparison first</CardTitle>
            <CardDescription>
              The DB step is driven by the app&apos;s bizpolicydefs decisions. Complete{" "}
              <Link className="underline" href={`/projects/${encodeURIComponent(id)}/scan`}>Scan &amp; Compare</Link> (and
              the relevant Merges), then return here.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {enabled && appScanned && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={runScan} disabled={scanning}>
              {scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCcw className="h-4 w-4" />}
              {scan ? "Re-scan DB" : "Scan DB"}
            </Button>
            <Button
              variant="outline"
              onClick={mergeAll}
              disabled={mergingAll || readyCount === 0 || stale}
            >
              {mergingAll ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              Reconcile all ready ({readyCount})
            </Button>
            {Object.keys(merges).length > 0 && (
              <a href={api.dbDownloadAllUrl(id)} download>
                <Button variant="ghost" size="sm">
                  <Download className="h-4 w-4" /> Download all xlsx
                </Button>
              </a>
            )}
          </div>

          {stale && (
            <Card className="border-amber-300 dark:border-amber-800">
              <CardContent className="py-3 text-sm text-amber-700 dark:text-amber-400">
                ⚠ The app comparison or a bizpolicydefs merge changed since this DB
                scan — the actions below may be out of date. Click <strong>Re-scan DB</strong> before reconciling.
              </CardContent>
            </Card>
          )}

          {pending > 0 && (
            <Card>
              <CardContent className="py-3 text-sm text-muted-foreground">
                ℹ {pending} bizpolicydef(s) with a Merge decision haven&apos;t been merged on the
                app side yet — finish those{" "}
                <Link className="underline" href={`/projects/${encodeURIComponent(id)}/merges`}>app merges</Link>{" "}
                to reconcile them here.
              </CardContent>
            </Card>
          )}

          {scan?.resolve_error && (
            <Card>
              <CardContent className="py-3 text-sm text-amber-600">
                DB repo could not be resolved: {scan.resolve_error}
              </CardContent>
            </Card>
          )}

          {actions.length === 0 ? (
            <Card>
              <CardContent className="py-6 text-sm text-muted-foreground">
                {scan ? "No bizpolicydefs artifacts found in the app comparison." : "Click “Scan DB” to derive actions from the app comparison."}
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardContent className="overflow-x-auto p-0">
                <table className="w-full text-sm">
                  <thead className="border-b border-border/60 text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2">Policy</th>
                      <th className="px-3 py-2">App decision</th>
                      <th className="px-3 py-2">DB action</th>
                      <th className="px-3 py-2">DB workbook</th>
                      <th className="px-3 py-2">Result</th>
                      <th className="px-3 py-2"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {actions.map((a) => {
                      const rec = merges[a.key];
                      return (
                        <tr key={a.key} className="border-b border-border/30 align-top">
                          <td className="px-3 py-2">
                            <div className="font-mono text-xs">{a.name}</div>
                            <div className="text-[11px] text-muted-foreground">{a.bucket}</div>
                          </td>
                          <td className="px-3 py-2">
                            <Badge variant="outline">{a.app_decision}</Badge>
                          </td>
                          <td className="px-3 py-2">
                            <ActionBadge type={a.action_type} />
                            <div className="mt-1 max-w-xs text-[11px] text-muted-foreground">{a.message}</div>
                          </td>
                          <td className="px-3 py-2 text-xs">
                            {a.bppol_exists ? (
                              <span className="break-all font-mono text-emerald-700 dark:text-emerald-400" title={a.bppol_path}>
                                {a.workbook_name}
                              </span>
                            ) : a.action_type === "remove_absent" ? (
                              <span className="text-muted-foreground">not in repo</span>
                            ) : (
                              <span className="text-amber-600">not found</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-xs">
                            {rec ? (
                              <div>
                                <span className="font-medium text-emerald-600">+{rec.total_added} row(s)</span>
                                {rec.tabs.filter((t) => t.added > 0).map((t) => (
                                  <div key={t.sheet} className="text-[11px] text-muted-foreground">
                                    {t.json_key.replace("MDI_RE_POLICY_CONFIG_", "")}: +{t.added}
                                    {t.added_alt_keys && Object.entries(t.added_alt_keys).map(([c, ks]) => (
                                      <span key={c}> ({c} {ks.join(", ")})</span>
                                    ))}
                                  </div>
                                ))}
                                {rec.warnings?.map((w, i) => (
                                  <div key={i} className="text-[11px] text-amber-600">⚠ {w}</div>
                                ))}
                              </div>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-right">
                            {a.action_type === "merge" ? (
                              <div className="flex items-center justify-end gap-1">
                                <Button
                                  size="sm"
                                  variant={rec ? "ghost" : "default"}
                                  disabled={busyKey === a.key || !a.ready || stale}
                                  onClick={() => mergeOne(a.key)}
                                  title={
                                    stale
                                      ? "Re-scan DB first — app data changed"
                                      : a.ready
                                        ? ""
                                        : !a.app_merged
                                          ? "Run the app Merge for this policy first"
                                          : "bppol workbook not found in DB repo"
                                  }
                                >
                                  {busyKey === a.key ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                                  {rec ? "Re-run" : "Reconcile"}
                                </Button>
                                {rec && (
                                  <a href={api.dbDownloadOneUrl(id, a.key)} download>
                                    <Button size="sm" variant="ghost">
                                      <Download className="h-4 w-4" />
                                    </Button>
                                  </a>
                                )}
                              </div>
                            ) : a.action_type === "remove" ? (
                              <span className="text-[11px] font-medium text-amber-600">
                                Remove from DB git check-in
                              </span>
                            ) : a.action_type === "remove_absent" ? (
                              <span className="text-[11px] text-muted-foreground">
                                Nothing to remove
                              </span>
                            ) : (
                              <span className="text-[11px] text-muted-foreground">No action</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
