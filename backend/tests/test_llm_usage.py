"""Per-user LLM usage tracking (llm_usage)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database_mod
import app.llm.usage as usage_mod
from app.database import Base
from app.llm.usage import (
    ContextThreadPoolExecutor,
    UsageOwner,
    UsageOwnerMiddleware,
    bucket_start,
    current_usage_feature,
    current_usage_owner,
    feature_for_path,
    record_llm_usage,
    usage_feature,
    usage_owner,
    write_usage_row,
)
from app.models import LLMUsage
from app.security import TokenManager


class _CapturingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((*args, kwargs.get("feature")))


def test_upsert_adds_to_the_same_bucket_row(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database_mod, "engine", engine)
    monkeypatch.setattr(database_mod, "SessionLocal", Session)

    owner = UsageOwner(uuid4(), uuid4())
    now = bucket_start(datetime(2026, 9, 29, 8, 7, 30, tzinfo=timezone.utc))
    later = bucket_start(datetime(2026, 9, 29, 8, 12, tzinfo=timezone.utc))
    assert now == datetime(2026, 9, 29, 8, 5, tzinfo=timezone.utc)
    write_usage_row(owner, now, "gpt-x", 100, 20, Decimal("0.001"), feature="chat")
    write_usage_row(owner, now, "gpt-x", 50, 5, Decimal("0.0005"), feature="chat")
    write_usage_row(owner, now, "gpt-x", 70, 7, Decimal("0"), feature="schema_discovery")
    write_usage_row(owner, now, "embed-x", 30, 0, Decimal("0"), feature="chat")
    write_usage_row(owner, later, "gpt-x", 1, 1, Decimal("0"), feature="chat")

    with Session() as db:
        rows = db.scalars(select(LLMUsage)).all()
        assert len(rows) == 4
        discovery = next(r for r in rows if r.feature == "schema_discovery")
        assert (discovery.calls, discovery.prompt_tokens) == (1, 70)
        gpt_today = next(
            r
            for r in rows
            if r.model == "gpt-x"
            and r.bucket_start.replace(tzinfo=None) == now.replace(tzinfo=None)
            and r.feature == "chat"
        )
        assert gpt_today.calls == 2
        assert gpt_today.prompt_tokens == 150
        assert gpt_today.completion_tokens == 25
        assert Decimal(str(gpt_today.cost_usd)) == Decimal("0.0015")


def test_no_signed_in_user_records_nothing(monkeypatch) -> None:
    writer = _CapturingWriter()
    monkeypatch.setattr(usage_mod, "_writer", writer)
    record_llm_usage("gpt-x", 100, 10)
    assert writer.calls == []


def test_records_for_current_user_with_cost(monkeypatch) -> None:
    writer = _CapturingWriter()
    monkeypatch.setattr(usage_mod, "_writer", writer)
    settings = usage_mod.get_settings()
    monkeypatch.setattr(settings, "llm_input_price_per_mtok", 2.0)
    monkeypatch.setattr(settings, "llm_output_price_per_mtok", 8.0)

    tenant_id, user_id = uuid4(), uuid4()
    with usage_owner(tenant_id, user_id), usage_feature("semantic_model"):
        record_llm_usage("gpt-x", 1_000, 500)
    assert current_usage_owner() is None
    assert current_usage_feature() == "other"

    (owner, _day, model, prompt, completion, cost, feature), = writer.calls
    assert owner == UsageOwner(tenant_id, user_id)
    assert (model, prompt, completion, feature) == ("gpt-x", 1_000, 500, "semantic_model")
    assert cost == Decimal("0.006")  # 1000*2/1M + 500*8/1M


def test_context_pool_keeps_the_user() -> None:
    tenant_id, user_id = uuid4(), uuid4()
    with usage_owner(tenant_id, user_id):
        with ContextThreadPoolExecutor(max_workers=2) as pool:
            seen = list(pool.map(lambda _: current_usage_owner(), range(3)))
    assert seen == [UsageOwner(tenant_id, user_id)] * 3


def test_llm_client_reports_response_usage(monkeypatch) -> None:
    import app.llm.client as client_mod

    recorded: list[tuple] = []
    monkeypatch.setattr(
        client_mod, "record_llm_usage", lambda *args, **kw: recorded.append(args)
    )

    class Usage:
        prompt_tokens = 42
        completion_tokens = 7

    class Response:
        model = "gpt-x-2026"
        usage = Usage()
        choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

    llm = client_mod.LLMClient()
    llm.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": type("Comp", (), {"create": lambda self, **kw: Response()})()})()},
    )()
    assert llm.complete("hi") == "ok"
    assert recorded == [("gpt-x-2026", 42, 7)]


def test_request_user_reaches_threadpool_and_background_pool() -> None:
    """Sync routes run in FastAPI's threadpool and background work (schema
    discovery, memory) in our pools — the signed-in user must reach both."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    pool = ContextThreadPoolExecutor(max_workers=1)

    @app.get("/api/v1/semantic/_usage_probe")
    def probe() -> dict:
        background = pool.submit(current_usage_owner).result()
        here = current_usage_owner()
        return {
            "route": str(here.user_id) if here else None,
            "background": str(background.user_id) if background else None,
            "feature": pool.submit(current_usage_feature).result(),
        }

    tenant_id, user_id = uuid4(), uuid4()
    token = TokenManager.create_access_token(
        user_id=user_id, tenant_id=tenant_id, email="a@example.com"
    )
    client = TestClient(app)  # no `with`: skip lifespan (DB init)
    url = "/api/v1/semantic/_usage_probe"
    body = client.get(url, headers={"Authorization": f"Bearer {token}"}).json()
    assert body == {
        "route": str(user_id),
        "background": str(user_id),
        "feature": "semantic_model",
    }
    assert client.get(url).json()["route"] is None


def test_feature_for_path() -> None:
    assert feature_for_path("/api/v1/connections/abc/discover") == "schema_discovery"
    assert feature_for_path("/api/v1/semantic/model-import") == "semantic_model"
    assert feature_for_path("/api/v1/dashboards/x/refresh") == "dashboards"
    assert feature_for_path("/api/v1/conversations") == "chat"
    assert feature_for_path("/health") == "other"


def test_memory_update_is_labelled_chat_memory(monkeypatch) -> None:
    import app.services.memory_service as memory_mod

    seen: list[str] = []
    monkeypatch.setattr(memory_mod, "update_after_turn", lambda db, **kw: seen.append(current_usage_feature()))
    monkeypatch.setattr(memory_mod, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())
    with usage_feature("chat"):
        memory_mod._update_safely()
    assert seen == ["chat_memory"]


def test_middleware_sets_owner_from_bearer_token() -> None:
    tenant_id, user_id = uuid4(), uuid4()
    token = TokenManager.create_access_token(
        user_id=user_id, tenant_id=tenant_id, email="a@example.com"
    )
    seen: list = []

    async def inner(scope, receive, send):
        seen.append(current_usage_owner())

    mw = UsageOwnerMiddleware(inner)
    headers = [(b"authorization", f"Bearer {token}".encode())]
    asyncio.run(mw({"type": "http", "headers": headers}, None, None))
    asyncio.run(mw({"type": "http", "headers": [(b"authorization", b"Bearer junk")]}, None, None))
    asyncio.run(mw({"type": "http", "headers": []}, None, None))

    assert seen == [UsageOwner(tenant_id, user_id), None, None]
    assert current_usage_owner() is None
