"""Redis client helpers for caching, memory, pub/sub, and rate limits.

Cache keys follow the multi-tenant convention:
  schema:{tenant_id}:{connection_id}
  semantic:{tenant_id}
TTL defaults to 1 hour (see Settings.schema_cache_ttl_seconds).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from app.config import get_settings

logger = logging.getLogger(__name__)


class RedisClient:
    """Lazy Redis connection. Safe no-op when Redis is unavailable."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._failed = False

    def _connect(self) -> Any | None:
        if self._failed:
            return None
        if self._client is not None:
            return self._client

        settings = get_settings()
        url = (settings.redis_url or "").strip()
        if not url:
            self._failed = True
            return None

        try:
            import redis

            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            client.ping()
            self._client = client
            return self._client
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            logger.warning("Redis unavailable (%s); schema cache disabled", exc)
            self._failed = True
            self._client = None
            return None

    def get(self, key: str) -> str | None:
        client = self._connect()
        if client is None:
            return None
        try:
            return client.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis GET failed for %s: %s", key, exc)
            return None

    def setex(self, key: str, ttl_seconds: int, value: str) -> bool:
        client = self._connect()
        if client is None:
            return False
        try:
            client.setex(key, ttl_seconds, value)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis SETEX failed for %s: %s", key, exc)
            return False

    def delete(self, key: str) -> bool:
        client = self._connect()
        if client is None:
            return False
        try:
            client.delete(key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis DELETE failed for %s: %s", key, exc)
            return False

    def publish(self, channel: str, message: str) -> int:
        """Publish a message to a Redis pub/sub channel. Returns subscriber count."""
        client = self._connect()
        if client is None:
            return 0
        try:
            return int(client.publish(channel, message))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis PUBLISH failed for %s: %s", channel, exc)
            return 0

    def reset(self) -> None:
        """Drop cached connection (tests / reconnect)."""
        self._client = None
        self._failed = False


_redis = RedisClient()


def get_redis() -> RedisClient:
    return _redis


class QueryResultCache:
    """Rows of a read-only query, keyed by tenant, connection and exact SQL.

    Used for dashboard widgets so reopening a dashboard doesn't re-run every
    query on a (possibly remote) customer database. Rows are stored in
    their JSON-safe form, which is what the API returns anyway.
    """

    MAX_BYTES = 2_000_000

    @staticmethod
    def key(tenant_id: UUID | str, connection_id: UUID | str, sql: str) -> str:
        import hashlib

        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        return f"qres:v1:{tenant_id}:{connection_id}:{digest}"

    def get(self, tenant_id: UUID | str, connection_id: UUID | str, sql: str) -> list | None:
        raw = get_redis().get(self.key(tenant_id, connection_id, sql))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, list) else None

    def set(
        self,
        tenant_id: UUID | str,
        connection_id: UUID | str,
        sql: str,
        rows: list,
        *,
        ttl_seconds: int,
    ) -> bool:
        from app.serialize import json_safe

        if ttl_seconds <= 0:
            return False
        payload = json.dumps(json_safe(rows))
        if len(payload) > self.MAX_BYTES:
            return False
        return get_redis().setex(self.key(tenant_id, connection_id, sql), ttl_seconds, payload)


query_result_cache = QueryResultCache()


class SchemaCache:
    """Tenant-scoped schema metadata cache (T-43)."""

    @staticmethod
    def key(tenant_id: UUID | str, connection_id: UUID | str) -> str:
        return f"schema:{tenant_id}:{connection_id}"

    def get(self, tenant_id: UUID | str, connection_id: UUID | str) -> dict | None:
        raw = get_redis().get(self.key(tenant_id, connection_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or "tables" not in data:
            return None
        return data

    def set(
        self,
        tenant_id: UUID | str,
        connection_id: UUID | str,
        payload: dict,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        settings = get_settings()
        ttl = ttl_seconds if ttl_seconds is not None else settings.schema_cache_ttl_seconds
        body = dict(payload)
        body["connection_id"] = str(body.get("connection_id", connection_id))
        return get_redis().setex(
            self.key(tenant_id, connection_id),
            ttl,
            json.dumps(body, default=str),
        )

    def invalidate(self, tenant_id: UUID | str, connection_id: UUID | str) -> bool:
        return get_redis().delete(self.key(tenant_id, connection_id))


schema_cache = SchemaCache()


class SemanticCache:
    """Connection-scoped semantic catalog cache.

    Key: semantic:v2:{tenant_id}:{connection_id}. v2 = the agent catalog
    with rules; the semantic API used to cache a different shape under the
    unversioned key, so those entries are simply never read again.
    """

    @staticmethod
    def key(tenant_id: UUID | str, connection_id: UUID | str) -> str:
        return f"semantic:v2:{tenant_id}:{connection_id}"

    def get(self, tenant_id: UUID | str, connection_id: UUID | str) -> dict | None:
        raw = get_redis().get(self.key(tenant_id, connection_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if "metrics" not in data or "dimensions" not in data:
            return None
        return data

    def set(
        self,
        tenant_id: UUID | str,
        connection_id: UUID | str,
        payload: dict,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        settings = get_settings()
        ttl = ttl_seconds if ttl_seconds is not None else settings.schema_cache_ttl_seconds
        return get_redis().setex(
            self.key(tenant_id, connection_id),
            ttl,
            json.dumps(payload, default=str),
        )

    def invalidate(self, tenant_id: UUID | str, connection_id: UUID | str) -> bool:
        return get_redis().delete(self.key(tenant_id, connection_id))


semantic_cache = SemanticCache()


class ConversationMemory:
    """Store short-term conversation state in Redis."""

    DEFAULT_TTL_SECONDS = 1800

    @staticmethod
    def key(tenant_id: UUID | str, conversation_id: UUID | str) -> str:
        return f"conversation:{tenant_id}:{conversation_id}"

    def get(
        self,
        tenant_id: UUID | str,
        conversation_id: UUID | str,
    ) -> dict[str, Any] | None:
        raw = get_redis().get(self.key(tenant_id, conversation_id))

        if not raw:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        return data

    def set(
        self,
        tenant_id: UUID | str,
        conversation_id: UUID | str,
        *,
        last_sql: str | None = None,
        filters: list[Any] | None = None,
        tables: list[str] | None = None,
        metrics: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        dashboard: dict[str, Any] | None = None,
        analysis_state: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> bool:
        settings = get_settings()

        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else getattr(
                settings,
                "conversation_memory_ttl_seconds",
                self.DEFAULT_TTL_SECONDS,
            )
        )

        payload = {
            "last_sql": last_sql,
            "filters": filters or [],
            "tables": tables or [],
            "metrics": metrics or [],
            "messages": messages or [],
            "dashboard": dashboard,
            "analysis_state": analysis_state,
        }

        return bool(
            get_redis().setex(
                self.key(tenant_id, conversation_id),
                ttl,
                json.dumps(payload, default=str),
            )
        )

    def set_state(
        self,
        tenant_id: UUID | str,
        conversation_id: UUID | str,
        state: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Replace the full conversation state blob."""
        settings = get_settings()
        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else getattr(
                settings,
                "conversation_memory_ttl_seconds",
                self.DEFAULT_TTL_SECONDS,
            )
        )
        return bool(
            get_redis().setex(
                self.key(tenant_id, conversation_id),
                ttl,
                json.dumps(state, default=str),
            )
        )

    def update(
        self,
        tenant_id: UUID | str,
        conversation_id: UUID | str,
        *,
        last_sql: str | None = None,
        filters: list[Any] | None = None,
        tables: list[str] | None = None,
        metrics: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        dashboard: dict[str, Any] | None = None,
        analysis_state: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> bool:
        current = self.get(tenant_id, conversation_id) or {}

        return self.set(
            tenant_id,
            conversation_id,
            last_sql=(
                last_sql
                if last_sql is not None
                else current.get("last_sql")
            ),
            filters=(
                filters
                if filters is not None
                else current.get("filters", [])
            ),
            tables=(
                tables
                if tables is not None
                else current.get("tables", [])
            ),
            metrics=(
                metrics
                if metrics is not None
                else current.get("metrics", [])
            ),
            messages=(
                messages
                if messages is not None
                else current.get("messages", [])
            ),
            dashboard=(
                dashboard
                if dashboard is not None
                else current.get("dashboard")
            ),
            analysis_state=(
                analysis_state
                if analysis_state is not None
                else current.get("analysis_state")
            ),
            ttl_seconds=ttl_seconds,
        )

    def invalidate(self, tenant_id: UUID | str, conversation_id: UUID | str) -> bool:
        return bool(
            get_redis().delete(
                self.key(tenant_id, conversation_id)
            )
        )

class RedisPublisher:
    """Publish LangGraph progress events onto Redis pub/sub (T-58).

    The chat WebSocket layer will subscribe to the same channel and forward
    events to the browser. Publishing is best-effort: if Redis is down the
    workflow still completes.
    """

    def channel_for(
        self,
        tenant_id: str,
        *,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        from app.agent.events import progress_channel

        return progress_channel(
            tenant_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )

    def publish_event(self, event: Any) -> bool:
        """Serialize a ProgressEvent and publish it.

        ``event`` may be a Pydantic model (``model_dump``) or a plain dict.
        Returns True when Redis accepted the publish call (subscribers may be 0).
        """
        if hasattr(event, "model_dump"):
            payload = event.model_dump(mode="json")
        elif isinstance(event, dict):
            payload = event
        else:
            raise TypeError(f"Unsupported progress event: {type(event)!r}")

        tenant_id = str(payload.get("tenant_id") or "")
        if not tenant_id:
            logger.warning("Skipping progress publish: missing tenant_id")
            return False

        conversation_id = payload.get("conversation_id")
        user_id = payload.get("user_id")
        channel = self.channel_for(
            tenant_id,
            conversation_id=str(conversation_id) if conversation_id else None,
            user_id=str(user_id) if user_id else None,
        )
        return self.publish_raw(channel, payload)

    def publish_raw(self, channel: str, payload: dict[str, Any]) -> bool:
        """Publish a dict payload. False only when Redis is unavailable."""
        client = get_redis()._connect()
        if client is None:
            return False
        body = json.dumps(payload, default=str)
        get_redis().publish(channel, body)
        return True


progress_publisher = RedisPublisher()


class AuthRateLimitUnavailableError(RuntimeError):
    """Raised when production auth rate limiting requires Redis but it is unavailable."""


class RateLimiter:
    """Bounded attempt counter with Redis (atomic) and dev-only in-process fallback."""

    _memory: dict[str, tuple[int, float]] = {}

    def redis_available(self) -> bool:
        return get_redis()._connect() is not None

    def ensure_distributed_backend(self) -> None:
        """Production must use Redis for auth limits — fail closed otherwise."""
        from app.config import is_production_env

        if is_production_env() and not self.redis_available():
            raise AuthRateLimitUnavailableError(
                "Auth rate limiting requires Redis in production"
            )

    def record_failure(
        self,
        key: str,
        *,
        window_seconds: int,
        max_failures: int,
    ) -> tuple[int, bool]:
        """Atomically increment failure counter; return (count, exceeded)."""
        client = get_redis()._connect()
        if client is not None:
            try:
                count = int(client.incr(key))
                if count == 1:
                    client.expire(key, window_seconds)
                return count, count > max_failures
            except Exception:
                pass

        from app.config import is_production_env

        if is_production_env():
            raise AuthRateLimitUnavailableError(
                "Auth rate limiting requires Redis in production"
            )
        count = self._memory_increment(key, window_seconds)
        return count, count > max_failures

    def is_over_limit(self, key: str, max_failures: int) -> bool:
        """Pre-check before auth; record_failure is the authoritative atomic gate."""
        self.ensure_distributed_backend()
        client = get_redis()._connect()
        if client is not None:
            try:
                raw = client.get(key)
                return int(raw or 0) >= max_failures
            except Exception:
                pass

        from app.config import is_production_env

        if is_production_env():
            raise AuthRateLimitUnavailableError(
                "Auth rate limiting requires Redis in production"
            )
        count, _start = self._memory.get(key, (0, 0.0))
        return count >= max_failures

    def increment(self, key: str, window_seconds: int) -> int:
        """Legacy increment helper (non-auth callers). Prefer record_failure for login."""
        count, _exceeded = self.record_failure(
            key, window_seconds=window_seconds, max_failures=10**9
        )
        return count

    def get_count(self, key: str) -> int:
        client = get_redis()._connect()
        if client is not None:
            try:
                raw = client.get(key)
                return int(raw) if raw else 0
            except Exception:
                pass
        count, _start = self._memory.get(key, (0, 0.0))
        return count

    def _memory_increment(self, key: str, window_seconds: int) -> int:
        import time

        now = time.time()
        count, start = self._memory.get(key, (0, now))
        if now - start >= window_seconds:
            count = 0
            start = now
        count += 1
        self._memory[key] = (count, start)
        return count


def auth_rate_limit_status() -> dict[str, str | bool]:
    """Health/startup signal for distributed auth rate limiting."""
    from app.config import is_production_env

    available = rate_limiter.redis_available()
    production = is_production_env()
    if production and not available:
        return {
            "ok": False,
            "mode": "production",
            "redis_available": False,
            "message": "Redis required for auth rate limiting in production",
        }
    return {
        "ok": True,
        "mode": "production" if production else "development",
        "redis_available": available,
        "message": "distributed" if available else "in-process-fallback",
    }


rate_limiter = RateLimiter()
