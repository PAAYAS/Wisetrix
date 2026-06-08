# Merge Policy

This document defines the rules for comparing and merging artifacts between ALDI (customized source) and SYSTEM 26.2 (upgrade target). Claude uses these rules to decide per-file and per-artifact outcomes.

---

## 1. File-Level Comparison

### JSON Files
- Compare as **structured objects**, not as raw text.
- Key ordering does NOT matter.
- Whitespace / indentation differences are ignored.
- Trailing commas and formatting variations are ignored.

### XML Files
- Compare as **tree structure**.
- Attribute ordering does NOT matter.
- Whitespace between tags is ignored.

### Code Files (.java, .js, .jsp, .groovy)
- Compare line-by-line after trimming trailing whitespace.
- **Comment-only differences = IDENTICAL**.
  - If ALDI has `// someMethod();` and SYSTEM has `someMethod();`, the ALDI customization is **intentional** — treat the file as needing **Retain**, NOT Merge.
  - ALDI commented-out code represents deliberate disabling of base behaviour.

### Binary / Unknown Files
- Compare by byte hash.

---

## 2. Artifact-Level Rollup

After deciding each file's status, roll up to an artifact-level decision:

| Condition | Artifact Decision |
|-----------|-------------------|
| ANY file needs Merge | **Merge** |
| All files byte-identical | **Remove** (ALDI version is now redundant) |
| Mix of Retain + Identical, no Merge needed | **Retain** (ALDI customization stands as-is) |
| Files ONLY in ALDI (not in SYSTEM) | **Retain** (new customization) |
| Files ONLY in SYSTEM (not in ALDI) | **Merge** (bring new SYSTEM file into result) |

---

## 3. Business Rules (Post-Decision)

Apply these after per-artifact decisions:

### Existing rules
- If both `windowdefs/{name}` AND `adhoc_windowdefs/{name}` exist in **customer source** → **Remove** `windowdefs/{name}` (Rule 1).
- If both `datasets/SEARCH/ADHOC_SEARCH_{name}` AND `datasets/REPORT/{name}` exist → **Remove** the SEARCH variant.
- If an artifact is listed as deprecated in SYSTEM 26.2 release notes → **Remove**.

### Rule 2 — windowdefs → adhoc_windowdefs promotion
If `windowdefs/{name}` exists in the customer source but `adhoc_windowdefs/{name}` does NOT exist in the customer source — check whether `adhoc_windowdefs/{name}` exists in the **target version (SYSTEM 26.2)**. If yes:
- **Remove** `windowdefs/{name}` from the output.
- The customizations from `windowdefs/{name}` must be merged into `adhoc_windowdefs/{name}` of the target version.

### Rules 3 & 4 — BASE_*_NAME redirect (integration_def, datasets, reports, searches, templates, windowdefs)
When a customer artifact has **no counterpart in SYSTEM** (target does not exist), check the artifact's JSON file for a `BASE_*_NAME` tag:

| Tag | Example |
|-----|---------|
| `BASE_INTEGRATION_DEF_NAME` | `PTX_GPM_INBOUND_V2` → `GPM_INBOUND_V2` |
| `BASE_DATASET_NAME` | Customer dataset → SYSTEM base dataset |
| `BASE_REPORT_NAME` | Customer report → SYSTEM base report |
| `BASE_SEARCH_NAME` | Customer search → SYSTEM base search |
| `BASE_TEMPLATE_NAME` | Customer template → SYSTEM base template |
| `BASE_WINDOWDEF_NAME` | Customer windowdef → SYSTEM base windowdef |

If the tag is found, **redirect** the comparison and merge to use the named SYSTEM artifact as the base — do NOT treat it as a source-only artifact. The customer artifact extends the SYSTEM base; all SYSTEM 26.2 changes to the base must be preserved.

### Rule 5 — `__env_specific` bucket always Retain
Artifacts in the `__env_specific` bucket (`__env_specific/{ENV}/{CUSTOMER}/{category}/{artifact}`) are environment-specific configurations (DEV / UAT / PROD). They have no counterpart in SYSTEM and must **never be merged** — always **Retain** as-is.

### Rule 6 — business_process_policies requires manual DB handling
If a `business_process_policies` artifact has any change (decision is Merge or Retain), **a manual DB action is required**:
> ⚠ Delete the **previous entry** from the database BEFORE the upgrade is applied to the environment.

This cannot be automated — the engineer must perform this step manually after merge and before deployment.

### Rule 7 — DGS artifacts always Retain
`dgs/` category artifacts (Document Generation System) are **not migrated** through this tool. Decision is always **Retain** — they are kept as-is without comparison against SYSTEM.

---

## 4. 3-Way Code Merge (when baseline is available)

For code files (Java / JS / JSP), use baseline (24.4.11) as the common ancestor:

| SYSTEM vs Baseline | ALDI vs Baseline | Winner |
|---------------------|------------------|--------|
| Changed | Unchanged | **SYSTEM wins** (adopt upgrade) |
| Unchanged | Changed | **ALDI wins** (preserve customization) |
| Changed | Changed (same region) | **SYSTEM wins** (upgrade takes priority, flag for review) |
| Changed | Changed (different regions) | **Merge both** (combine changes) |
| Unchanged | Unchanged | Keep baseline |

### Special Code Rules
- **Imports**: union of SYSTEM + ALDI imports, then prune to only those actually used in the merged file.
- **No duplicate methods**: if a method with the same signature exists at class level in both, keep SYSTEM's version unless ALDI version has meaningful customization (different body).
- **Anonymous inner classes**: treat methods inside `new X() { ... }` as part of the outer expression, NOT as class-level duplicates.

---

## 5. JSON Merge Rules

### Entity-level Fields (top-level object)
- Start with SYSTEM 26.2 as base.
- Apply ALDI overrides recursively.
- ALDI value wins for scalar overrides of customization fields (e.g., SET_DESCRIPTION, SET_NAME if ALDI renamed).
- Preserve SYSTEM structural fields (SCHEMA_NAME, ENTITY_NAME, BASE_SET_ID) unless ALDI explicitly overrides for a reason.

### Arrays of Records
Arrays like `AVS_VALIDATION`, `AVS_FIELD_VALIDATION`, `AVS_REQUIRED_FIELDS`, `FIELDS`, `CODE_FIELDS`, `TABLE_HIER` are merged by **primary key**.

Primary keys by array type:
- `AVS_VALIDATION` → `VALIDATION_ID`
- `AVS_FIELD_VALIDATION` → `FIELD_NAME`
- `AVS_REQUIRED_FIELDS` → `FIELD_NAME`
- `FIELDS` → `FIELD_ID`
- `CODE_FIELDS` → `FIELD_NAME` (e.g., CH_CLASS_CODE, CH_IMPORTS, JS_CODE, AJAX_CODE)
- `TABLE_HIER` → `INTERNAL_ID`
- `COLUMN_DEFS` → `COLUMN_NAME`
- `MENU_ITEMS` → `MENU_ITEM_ID`

Merge rules per record:
- **Same key in both** → deep merge, ALDI wins on scalar conflicts.
- **SYSTEM-only** → preserve (base already has it).
- **ALDI-only** → append to merged array.

### Resequencing After Merge
- `ROW_SEQ` and `SET_VALIDATION_ID` must be **sequential integers starting from 1** after merge.
- Preserve ordering: SYSTEM records first (in their original order), then ALDI-only records appended.

### CODE_FIELDS (JS_CODE, AJAX_CODE, CH_CLASS_CODE, CH_IMPORTS, etc.)
Treat each CODE_FIELDS entry as a separate code file:
- Apply 3-way code merge rules.
- SYSTEM changed from baseline → SYSTEM wins.
- SYSTEM unchanged, ALDI changed → ALDI wins.
- Both changed → SYSTEM wins (flag for review).

---

## 6. _diff.json Regeneration

`_diff.json` is the runtime delta applied on top of SYSTEM 26.2 base. After producing the merged artifact, regenerate `_diff.json` by comparing merged output vs SYSTEM 26.2:

### Rules
- **Top-level scalar field differs** between merged and SYSTEM → emit `MOD_FIELDS` entry.
- **Record in merged but NOT in SYSTEM** (by primary key) → emit `NEW_RECORD` entry.
- **Record in SYSTEM but NOT in merged** → emit `DEL_RECORD` entry.
- **Record in both with same key but different values** → this should NOT happen if merge is correct (the merged already contains the SYSTEM version with ALDI overrides). If it does, skip — SYSTEM base already provides the record.
- **Record in both, identical** → skip (no delta needed).

### No MOD_FIELDS for Resequenced Records
- Do NOT emit MOD_FIELDS entries for SYSTEM records that only differ in ROW_SEQ / SET_VALIDATION_ID due to insertion of ALDI records. The runtime applies deltas on top of SYSTEM's original sequence.

---

## 7. Edge Cases

- **Anonymous Comparator / Runnable / Callback classes**: do NOT dedup methods inside these — they are nested scope.
- **Import-usage mismatch**: if merged code uses a class not imported, search ALDI & SYSTEM imports and add the correct one.
- **Empty files**: if ALDI file is empty and SYSTEM is not → Merge (take SYSTEM).
- **Conflict markers** (`<<<<<<<`, `=======`, `>>>>>>>`): never allowed in output. Treat presence as merge failure.
- **Trailing newline**: normalize to single trailing newline in output.
