# Database (seed-data) Policy

How the tool handles the **DB side** of an upgrade. This runs **after** the app
upgrade and is driven entirely by the app's per-artifact decisions. It is
deterministic Python (openpyxl) — no Claude.

Scope today: **`bizpolicydefs`** only. Each policy's data also lives in the
customer's `*_db` seed-data git repo as one workbook:

```
<project>_db/src/main/resources/seed_data_src/workspace/packages/<project>_seed_data/data/bppol/bppol.<ORG>.<POLICY_NAME>.xlsx
```

The `<project>` token and repo vary per customer; the subpath is derived from
the DB git URL (or set explicitly in project config).

## 1. Decision → DB action

For every `bizpolicydefs` artifact, the **app decision** dictates the DB action:

| App decision | DB action |
|--------------|-----------|
| **Remove** | Advise: remove the `bppol.<ORG>.<NAME>.xlsx` workbook from the DB git check-in (the artifact comes from the 26.2 core). No file produced. |
| **Retain** | Keep the DB workbook as-is. No change. |
| **Merge** | Reconcile the app's merged `bizpolicydef.json` into a **new** copy of the workbook. |

## 2. Reconciliation (Merge)

Source of truth = the app's merged `bizpolicydef.json`. The workbook is loaded
and saved with openpyxl so **every other tab, cell, style and value is
preserved**; only the matched data tab(s) gain rows.

### Tab discovery (dynamic — never hardcoded)
- Each policy's record arrays are routed to a data tab by matching the JSON
  record's field names against each `data.*` tab's header row. Tab aliases
  (`data.TBL_<timestamp>.1`) vary per export and per project, so routing is by
  content, not by name.
- Currently routed arrays: `MDI_RE_POLICY_CONFIG_RULE_INSTANCE`,
  `MDI_RE_POLICY_CONFIG_STATUS_COLUMNS`, `MDI_RE_POLICY_CONFIG_NAME`
  (others are routed automatically if a matching tab + identity exist).

### Identity (which records already exist)
- Matched by the **stable business key**, never by sequence numbers.
  - rule instances → `INSTANCE_ID` + `RULE_ID`
  - status columns → `STATUS_COLUMN`
  - name → `NAME` + `LOCALE`
- **`EXEC_SEQ` / `ROW_SEQ` are excluded from identity** — the app merge
  renumbers them, so including them would mis-flag existing rows as new and
  create duplicates. They are still written verbatim on genuinely-new rows.

### Additive only
- Only rows present in the merged JSON but missing from the workbook are
  appended. Existing rows are **never modified or deleted**.

### New-row column fill (per column, by class)
For each header column of the target tab:
1. **Generated surrogate** — `alt_key_*` whose values vary per row
   (e.g. `alt_key_instance`, `alt_key_columns`) → `max(existing) + 1`,
   incrementing per appended row.
2. **Policy-constant** — `org_code`, `policy_id`, `alt_key_policy`, and any
   column whose value is identical across existing rows → copy that constant.
3. **Audit** — `created_by` / `created_date` / `last_modified_*` → inherit from
   an existing sibling row.
4. **Direct field** — header matches a JSON record field → write the JSON value
   (sequence numbers like `exec_seq` written verbatim).
5. **Otherwise** → left blank.

## 3. Output & check-in

- Reconciled workbooks are written to `<db_output_dir>` (default
  `./output/<project>/_db/bppol/`), one per Merge-decision policy.
- The engineer downloads the new workbook(s) and check them into the `*_db`
  repo; Remove-decision policies are deleted from the check-in instead.
