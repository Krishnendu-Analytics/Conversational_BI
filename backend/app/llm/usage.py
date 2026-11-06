"""Per-user LLM usage tracking (llm_usage).

Who is calling is carried in a context variable, so the ~30 LLM call sites
need no extra arguments: an HTTP middleware sets it from the access token,
the chat websocket sets it after auth, and asyncio.to_thread /
run_in_threadpool copy it into worker threads. Plain ThreadPoolExecutor does
not, so code that fans LLM work out to a pool uses ContextThreadPoolExecutor.

What the tokens were spent on (chat, schema_discovery, semantic_model, …) is
a second context variable: the middleware derives it from the route, the
chat websocket and background jobs set their own.

Each call upserts its (tenant, user, 5-minute bucket, model, feature) row on a background writer
thread, so tracking never adds a platform-DB round trip to an answer and a
failed write never fails the request.
"""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator
from uuid import UUID, uuid4

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageOwner:
    tenant_id: UUID
    user_id: UUID


_owner: contextvars.ContextVar[UsageOwner | None] = contextvars.ContextVar(
    "llm_usage_owner", default=None
)


# What the tokens were spent on — a separate row per feature per day.
_feature: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_usage_feature", default="other"
)

# REST route prefix → feature; the chat websocket and background jobs set
# theirs explicitly.
_PATH_FEATURES: tuple[tuple[str, str], ...] = (
    ("/api/v1/connections", "schema_discovery"),
    ("/api/v1/semantic", "semantic_model"),
    ("/api/v1/dashboards", "dashboards"),
    ("/api/v1/documents", "documents"),
    ("/api/v1/datasets", "datasets"),
    ("/api/v1/conversations", "chat"),
    ("/api/v1/search", "search"),
)


def feature_for_path(path: str) -> str:
    for prefix, feature in _PATH_FEATURES:
        if path.startswith(prefix):
            return feature
    return "other"


def set_usage_feature(feature: str) -> contextvars.Token:
    return _feature.set(feature)


def current_usage_feature() -> str:
    return _feature.get()


@contextmanager
def usage_feature(feature: str) -> Iterator[None]:
    token = _feature.set(feature)
    try:
        yield
    finally:
        _feature.reset(token)


def set_usage_owner(tenant_id: UUID | str, user_id: UUID | str) -> contextvars.Token:
    return _owner.set(UsageOwner(UUID(str(tenant_id)), UUID(str(user_id))))


def reset_usage_owner(token: contextvars.Token) -> None:
    _owner.reset(token)


def current_usage_owner() -> UsageOwner | None:
    return _owner.get()


@contextmanager
def usage_owner(tenant_id: UUID | str, user_id: UUID | str) -> Iterator[None]:
    token = set_usage_owner(tenant_id, user_id)
    try:
        yield
    finally:
        reset_usage_owner(token)


class UsageOwnerMiddleware:
    """ASGI middleware: attribute LLM calls made while serving an HTTP request
    to the user in its bearer token. Attribution only — routes still
    authenticate; an invalid token simply leaves the owner unset."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = None
        owner = _owner_from_headers(scope.get("headers") or [])
        if owner is not None:
            token = _owner.set(owner)
        feature_token = _feature.set(feature_for_path(scope.get("path") or ""))
        try:
            await self.app(scope, receive, send)
        finally:
            _feature.reset(feature_token)
            if token is not None:
                _owner.reset(token)


def _owner_from_headers(headers) -> UsageOwner | None:
    from app.security import TokenManager

    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        scheme, _, credentials = value.decode("latin-1").partition(" ")
        if scheme.lower() != "bearer" or not credentials:
            return None
        try:
            payload = TokenManager.decode_access_token(credentials.strip())
            return UsageOwner(UUID(str(payload["tenant_id"])), UUID(str(payload["sub"])))
        except Exception:  # noqa: BLE001
            return None
    return None


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose tasks run in a copy of the submitter's
    context, so usage is attributed to the user who started the work."""

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)


def estimate_cost_usd(
    prompt_tokens: int, completion_tokens: int, *, kind: str = "chat"
) -> Decimal:
    settings = get_settings()
    if kind == "embedding":
        per_mtok_in, per_mtok_out = settings.embedding_price_per_mtok, 0.0
    else:
        per_mtok_in = settings.llm_input_price_per_mtok
        per_mtok_out = settings.llm_output_price_per_mtok
    cost = (prompt_tokens * per_mtok_in + completion_tokens * per_mtok_out) / 1_000_000
    return Decimal(str(round(cost, 6)))


def usage_tokens(response: Any) -> tuple[int, int]:
    """(prompt, completion) tokens from an OpenAI-style response; (0, 0) if absent."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    if not prompt and not completion:
        prompt = int(getattr(usage, "total_tokens", 0) or 0)
    return prompt, completion


_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-usage")

BUCKET_SECONDS = 300


def bucket_start(moment: datetime | None = None) -> datetime:
    """Start of the 5-minute UTC bucket containing ``moment`` (default: now)."""
    moment = moment or datetime.now(timezone.utc)
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % BUCKET_SECONDS, tz=timezone.utc)


def record_llm_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int = 0,
    *,
    kind: str = "chat",
) -> None:
    """Add one call's tokens to the current user's row for this 5-minute
    bucket. Never raises."""
    owner = _owner.get()
    if owner is None:
        # Startup/CLI work with no signed-in user: nothing to attribute to.
        logger.debug("LLM usage not attributed (no user in context), model=%s", model)
        return
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return
    cost = estimate_cost_usd(prompt_tokens, completion_tokens, kind=kind)
    try:
        _writer.submit(
            write_usage_row,
            owner,
            bucket_start(),
            (model or "unknown")[:120],
            prompt_tokens,
            completion_tokens,
            cost,
            feature=_feature.get()[:40],
        )
    except RuntimeError:  # interpreter shutting down
        pass


def write_usage_row(
    owner: UsageOwner,
    bucket: datetime,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: Decimal,
    *,
    feature: str = "other",
) -> None:
    """Atomic upsert: concurrent calls for the same row add up, none are lost."""
    from app.database import SessionLocal, engine
    from app.models import LLMUsage

    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    table = LLMUsage.__table__
    stmt = insert(table).values(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        bucket_start=bucket,
        model=model,
        feature=feature,
        calls=1,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenant_id", "user_id", "bucket_start", "model", "feature"],
        set_={
            "calls": table.c.calls + 1,
            "prompt_tokens": table.c.prompt_tokens + prompt_tokens,
            "completion_tokens": table.c.completion_tokens + completion_tokens,
            "cost_usd": table.c.cost_usd + cost,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    db = SessionLocal()
    try:
        db.execute(stmt)
        db.commit()
    except Exception:  # noqa: BLE001 — tracking must never break a request
        db.rollback()
        logger.warning("Failed to record LLM usage for user %s", owner.user_id, exc_info=True)
    finally:
        db.close()
