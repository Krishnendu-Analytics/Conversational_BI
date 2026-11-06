# Operations

Operational notes for the currently shipped runtime. This is **not** a
completed production-hardening deliverable.

## Health endpoint

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Process is up; includes auth rate-limit / Redis status |

Response fields:

- `status`: `ok` or `degraded` (based on auth rate-limit readiness)
- `auth_rate_limits_ok`
- `auth_rate_limits_mode`
- `auth_rate_limits_redis`

There is **no** separate `/health/live` or `/health/ready` endpoint in the
current application. Production (`APP_ENV=production|prod`) still fails closed
at startup when Redis is required for distributed auth rate limits (see
`app/main.py` lifespan + `auth_rate_limit_status()`).

## File ingestion

File dataset upload remains **synchronous**. See `DATASETS.md`.

Async ingestion jobs, request-correlation middleware, and structured ops-event
frameworks are **not** shipped. Do not assume they exist from older drafts.

## Schema discovery jobs

Schema discovery uses the existing in-process `discovery_jobs` helper
(`ThreadPoolExecutor`, optional sync mode for tests). That is separate from
file-upload ingestion.
