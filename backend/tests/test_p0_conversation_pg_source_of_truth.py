"""P0-3: PostgreSQL is authoritative for conversation analysis state."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.agent.analysis_state import (
    AnalysisState,
    analysis_state_to_json,
    analysis_state_to_memory_blob,
    merge_workflow_into_state,
)
from app.agent.query_plan import QueryPlan
from app.database import get_db
from app.main import app
from app.models import Conversation, User
from app.redis_client import ConversationMemory
from app.services.conversation_state_service import ConversationStateService
from tests.test_conversation_memory import FakeRedis
from tests.test_tenant_isolation import (
    _create_connection,
    _create_conversation,
    _signup_and_token,
)


def _region_plan() -> QueryPlan:
    return QueryPlan(intent="aggregation", metrics=["Revenue"], dimensions=["Region"])


def _db_session(client) -> Session:
    test_client, _ = client
    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    return next(gen)


def _close_db(gen) -> None:
    try:
        next(gen)
    except StopIteration:
        pass


def test_pg_exists_redis_missing_returns_pg(client, monkeypatch) -> None:
    monkeypatch.setattr("app.redis_client.get_redis", lambda: FakeRedis())
    test_client, sent = client
    headers, _, _, user_id = _signup_and_token(test_client, sent, "pg-only@test.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        user = db.get(User, user_id)
        assert conv and user
        state = AnalysisState(
            turn_count=1,
            last_sql="SELECT region FROM sales",
            query_plan=_region_plan(),
        )
        conv.analysis_state_json = analysis_state_to_json(state)
        db.commit()

        loaded = ConversationStateService().load(db, conversation_id=conv_id, user=user)
    finally:
        _close_db(gen)

    assert loaded.last_sql == "SELECT region FROM sales"
    assert loaded.query_plan is not None


def test_pg_newer_than_redis_wins(client, monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake_redis)

    test_client, sent = client
    headers, _, tenant_id, user_id = _signup_and_token(
        test_client, sent, "pg-newer@test.com"
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        user = db.get(User, user_id)
        assert conv and user

        pg_pending = {"plan": {"dashboard_id": str(uuid4()), "operations": []}}
        pg_state = AnalysisState(turn_count=2, last_sql="SELECT 2")
        pg_state.dashboard.pending_operation = pg_pending
        conv.analysis_state_json = analysis_state_to_json(pg_state)
        db.commit()

        stale = AnalysisState(turn_count=2, last_sql="SELECT 1")
        stale.dashboard.pending_operation = None
        ConversationMemory().set_state(
            tenant_id, conv_id, analysis_state_to_memory_blob(stale)
        )

        loaded = ConversationStateService().load(db, conversation_id=conv_id, user=user)
    finally:
        _close_db(gen)

    assert loaded.dashboard.pending_operation == pg_pending
    assert loaded.last_sql == "SELECT 2"


def test_redis_unavailable_pg_still_works(client, monkeypatch) -> None:
    def broken_redis():
        raise ConnectionError("Redis down")

    monkeypatch.setattr("app.redis_client.get_redis", broken_redis)
    test_client, sent = client
    headers, _, _, user_id = _signup_and_token(test_client, sent, "no-redis@test.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        user = db.get(User, user_id)
        assert conv and user
        conv.analysis_state_json = analysis_state_to_json(
            AnalysisState(turn_count=1, last_sql="SELECT 3")
        )
        db.commit()
        loaded = ConversationStateService().load(db, conversation_id=conv_id, user=user)
    finally:
        _close_db(gen)

    assert loaded.last_sql == "SELECT 3"


def test_two_sequential_saves_second_turn_visible(client, monkeypatch) -> None:
    monkeypatch.setattr("app.redis_client.get_redis", lambda: FakeRedis())
    test_client, sent = client
    headers, _, _, user_id = _signup_and_token(test_client, sent, "two-turn@test.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        user = db.get(User, user_id)
        assert user
        svc = ConversationStateService()
        svc.save(
            db,
            conversation_id=conv_id,
            user=user,
            question="Show revenue",
            final={"answer": "Here.", "sql": "SELECT 1", "intent": "sql"},
            connection_id=conn_id,
        )
        svc.save(
            db,
            conversation_id=conv_id,
            user=user,
            question="Only West",
            final={"answer": "Filtered.", "sql": "SELECT 2", "intent": "sql"},
            connection_id=conn_id,
        )
        loaded = svc.load(db, conversation_id=conv_id, user=user)
    finally:
        _close_db(gen)

    assert loaded.turn_count == 2
    assert loaded.last_sql == "SELECT 2"
    assert loaded.last_user_question == "Only West"


def test_pg_update_redis_write_fails_pg_correct_on_reload(client, monkeypatch) -> None:
    class FailSetRedis(FakeRedis):
        def setex(self, key: str, ttl: int, value: str) -> bool:
            raise ConnectionError("Redis write failed")

    monkeypatch.setattr("app.redis_client.get_redis", lambda: FailSetRedis())
    test_client, sent = client
    headers, _, _, user_id = _signup_and_token(test_client, sent, "redis-fail@test.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        user = db.get(User, user_id)
        assert user
        svc = ConversationStateService()
        svc.save(
            db,
            conversation_id=conv_id,
            user=user,
            question="persist me",
            final={"answer": "ok", "sql": "SELECT 9", "intent": "sql"},
            connection_id=conn_id,
        )
        loaded = svc.load(db, conversation_id=conv_id, user=user)
    finally:
        _close_db(gen)

    assert loaded.last_sql == "SELECT 9"


def test_load_repairs_cache_from_pg(client, monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake_redis)

    test_client, sent = client
    headers, _, tenant_id, user_id = _signup_and_token(
        test_client, sent, "repair-cache@test.com"
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        user = db.get(User, user_id)
        assert conv and user
        conv.analysis_state_json = analysis_state_to_json(
            AnalysisState(turn_count=1, last_sql="SELECT repair")
        )
        db.commit()
        ConversationStateService().load(db, conversation_id=conv_id, user=user)
    finally:
        _close_db(gen)

    mem = ConversationMemory()
    cached = mem.get(tenant_id, conv_id)
    assert cached is not None
    assert cached.get("last_sql") == "SELECT repair"
