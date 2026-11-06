# File Datasets (CSV / Excel)

## Architecture

```
Upload (CSV/Excel)
  → validate (size, type, rows/cols)
  → tenant-scoped file storage
  → Connection (db_type=csv|excel) + Dataset row(s)
  → parse → type inference → DuckDB (tenant/connection scoped)
  → schema discovery (schema_tables + Redis cache)
  → semantic suggestions (deterministic heuristics)
  → QueryPlan → validate → compile → SQLSafetyPipeline → DuckDBFileProvider
  → facts → grounded narration / dashboards
```

SQL connections (`postgres`, `mysql`, `mssql`) are unchanged. File-backed
connections reuse the same `connection_id` scope, semantic layer, QueryPlan
compiler, resource limits, and dashboard/chat paths.

## Supported types

| Type   | Extensions        | Notes                          |
|--------|-------------------|--------------------------------|
| CSV    | `.csv`            | UTF-8 / BOM, delimiter detect  |
| Excel  | `.xlsx`, `.xlsm`  | No macros; formulas as values  |

## Limits (environment variables)

| Variable                      | Default   |
|-------------------------------|-----------|
| `FILE_STORAGE_ROOT`           | `./data/file_datasets` |
| `FILE_MAX_SIZE_BYTES`         | 50 MiB    |
| `FILE_MAX_ROWS`               | 500,000   |
| `FILE_MAX_COLUMNS`            | 256       |
| `FILE_MAX_WORKSHEETS`         | 20        |
| `FILE_INGEST_TIMEOUT_SECONDS` | 120       |

Query execution uses the same P1-2 limits as SQL (`QUERY_MAX_ROWS`, timeouts, etc.).

## Security

- All operations are tenant-scoped; never trust client `tenant_id`.
- Internal table names use `t_{dataset_uuid}` — never the original filename.
- Uploaded files are stored under `{FILE_STORAGE_ROOT}/{tenant_id}/{connection_id}/`.
- SQL passes through `SQLSafetyPipeline` with the DuckDB dialect.
- Excel is read with `openpyxl` (`read_only`, `data_only`); macros are not executed.

## API

- `POST /api/v1/datasets/upload` — multipart: `name`, `file`, optional `sheets`
- `GET /api/v1/datasets` — list datasets (`?connection_id=`)
- `GET /api/v1/datasets/{id}` — dataset details
- `DELETE /api/v1/datasets/{id}` — remove dataset (and connection if last)

## Multi-dataset connections

Excel workbooks may produce multiple datasets (one per usable sheet). When more
than one dataset exists on a connection, the agent should clarify which dataset
to use unless context identifies one unambiguously.

## Ingestion model (P1-5)

File ingestion runs **synchronously** inside `POST /api/v1/datasets/upload`.
This reuses the same request lifecycle as schema discovery in test/sync mode
rather than introducing a separate job queue.

**Why synchronous is acceptable for this phase:**

- Configured limits cap files at 50 MiB / 500k rows by default
- Parsing uses bounded row/column counts with early rejection
- Typical CSV/Excel files complete within `FILE_INGEST_TIMEOUT_SECONDS` (120s)
- Upload returns only after ingest + schema discovery + heuristic semantic suggestions finish

**Production note:** For larger files or multi-tenant load, migrate ingestion to
the existing `discovery_jobs` enqueue pattern (Option A in P1-6+) without changing
the downstream QueryPlan / DuckDB execution path.

## Dataset versioning / re-upload

**Deferred.** Re-uploading a file to an existing connection is not supported in P1-5.
Each upload creates a new `Connection` + `Dataset` pair. Confirmed semantic
definitions on prior connections are not mutated. Full version/re-upload semantics
remain a follow-up item.

## Workflow integration

- `AnalysisState.dataset_id` tracks the active dataset across turns
- `resolve_active_dataset()` is wired into `SQLGenerator`, `QueryExecutor`, and
  `DashboardBuilder` via `prepare_file_dataset_context()`
- Multi-dataset connections trigger `pending_dataset_clarification` (same UX as
  query clarification)
- Dataset switching phrases (`use …`, `switch to …`) reset incompatible query context
