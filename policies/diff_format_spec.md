# _diff.json Format Specification

`_diff.json` files carry **runtime deltas** applied on top of the SYSTEM 26.2 base at load time. They are NOT a generic git-style diff — they use a domain-specific schema the runtime understands.

---

## Top-Level Shape

```
{ARTIFACT_ID}:[
   <entry-1>,
   <entry-2>,
   ...
]
```

- The outer key is the artifact's SET_ID / ID (matches the base artifact's `SET_ID` or unique identifier).
- The value is an **array** of delta entries.

---

## Entry Types

### 1. MOD_FIELDS — Modify top-level fields on the base entity

Use when the artifact-level (parent record) has scalar field changes vs SYSTEM base.

```json
{
   "fields": [
      { "cn": "FIELD_NAME", "cv": "new value" },
      { "cn": "ANOTHER_FIELD", "cv": "another value" }
   ],
   "key": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"TABLE_NAME\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"ARTIFACT_ID\"}]}",
   "type": "MOD_FIELDS"
}
```

Rules:
- `cn` = column name.
- `cv` = column value (always a string in the delta even if underlying type is different).
- `key` is a JSON-escaped string describing the target row.
- Always emit a `BASE_SET_ID` field set to the artifact ID if the merged artifact derives from SYSTEM base.

---

### 2. NEW_RECORD — Insert a new child record

Use when merged output has a child record (in an array like AVS_VALIDATION) that does NOT exist in SYSTEM base.

```json
{
   "parentKey": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"PARENT_TABLE\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"ARTIFACT_ID\"}]}",
   "record": "{\"FIELD1\":\"value1\",\"FIELD2\":\"value2\",...}",
   "tableName": "CHILD_TABLE_NAME",
   "type": "NEW_RECORD"
}
```

Rules:
- `parentKey` links the new record to the parent artifact.
- `record` is a JSON-escaped **string** containing the full record's field-value map.
- `tableName` names the child table (e.g., AVS_VALIDATION, AVS_FIELD_VALIDATION).
- Preserve `REFERENCED_COLUMNS` as `[]` if absent.

---

### 3. DEL_RECORD — Delete an existing SYSTEM child record

Use when ALDI intentionally removed a SYSTEM base record.

```json
{
   "key": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"CHILD_TABLE_NAME\",\"m_KeyValues\":[{\"m_KeyName\":\"PK_COLUMN\",\"m_KeyValue\":\"PK_VALUE\"}]}",
   "type": "DEL_RECORD"
}
```

---

## Examples

### Example A — Simple validationset

Artifact: `IMPL.Trans.BROKER_PACKET.PostValidationSet` (ALDI adds 2 new validations on top of SYSTEM base)

```json
IMPL.Trans.BROKER_PACKET.PostValidationSet:[
   {
      "fields": [{ "cn": "BASE_SET_ID", "cv": "IMPL.Trans.BROKER_PACKET.PostValidationSet" }],
      "key": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"ATOMIC_VALIDATION_SET\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"IMPL.Trans.BROKER_PACKET.PostValidationSet\"}]}",
      "type": "MOD_FIELDS"
   },
   {
      "parentKey": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"ATOMIC_VALIDATION_SET\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"IMPL.Trans.BROKER_PACKET.PostValidationSet\"}]}",
      "record": "{\"REFERENCED_COLUMNS\":[],\"ENABLED\":\"Y\",\"VALIDATION_ID\":\"aldi.bp.reporting.code\",\"ROW_SEQ\":\"1\",\"SET_VALIDATION_ID\":\"1\"}",
      "tableName": "AVS_VALIDATION",
      "type": "NEW_RECORD"
   },
   {
      "parentKey": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"ATOMIC_VALIDATION_SET\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"IMPL.Trans.BROKER_PACKET.PostValidationSet\"}]}",
      "record": "{\"REFERENCED_COLUMNS\":[],\"ENABLED\":\"Y\",\"VALIDATION_ID\":\"aldi.bp.destination.arrivalDate\",\"ROW_SEQ\":\"2\",\"SET_VALIDATION_ID\":\"2\"}",
      "tableName": "AVS_VALIDATION",
      "type": "NEW_RECORD"
   }
]
```

Notes:
- ROW_SEQ for the NEW_RECORDs starts from 1 **within the ALDI additions**; the runtime stitches them after SYSTEM's existing records.
- Only MOD_FIELDS for entity-level field changes, never for existing SYSTEM records that merely got resequenced.

---

### Example B — validationset with MOD_FIELDS + AVS_FIELD_VALIDATION + AVS_REQUIRED_FIELDS + AVS_VALIDATION

Artifact: `trade.tx.pre_customs_entry.validationset`

```json
trade.tx.pre_customs_entry.validationset:[
   {
      "fields": [
         { "cn": "SCHEMA_NAME", "cv": "TradeSchema" },
         { "cn": "BASE_SET_ID", "cv": "trade.tx.pre_customs_entry.validationset" },
         { "cn": "ENTITY_NAME", "cv": "MDI_TX" }
      ],
      "key": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"ATOMIC_VALIDATION_SET\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"trade.tx.pre_customs_entry.validationset\"}]}",
      "type": "MOD_FIELDS"
   },
   {
      "parentKey": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"ATOMIC_VALIDATION_SET\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"trade.tx.pre_customs_entry.validationset\"}]}",
      "record": "{\"ONLY_ONE_QUALIFIER_VALUE\":\"N\",\"ONLY_QUALIFIER_VALUES_ALLOWED\":\"Y\",\"QUALIFIER_VALUES\":\"REVIEWED\",\"FIELD_ID\":\"1\",\"IS_REQUIRED\":\"Y\",\"ALL_QUALIFIER_VALUES_REQUIRED\":\"N\",\"REFERENCED_COLUMNS\":[],\"QUALIFIER_COLUMN_NAME\":\"MDI_TX.SOURCE_TX_STATE\",\"ENABLED\":\"Y\",\"FIELD_NAME\":\"MDI_TX.SOURCE_TX_STATE\"}",
      "tableName": "AVS_FIELD_VALIDATION",
      "type": "NEW_RECORD"
   },
   {
      "parentKey": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"ATOMIC_VALIDATION_SET\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"trade.tx.pre_customs_entry.validationset\"}]}",
      "record": "{\"REFERENCED_COLUMNS\":[],\"FIELD_NAME\":\"MDI_TX.SOURCE_TX_STATE\"}",
      "tableName": "AVS_REQUIRED_FIELDS",
      "type": "NEW_RECORD"
   },
   {
      "parentKey": "{\"m_Schema\":\"DefaultSchema\",\"m_TableName\":\"ATOMIC_VALIDATION_SET\",\"m_KeyValues\":[{\"m_KeyName\":\"SET_ID\",\"m_KeyValue\":\"trade.tx.pre_customs_entry.validationset\"}]}",
      "record": "{\"REFERENCED_COLUMNS\":[],\"ENABLED\":\"Y\",\"VALIDATION_ID\":\"ALDI.PCE.Product.Description\",\"ROW_SEQ\":\"58\",\"SET_VALIDATION_ID\":\"58\"}",
      "tableName": "AVS_VALIDATION",
      "type": "NEW_RECORD"
   }
]
```

Notes:
- ROW_SEQ `58` here indicates the ALDI record is appended after SYSTEM's 57 existing entries.
- The schema override `SCHEMA_NAME: TradeSchema` lands in MOD_FIELDS because the base artifact may live in DefaultSchema.

---

## Generation Algorithm (Claude follows this)

1. Parse merged JSON and SYSTEM base JSON.
2. Compare top-level scalar fields:
   - Differs → MOD_FIELDS entry with all differing fields (always include BASE_SET_ID).
3. For each array field with a known primary key:
   - Merged record NOT in SYSTEM (by PK) → NEW_RECORD
   - SYSTEM record NOT in merged (by PK) → DEL_RECORD
   - Both present, identical → skip
   - Both present, differ → skip (SYSTEM base already provides; MOD_FIELDS on child records is not used for resequencing)
4. Emit entries in stable order: MOD_FIELDS first, then NEW_RECORD by tableName then PK, then DEL_RECORD.
5. Preserve JSON-escape quoting in `key`, `parentKey`, and `record` string fields exactly.
