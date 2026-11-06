"""Central query resource policy, concurrency, and bounded execution."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator
from uuid import UUID

from app.config import get_settings, is_production_env
from app.serialize import json_safe

logger = logging.getLogger(__name__)


class QueryCategory(str, Enum):
    ANALYTICS = "analytics"
    DASHBOARD = "dashboard"
    PROFILING = "profiling"
    METADATA = "metadata"


@dataclass(frozen=True)
class QueryExecutionLimits:
    timeout_seconds: int
    max_rows: int
    max_payload_bytes: int


class QueryExecutionError(Exception):
    """Controlled application error for query execution."""

    code: str = "query_execution_error"
    user_message: str = "The query could not be completed."

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        if user_message is not None:
            self.user_message = user_message


class QueryTimeoutError(QueryExecutionError):
    code = "query_timeout"

    def __init__(self, message: str = "Query timed out") -> None:
        super().__init__(
            message,
            user_message=(
                "The query took too long to complete. "
                "Try narrowing the time range or asking for an aggregated result."
            ),
        )


class ResultTooLargeError(QueryExecutionError):
    code = "result_too_large"

    def __init__(self, message: str = "Result too large") -> None:
        super().__init__(
            message,
            user_message=(
                "The query returned more data than allowed. "
                "Try adding filters, grouping, or asking for a summary."
            ),
        )


class ConcurrencyLimitError(QueryExecutionError):
    code = "concurrency_limit_exceeded"

    def __init__(self, message: str = "Concurrency limit exceeded") -> None:
        super().__init__(
            message,
            user_message=(
                "Too many queries are running right now. "
                "Please wait a moment and try again."
            ),
        )


class ResourcePolicyError(QueryExecutionError):
    code = "resource_policy_violation"

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(
            message,
            user_message=user_message
            or "The query could not be run safely with the current request.",
        )


NON_RETRYABLE_EXECUTION_ERRORS: tuple[type[BaseException], ...] = (
    QueryTimeoutError,
    ResultTooLargeError,
    ConcurrencyLimitError,
    ResourcePolicyError,
)


def is_non_retryable_execution_error(exc: BaseException) -> bool:
    return isinstance(exc, NON_RETRYABLE_EXECUTION_ERRORS)


def user_facing_execution_error(exc: BaseException) -> str:
    if isinstance(exc, QueryExecutionError):
        return exc.user_message
    name = type(exc).__name__
    if "timeout" in name.lower() or "timed out" in str(exc).lower():
        return QueryTimeoutError().user_message
    return "The query could not be completed. Try a simpler or more aggregated question."


def sql_fingerprint(sql: str) -> str:
    normalized = " ".join((sql or "").split()).lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def limits_for_category(category: QueryCategory) -> QueryExecutionLimits:
    settings = get_settings()
    if category == QueryCategory.PROFILING:
        return QueryExecutionLimits(
            timeout_seconds=settings.profiling_timeout_seconds,
            max_rows=settings.profiling_max_rows,
            max_payload_bytes=settings.profiling_max_payload_bytes,
        )
    if category == QueryCategory.METADATA:
        return QueryExecutionLimits(
            timeout_seconds=settings.metadata_timeout_seconds,
            max_rows=settings.metadata_max_rows,
            max_payload_bytes=settings.metadata_max_payload_bytes,
        )
    if category == QueryCategory.DASHBOARD:
        return QueryExecutionLimits(
            timeout_seconds=settings.dashboard_query_timeout_seconds,
            max_rows=settings.dashboard_query_max_rows,
            max_payload_bytes=settings.dashboard_query_max_payload_bytes,
        )
    return QueryExecutionLimits(
        timeout_seconds=settings.query_timeout_seconds,
        max_rows=settings.query_max_rows,
        max_payload_bytes=settings.query_max_payload_bytes,
    )


def _estimate_row_bytes(row: dict[str, Any]) -> int:
    try:
        return len(json.dumps(row, default=str, separators=(",", ":")))
    except Exception:  # noqa: BLE001
        return 256


def fetch_bounded_rows(
    cur: Any,
    *,
    max_rows: int,
    max_payload_bytes: int,
    row_factory: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch rows without loading an unbounded result into memory."""
    rows: list[dict[str, Any]] = []
    payload = 0
    batch_size = min(500, max(1, max_rows))

    while len(rows) < max_rows:
        batch = cur.fetchmany(batch_size)
        if not batch:
            break
        for raw in batch:
            if row_factory is not None:
                row = row_factory(raw)
            elif isinstance(raw, dict):
                row = raw
            else:
                row = dict(raw)
            safe = json_safe(row)
            row_bytes = _estimate_row_bytes(safe)
            if payload + row_bytes > max_payload_bytes:
                raise ResultTooLargeError(
                    f"Result payload exceeds {max_payload_bytes} bytes"
                )
            rows.append(safe)
            payload += row_bytes
            if len(rows) >= max_rows:
                extra = cur.fetchone()
                if extra is not None:
                    raise ResultTooLargeError(
                        f"Result exceeds maximum of {max_rows} rows"
                    )
                break
    return rows


class _LocalConcurrencyGate:
    """Dev/test in-process concurrency slots."""

    _locks: dict[str, threading.Lock] = {}
    _counts: dict[str, int] = {}
    _gate_lock = threading.Lock()

    @classmethod
    def acquire(cls, key: str, limit: int) -> bool:
        with cls._gate_lock:
            lock = cls._locks.setdefault(key, threading.Lock())
        with lock:
            count = cls._counts.get(key, 0)
            if count >= limit:
                return False
            cls._counts[key] = count + 1
            return True

    @classmethod
    def release(cls, key: str) -> None:
        with cls._gate_lock:
            lock = cls._locks.setdefault(key, threading.Lock())
        with lock:
            count = cls._counts.get(key, 0)
            cls._counts[key] = max(0, count - 1)


class QueryConcurrencyGate:
    """Bounded concurrent query slots (Redis in production when available)."""

    _ACQUIRE_LUA = """
local current = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if current > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return 1
"""

    _RELEASE_LUA = """
local current = redis.call('DECR', KEYS[1])
if current < 0 then
  redis.call('SET', KEYS[1], 0)
end
return 1
"""

    def __init__(self) -> None:
        self._warned_local_fallback = False

    def _redis_client(self) -> Any | None:
        from app.redis_client import get_redis

        return get_redis()._connect()

    def acquire(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        category: QueryCategory,
    ) -> tuple[bool, list[str]]:
        settings = get_settings()
        keys: list[str] = []
        limits: list[tuple[str, int]] = [
            (f"query:conc:global:{category.value}", settings.query_global_concurrency),
            (f"query:conc:tenant:{tenant_id}:{category.value}", settings.query_tenant_concurrency),
        ]
        if user_id:
            limits.append(
                (
                    f"query:conc:user:{tenant_id}:{user_id}:{category.value}",
                    settings.query_user_concurrency,
                )
            )

        client = self._redis_client()
        if client is not None:
            acquired: list[str] = []
            try:
                for key, limit in limits:
                    ok = int(
                        client.eval(
                            self._ACQUIRE_LUA,
                            1,
                            key,
                            str(limit),
                            "300",
                        )
                    )
                    if not ok:
                        for prev in acquired:
                            client.eval(self._RELEASE_LUA, 1, prev)
                        return False, keys
                    acquired.append(key)
                return True, acquired
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis concurrency acquire failed: %s", type(exc).__name__)
                for prev in acquired:
                    try:
                        client.eval(self._RELEASE_LUA, 1, prev)
                    except Exception:  # noqa: BLE001
                        pass

        if is_production_env():
            if not self._warned_local_fallback:
                logger.warning(
                    "Query concurrency using in-process fallback; "
                    "Redis recommended for distributed deployments"
                )
                self._warned_local_fallback = True

        acquired_keys: list[str] = []
        for key, limit in limits:
            if not _LocalConcurrencyGate.acquire(key, limit):
                for prev in acquired_keys:
                    _LocalConcurrencyGate.release(prev)
                return False, keys
            acquired_keys.append(key)
        return True, acquired_keys

    def release(self, keys: list[str]) -> None:
        if not keys:
            return
        client = self._redis_client()
        if client is not None:
            for key in keys:
                try:
                    client.eval(self._RELEASE_LUA, 1, key)
                except Exception:  # noqa: BLE001
                    pass
            return
        for key in keys:
            _LocalConcurrencyGate.release(key)


query_concurrency_gate = QueryConcurrencyGate()


@contextmanager
def query_execution_slot(
    *,
    tenant_id: str,
    user_id: str | None = None,
    category: QueryCategory = QueryCategory.ANALYTICS,
) -> Iterator[None]:
    acquired, keys = query_concurrency_gate.acquire(
        tenant_id=tenant_id,
        user_id=user_id,
        category=category,
    )
    if not acquired:
        logger.info(
            "query_concurrency_rejected tenant=%s user=%s category=%s",
            tenant_id[:8] if tenant_id else "",
            (user_id or "")[:8],
            category.value,
        )
        raise ConcurrencyLimitError("Concurrent query limit exceeded")
    try:
        yield
    finally:
        query_concurrency_gate.release(keys)


def log_query_event(
    event: str,
    *,
    tenant_id: str = "",
    connection_id: str = "",
    conversation_id: str | None = None,
    dashboard_id: str | None = None,
    category: str = "",
    sql_hash: str = "",
    duration_ms: float | None = None,
    row_count: int | None = None,
    result_bytes: int | None = None,
    outcome: str = "",
) -> None:
    logger.info(
        "%s tenant=%s connection=%s conversation=%s dashboard=%s "
        "category=%s sql_hash=%s duration_ms=%s row_count=%s result_bytes=%s outcome=%s",
        event,
        tenant_id[:8] if tenant_id else "",
        connection_id[:8] if connection_id else "",
        (conversation_id or "")[:8],
        (dashboard_id or "")[:8],
        category,
        sql_hash,
        int(duration_ms) if duration_ms is not None else "",
        row_count if row_count is not None else "",
        result_bytes if result_bytes is not None else "",
        outcome,
    )


def execute_readonly_with_limits(
    provider: Any,
    sql: str,
    *,
    limits: QueryExecutionLimits,
    db_type: str,
) -> list[dict[str, Any]]:
    """Invoke provider read-only execution with unified limits."""
    from app.tools.sql_safety import assert_read_only_sql

    assert_read_only_sql(sql, db_type=db_type)
    return provider.execute_read_only_query(
        sql,
        limits=limits,
        db_type=db_type,
    )


def wrap_provider_timeout_error(exc: Exception, *, limits: QueryExecutionLimits) -> Exception:
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    if (
        "timeout" in text
        or "timeout" in name
        or "timed out" in text
        or "statement_timeout" in text
        or "max_execution_time" in text
        or "query governor" in text
    ):
        return QueryTimeoutError(f"Query exceeded {limits.timeout_seconds}s: {type(exc).__name__}")
    if isinstance(exc, QueryExecutionError):
        return exc
    return exc
