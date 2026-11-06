# Query execution resource limits

Environment-driven limits for customer database queries. All analytical SQL flows through
`run_readonly_query()` in `app/tools/customer_db.py`, which applies concurrency gates,
provider timeouts, and bounded result retrieval.

## Categories

| Category | Used for | Default timeout | Default max rows |
|----------|----------|-----------------|------------------|
| `analytics` | Chat / BIWorkflow SQL | 15s | 10,000 |
| `dashboard` | Dashboard widget refresh | 15s | 10,000 |
| `profiling` | Schema profiling samples/stats | 10s | 100 |
| `metadata` | Reserved for internal metadata tier | 10s | 5,000 |

## Settings

### Conversational analytics

- `QUERY_TIMEOUT_SECONDS` (default `15`)
- `QUERY_MAX_ROWS` (default `10000`)
- `QUERY_MAX_PAYLOAD_BYTES` (default `5000000`)

### Concurrency

- `QUERY_GLOBAL_CONCURRENCY` (default `32`)
- `QUERY_TENANT_CONCURRENCY` (default `8`)
- `QUERY_USER_CONCURRENCY` (default `4`)

Redis is used for distributed concurrency when available. Production auth already requires Redis; query concurrency prefers Redis and falls back to in-process slots in development.

### Dashboard

- `DASHBOARD_QUERY_TIMEOUT_SECONDS` (default `15`)
- `DASHBOARD_QUERY_MAX_ROWS` (default `10000`)
- `DASHBOARD_QUERY_MAX_PAYLOAD_BYTES` (default `5000000`)
- `DASHBOARD_MAX_WIDGETS_PER_REFRESH` (default `80`) — widgets beyond it are not re-queried but still shown with their last results
- `DASHBOARD_WIDGET_MAX_CONCURRENCY` (default `4`)
- `DASHBOARD_RESULT_CACHE_SECONDS` (default `300`) — widget rows reused from Redis when a dashboard is opened; Refresh always re-queries; `0` disables

### Schema profiling

- `PROFILING_TIMEOUT_SECONDS` (default `10`)
- `PROFILING_MAX_ROWS` (default `100`)
- `PROFILING_MAX_PAYLOAD_BYTES` (default `500000`)
- `PROFILING_MAX_QUERIES_PER_TABLE` (default `50`)

### Metadata (introspection tier)

- `METADATA_TIMEOUT_SECONDS` (default `10`)
- `METADATA_MAX_ROWS` (default `5000`)
- `METADATA_MAX_PAYLOAD_BYTES` (default `2000000`)

## Provider timeouts

- **PostgreSQL:** `SET statement_timeout` (milliseconds)
- **MySQL:** `SET SESSION MAX_EXECUTION_TIME` when supported
- **SQL Server:** connection `query_timeout` + `SET LOCK_TIMEOUT`

## Errors

Resource failures return controlled application errors (`QueryTimeoutError`, `ResultTooLargeError`, `ConcurrencyLimitError`) with user-safe messages. These errors are **not retried** by SQL repair logic.

## Observability

Structured log events (no raw SQL or credentials):

- `query_start`, `query_complete`, `query_timeout`, `query_resource_limit`, `query_failed`, `query_concurrency_rejected`

Fields: tenant_id (prefix), connection_id (prefix), conversation_id, dashboard_id, category, sql_hash, duration_ms, row_count, result_bytes, outcome.
