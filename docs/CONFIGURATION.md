# Configuration

Wisetrix has two configuration files:

- **`config.yaml`** — credentials (Artifactory, JIRA, Git). Gitignored.
- **`projects.json`** — per-customer project definitions. Managed from the UI.

---

## Credentials — `config.yaml`

Copy the tracked template and fill it in:

```bash
cp config.example.yml config.yaml
```

`config.yaml` (and `config.yml`) are gitignored — **never commit credentials**. The loaders accept either filename.

```yaml
artifactory:
  base_url: "https://sv4.art.e2open.com"
  username: "you@wisetechglobal.com"
  password: "<API key or password>"     # used to download GTM install JARs

jira:
  enabled: true                          # set false to disable all JIRA calls
  base_url: "https://jira.dev.e2open.com/jira"
  username: "<username>"
  password: "<API token or password>"

git:
  # Only needed for private HTTPS repos. For SSH, use your SSH key instead.
  username: "<username>"
  password: "<personal access token>"
```

When `jira.enabled` is `false`, every JIRA operation is a safe no-op — the rest of the pipeline runs unaffected.

---

## Projects — `projects.json`

Each entry defines one upgrade project. Use the UI's **Add / Update Project** form rather than editing by hand. A project resolves its **source**, **target**, and **baseline** through one of three provider types.

### Example (Git source + Artifactory target/baseline)

```json
{
  "AGCO_26.2": {
    "source_type":       "git",
    "git_url":           "https://git.dev.e2open.com/scm/ser/agco_app.git",
    "git_branch":        "24.2.9-NEXT_RELEASE",
    "source_subpath":    "client_delivery/src/main/resources/app_root/repos",

    "target_type":       "artifactory",
    "artifactory_url":   "https://sv4.art.e2open.com/.../gtm-install/26.2",
    "target_version":    "26.2",

    "baseline_type":     "artifactory",
    "baseline_url":      "https://sv4.art.e2open.com/.../gtm-install/24.2.9",
    "baseline_version":  "24.2.9",

    "merge_output_dir":  "./output/AGCO_26.2",
    "jira_project_url":  "https://jira.dev.e2open.com/jira/projects/AGCO/",

    "db_enabled":        true,
    "db_git_url":        "https://git.dev.e2open.com/scm/ser/agco_db.git",
    "db_git_branch":     "24.2.9-NEXT_RELEASE",
    "db_source_subpath": ""
  }
}
```

### Field reference

| Field | Purpose |
|-------|---------|
| `source_type` | `git` \| `artifactory` \| (omit for local) |
| `git_url`, `git_branch`, `source_subpath` | Git source: repo, branch, and path from repo root to the `repos` dir |
| `source_root` | **Local** source only — absolute path to the customer's `repos` dir |
| `target_type`, `artifactory_url`, `target_version` | The new SYSTEM release to upgrade onto |
| `target_system` | **Local** target only — absolute path to the SYSTEM `repos` dir |
| `baseline_type`, `baseline_url`, `baseline_version` | The previous SYSTEM the customer was on (enables true 3-way merge) |
| `baseline_system` | **Local** baseline only — absolute path |
| `merge_output_dir` | Where merged artifacts are written |
| `jira_project_url` | JIRA project link or key, for ticket matching |
| `db_enabled` | Enable DB seed-data (`bizpolicydefs` → `bppol` xlsx) reconciliation |
| `db_git_url`, `db_git_branch`, `db_source_subpath` | The customer's DB seed-data repo, branch, and path |

### Source providers

| Provider | Selected by | Resolves to | Cache |
|----------|-------------|-------------|-------|
| **Git** | `source_type: "git"` | Cloned repo → `{source_subpath}` | `~/.wisetrix/git_cache/` |
| **Artifactory** | `target_type`/`baseline_type: "artifactory"` | Downloaded JAR → `app_root/repos/SYSTEM` | `~/.wisetrix/artifactory_cache/` |
| **Local** | `source_type` omitted, `source_root` set | Local paths as-is | none |

A baseline is optional but strongly recommended: without it, the merge falls back to a 2-way overlay (SYSTEM base with customer values layered on top) instead of a true 3-way merge.

### Adding a customer

1. UI sidebar → **Add / Update Project**.
2. Pick a project ID (e.g. `EMRSN_26.2`), choose the provider type, fill the fields, save.
3. Run **Scan & Compare**, then **Merges**.

No code changes are needed — the customer bucket is detected automatically from the top-level folder under the source root.
