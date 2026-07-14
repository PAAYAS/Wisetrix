"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Loader2, Plug, Save, Trash2 } from "lucide-react";

import {
  api,
  ApiError,
  emptyProject,
  type BaselineType,
  type ProjectConfig,
  type SourceType,
  type TargetType,
  type UpgradeMode,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

// Default source subpaths per upgrade mode.
const DOCKER_SUBPATH = "client_delivery/src/main/resources/app_root/repos";
const WEBLOGIC_SUBPATH = "plugins/IMPLEMENTATION";

interface ProjectFormProps {
  mode: "create" | "edit";
  initialId?: string;
  initialConfig?: ProjectConfig;
}

export function ProjectForm({ mode, initialId, initialConfig }: ProjectFormProps) {
  const router = useRouter();
  const [id, setId] = React.useState(initialId ?? "");
  const [config, setConfig] = React.useState<ProjectConfig>(
    initialConfig ?? emptyProject(),
  );
  const [saving, setSaving] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [confirmOpen, setConfirmOpen] = React.useState(false);

  const update = <K extends keyof ProjectConfig>(
    key: K,
    value: ProjectConfig[K],
  ) => setConfig((c) => ({ ...c, [key]: value }));

  // Switching upgrade mode also swaps in that mode's default source subpath —
  // but only when the current subpath is empty or still the other mode's
  // default, so a customised subpath is never clobbered.
  const onModeChange = (mode: UpgradeMode) =>
    setConfig((c) => {
      const cur = c.source_subpath ?? "";
      const isDefault =
        cur === "" || cur === DOCKER_SUBPATH || cur === WEBLOGIC_SUBPATH;
      return {
        ...c,
        upgrade_mode: mode,
        source_subpath: isDefault
          ? mode === "weblogic"
            ? WEBLOGIC_SUBPATH
            : DOCKER_SUBPATH
          : cur,
      };
    });

  const onSave = async () => {
    if (mode === "create" && !id.trim()) {
      toast.error("Project ID is required");
      return;
    }
    setSaving(true);
    try {
      if (mode === "create") {
        await api.createProject(id.trim(), config);
        toast.success(`Project "${id.trim()}" created`);
        router.push(`/projects/${encodeURIComponent(id.trim())}/setup`);
      } else {
        await api.updateProject(initialId!, config);
        toast.success("Project saved");
      }
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async () => {
    if (!initialId) return;
    setDeleting(true);
    try {
      await api.deleteProject(initialId);
      toast.success(`Project "${initialId}" deleted`);
      router.push("/projects");
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
      setDeleting(false);
      setConfirmOpen(false);
    }
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Project</CardTitle>
          <CardDescription>
            A short identifier used for output paths and JIRA tags.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-2">
            <Label htmlFor="project-id">Project ID</Label>
            <Input
              id="project-id"
              value={id}
              disabled={mode === "edit"}
              placeholder="e.g. ALDI or ALDI Upgrade 26.2"
              onChange={(e) => setId(e.target.value)}
            />
            {mode === "edit" && (
              <p className="text-xs text-muted-foreground">
                Project ID can&apos;t be changed after creation.
              </p>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Upgrade mode</CardTitle>
          <CardDescription>
            How the customer&apos;s source is laid out.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RadioGroup
            value={config.upgrade_mode ?? "docker"}
            onValueChange={(v) => onModeChange(v as UpgradeMode)}
          >
            <RadioRow value="docker" label="Docker → Docker" />
            <RadioRow value="weblogic" label="WebLogic → Docker" />
          </RadioGroup>
        </CardContent>
      </Card>

      <SourceSection config={config} update={update} />
      <TargetSection config={config} update={update} />
      <BaselineSection config={config} update={update} />
      <DatabaseSection config={config} update={update} />
      <MiscSection config={config} update={update} />

      <div className="flex items-center justify-between gap-3">
        {mode === "edit" ? (
          <Button
            variant="destructive"
            onClick={() => setConfirmOpen(true)}
            disabled={saving || deleting}
          >
            <Trash2 className="h-4 w-4" />
            Delete
          </Button>
        ) : (
          <div />
        )}
        <Button onClick={onSave} disabled={saving || deleting}>
          {saving ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          {mode === "create" ? "Create project" : "Save changes"}
        </Button>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete project</DialogTitle>
            <DialogDescription>
              This removes <strong>{initialId}</strong> from{" "}
              <code className="rounded bg-muted px-1 py-0.5 text-xs">
                projects.json
              </code>
              . Output files on disk are not deleted.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setConfirmOpen(false)}
              disabled={deleting}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={onDelete}
              disabled={deleting}
            >
              {deleting && <Loader2 className="h-4 w-4 animate-spin" />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------- Sections ----------

interface SectionProps {
  config: ProjectConfig;
  update: <K extends keyof ProjectConfig>(key: K, value: ProjectConfig[K]) => void;
}

function SourceSection({ config, update }: SectionProps) {
  const [testing, setTesting] = React.useState(false);
  const testGit = async () => {
    if (!config.git_url) {
      toast.error("Enter a Git URL first");
      return;
    }
    setTesting(true);
    try {
      const r = await api.testGit(config.git_url);
      if (r.success) {
        toast.success(r.message);
      } else {
        toast.error(r.message);
      }
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Source</CardTitle>
        <CardDescription>
          Where the customer&apos;s source files live.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <RadioGroup
          value={config.source_type}
          onValueChange={(v) => update("source_type", v as SourceType)}
        >
          <RadioRow value="git" label="Git repository" />
          <RadioRow value="local" label="Local path" />
        </RadioGroup>

        {config.source_type === "git" ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              className="sm:col-span-2"
              label="Git URL"
              placeholder="https://bitbucket.org/e2open/aldi_app.git"
              value={config.git_url ?? ""}
              onChange={(v) => update("git_url", v)}
            />
            <Field
              label="Branch"
              placeholder="main"
              value={config.git_branch ?? ""}
              onChange={(v) => update("git_branch", v)}
            />
            <Field
              label="Source subpath"
              placeholder="client_delivery/src/main/resources/app_root/repos"
              value={config.source_subpath ?? ""}
              onChange={(v) => update("source_subpath", v)}
            />
            <div className="sm:col-span-2">
              <Button
                variant="outline"
                size="sm"
                onClick={testGit}
                disabled={testing}
              >
                {testing ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Plug className="h-4 w-4" />
                )}
                Test Git connection
              </Button>
            </div>
          </div>
        ) : (
          <Field
            label="Source root (local path)"
            placeholder="C:/path/to/customer/source"
            value={config.source_root ?? ""}
            onChange={(v) => update("source_root", v)}
          />
        )}
      </CardContent>
    </Card>
  );
}

function TargetSection({ config, update }: SectionProps) {
  const [testing, setTesting] = React.useState(false);
  const testArt = async () => {
    if (!config.artifactory_url) {
      toast.error("Enter an Artifactory URL first");
      return;
    }
    setTesting(true);
    try {
      const r = await api.testArtifactory(config.artifactory_url);
      if (r.success) toast.success(r.message);
      else toast.error(r.message);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Target Version</CardTitle>
        <CardDescription>
          The release the customer is upgrading to.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <RadioGroup
          value={config.target_type}
          onValueChange={(v) => update("target_type", v as TargetType)}
        >
          <RadioRow value="artifactory" label="Artifactory" />
          <RadioRow value="local" label="Local path" />
        </RadioGroup>

        {config.target_type === "artifactory" ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              className="sm:col-span-2"
              label="Artifactory URL"
              placeholder="https://sv4.art.e2open.com/.../gtm-install/26.2/"
              value={config.artifactory_url ?? ""}
              onChange={(v) => update("artifactory_url", v)}
            />
            <Field
              label="Target version"
              placeholder="26.2"
              value={config.target_version ?? ""}
              onChange={(v) => update("target_version", v)}
            />
            <div className="flex items-end">
              <Button
                variant="outline"
                size="sm"
                onClick={testArt}
                disabled={testing}
              >
                {testing ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Plug className="h-4 w-4" />
                )}
                Test Artifactory
              </Button>
            </div>
          </div>
        ) : (
          <Field
            label="Target version path"
            placeholder="C:/path/to/SYSTEM"
            value={config.target_system ?? ""}
            onChange={(v) => update("target_system", v)}
          />
        )}
      </CardContent>
    </Card>
  );
}

function BaselineSection({ config, update }: SectionProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Baseline (optional)</CardTitle>
        <CardDescription>
          The previous version the customer upgraded from. Enables 3-way merges.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <RadioGroup
          value={config.baseline_type}
          onValueChange={(v) => update("baseline_type", v as BaselineType)}
        >
          <RadioRow value="none" label="None" />
          <RadioRow value="artifactory" label="Artifactory" />
          <RadioRow value="local" label="Local path" />
        </RadioGroup>

        {config.baseline_type === "artifactory" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              className="sm:col-span-2"
              label="Baseline Artifactory URL"
              placeholder="https://sv4.art.e2open.com/.../24.4.11/"
              value={config.baseline_url ?? ""}
              onChange={(v) => update("baseline_url", v)}
            />
            <Field
              label="Baseline version"
              placeholder="24.4.11"
              value={config.baseline_version ?? ""}
              onChange={(v) => update("baseline_version", v)}
            />
          </div>
        )}

        {config.baseline_type === "local" && (
          <Field
            label="Baseline SYSTEM path"
            placeholder="C:/path/to/baseline/SYSTEM"
            value={config.baseline_system ?? ""}
            onChange={(v) => update("baseline_system", v)}
          />
        )}
      </CardContent>
    </Card>
  );
}

function DatabaseSection({ config, update }: SectionProps) {
  const [testing, setTesting] = React.useState(false);
  const enabled = !!config.db_enabled;
  const dbType = config.db_source_type ?? "git";

  const testGit = async () => {
    if (!config.db_git_url) {
      toast.error("Enter a DB Git URL first");
      return;
    }
    setTesting(true);
    try {
      const r = await api.testGit(config.db_git_url);
      if (r.success) toast.success(r.message);
      else toast.error(r.message);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      toast.error(msg);
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Database (seed data)</CardTitle>
        <CardDescription>
          Optional. The customer&apos;s <code>*_db</code> seed-data repo holding
          the <code>bppol.*.xlsx</code> workbooks for bizpolicydefs. Runs after
          the app upgrade — the app decision drives the DB action.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="h-4 w-4"
            checked={enabled}
            onChange={(e) => update("db_enabled", e.target.checked)}
          />
          Enable database handling
        </label>

        {enabled && (
          <>
            <RadioGroup
              value={dbType}
              onValueChange={(v) => update("db_source_type", v as SourceType)}
            >
              <RadioRow value="git" label="Git repository" />
              <RadioRow value="local" label="Local path" />
            </RadioGroup>

            {dbType === "git" ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  className="sm:col-span-2"
                  label="DB Git URL"
                  placeholder="https://git.dev.e2open.com/scm/ser/agco_db.git"
                  value={config.db_git_url ?? ""}
                  onChange={(v) => update("db_git_url", v)}
                />
                <Field
                  label="Branch"
                  placeholder="main"
                  value={config.db_git_branch ?? ""}
                  onChange={(v) => update("db_git_branch", v)}
                />
                <Field
                  label="DB subpath (leave blank to auto-derive)"
                  placeholder="src/main/resources/seed_data_src/workspace/packages/<project>_seed_data/data"
                  value={config.db_source_subpath ?? ""}
                  onChange={(v) => update("db_source_subpath", v)}
                />
                <div className="sm:col-span-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={testGit}
                    disabled={testing}
                  >
                    {testing ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Plug className="h-4 w-4" />
                    )}
                    Test DB Git connection
                  </Button>
                </div>
              </div>
            ) : (
              <Field
                label="DB source root (local path to .../data)"
                placeholder="C:/path/to/agco_db/.../agco_seed_data/data"
                value={config.db_source_root ?? ""}
                onChange={(v) => update("db_source_root", v)}
              />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function MiscSection({ config, update }: SectionProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Tracking</CardTitle>
        <CardDescription>
          Optional metadata used by the JIRA tab.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Field
          label="JIRA project URL"
          placeholder="https://jira.dev.e2open.com/projects/ALDI"
          value={config.jira_project_url ?? ""}
          onChange={(v) => update("jira_project_url", v)}
        />
      </CardContent>
    </Card>
  );
}

// ---------- Primitives ----------

function Field({
  label,
  placeholder,
  value,
  onChange,
  className,
}: {
  label: string;
  placeholder?: string;
  value: string;
  onChange: (v: string) => void;
  className?: string;
}) {
  const id = React.useId();
  return (
    <div className={`grid gap-2 ${className ?? ""}`}>
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

function RadioRow({ value, label }: { value: string; label: string }) {
  const id = React.useId();
  return (
    <div className="flex items-center gap-2">
      <RadioGroupItem id={id} value={value} />
      <Label htmlFor={id} className="cursor-pointer font-normal">
        {label}
      </Label>
    </div>
  );
}
