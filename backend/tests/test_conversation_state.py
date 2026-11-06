"""Persistent conversation analysis state tests."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

import app.agent.workflow as workflow_mod

from app.agent.analysis_state import (
    AnalysisState,
    analysis_state_to_json,
    summarize_rows,
)
from app.agent.follow_up import resolve_follow_up
from app.agent.query_plan import FilterSpec, QueryPlan, TimeSpec
from app.agent.sql_compiler import compile_query_plan
import app.agent.workflow as workflow_mod
from app.agent.types import AgentQuestion
from app.agent.service import run_question
from app.database import get_db
from app.main import app
from app.models import Conversation, User
from app.services.conversation_state_service import ConversationStateService
from tests.test_conversation_memory import FakeRedis
from tests.test_tenant_isolation import (
    _create_connection,
    _create_conversation,
    _signup_and_token,
)


def _make_user(tenant_id: UUID, user_id: UUID | None = None) -> User:
    return User(
        id=user_id or uuid4(),
        tenant_id=tenant_id,
        email="analyst@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )


def _seed_pg_state(
    db: Session,
    conv: Conversation,
    state: AnalysisState,
) -> None:
    conv.analysis_state_json = analysis_state_to_json(state)
    db.commit()


def _region_revenue_plan() -> QueryPlan:
    return QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        dimensions=["Region"],
    )


def test_follow_up_filter_merge() -> None:
    prior = AnalysisState(
        query_plan=_region_revenue_plan(),
        turn_count=1,
        last_sql="SELECT region, SUM(revenue) FROM sales GROUP BY region",
    )
    plan, handled = resolve_follow_up("Only West", prior)
    assert handled is True
    assert plan is not None
    assert len(plan.filters) == 1
    assert plan.filters[0].field == "Region"
    assert plan.filters[0].value == "West"


def test_follow_up_time_comparison() -> None:
    prior = AnalysisState(
        query_plan=_region_revenue_plan(),
        turn_count=1,
    )
    plan, handled = resolve_follow_up("Compare with last year", prior)
    assert handled is True
    assert plan is not None
    assert plan.intent == "comparison"
    assert plan.time is not None
    assert plan.time.range == "ytd"
    assert plan.comparison is not None
    assert plan.comparison.type == "previous_year"


def test_result_summary_size_bounded() -> None:
    rows = [
        {f"col_{i}": "x" * 200 for i in range(12)}
        for _ in range(100)
    ]
    summary = summarize_rows(rows, headline="big result")
    assert summary is not None
    assert summary.row_count == 100
    assert len(summary.sample_rows) <= 5
    assert len(summary.columns) <= 8
    for row in summary.sample_rows:
        assert len(row) <= 8
        for val in row.values():
            if isinstance(val, str):
                assert len(val) <= 120


def test_no_full_transcript_in_sql_prompt(monkeypatch) -> None:
    captured_ctx: list[str] = []

    def spy_parse(llm, question, semantic, ctx):
        captured_ctx.append(ctx)
        return None

    prior = AnalysisState(
        turn_count=2,
        last_sql="SELECT region FROM sales",
        last_user_question="Show revenue by region",
        last_assistant_answer="Here are the regions.",
        query_plan=_region_revenue_plan(),
    )
    state = {
        "question": "Show monthly revenue trend breakdown by region for fiscal year",
        "tenant_id": "t1",
        "connection_id": "c1",
        "analysis_state": prior.model_dump(mode="json"),
    }
    monkeypatch.setattr(workflow_mod, "parse_query_plan", spy_parse)
    monkeypatch.setattr(workflow_mod, "get_llm", lambda: type("L", (), {"complete": lambda s, p: "SELECT 1"})())
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: {
            "db_type": "postgres",
            "tables": [
                {
                    "table_name": "sales",
                    "schema_name": "public",
                    "columns": [
                        {"name": "region", "data_type": "varchar"},
                        {"name": "revenue", "data_type": "numeric"},
                    ],
                }
            ],
        },
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda *a, **k: {
            "metrics": [{"name": "Revenue", "expression": "SUM(revenue)"}],
            "dimensions": [{"name": "Region", "expression": "region"}],
        },
    )

    workflow_mod.SQLGenerator().run(state)

    assert captured_ctx, "parse_query_plan should receive structured context"
    ctx = captured_ctx[0]
    assert "Last user question: Show revenue by region" in ctx
    assert "user: which client" not in ctx
    assert "assistant: Client" not in ctx


def test_state_survives_redis_expiry(monkeypatch) -> None:
    """Empty Redis falls back to PG state for follow-up SQL compilation."""
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake_redis)

    plan = _region_revenue_plan()
    pg_state = AnalysisState(
        query_plan=plan,
        last_sql="SELECT region, SUM(revenue) FROM public.sales GROUP BY region",
        turn_count=1,
    )
    pg_state.semantic_focus.metrics = ["Revenue"]
    pg_state.semantic_focus.dimensions = ["Region"]

    merged, handled = resolve_follow_up("Only West", pg_state)
    assert handled
    assert merged is not None
    sql = compile_query_plan(
        merged,
        {
            "metrics": [{"name": "Revenue", "expression": "SUM(revenue)"}],
            "dimensions": [{"name": "Region", "expression": "region"}],
        },
        {
            "db_type": "postgres",
            "tables": [
                {
                    "table_name": "sales",
                    "schema_name": "public",
                    "columns": [
                        {"name": "region", "data_type": "varchar"},
                        {"name": "revenue", "data_type": "numeric"},
                    ],
                }
            ],
        },
        "postgres",
    )
    assert "West" in sql


def test_state_survives_backend_restart(client, monkeypatch) -> None:
    """PG-only load when Redis is empty."""
    test_client, sent = client
    monkeypatch.setattr("app.redis_client.get_redis", lambda: type("R", (), {"get": lambda s, k: None})())

    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, "restart-user@test.com"
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db: Session = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        assert conv is not None
        user = db.get(User, user_id)
        assert user is not None
        state = AnalysisState(
            last_sql="SELECT region FROM sales",
            last_user_question="Show revenue by region",
            turn_count=1,
            query_plan=_region_revenue_plan(),
        )
        _seed_pg_state(db, conv, state)
        svc = ConversationStateService()
        loaded = svc.load(db, conversation_id=conv_id, user=user)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    assert loaded.last_sql == "SELECT region FROM sales"
    assert loaded.turn_count == 1
    assert loaded.query_plan is not None
    assert loaded.query_plan.dimensions == ["Region"]


def test_cross_tenant_state_denied(client, monkeypatch) -> None:
    test_client, sent = client
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake_redis)

    headers_a, _t, tenant_a, user_a = _signup_and_token(
        test_client, sent, "cross-a@test.com", tenant_name="Cross A"
    )
    headers_b, _t, tenant_b, _user_b = _signup_and_token(
        test_client, sent, "cross-b@test.com", tenant_name="Cross B"
    )
    conn_a = _create_connection(test_client, headers_a, monkeypatch)
    conv_id = _create_conversation(test_client, headers_a, connection_id=conn_a)

    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db: Session = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        assert conv is not None
        state = AnalysisState(last_sql="SELECT secret", turn_count=1)
        _seed_pg_state(db, conv, state)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    resp = test_client.get(
        f"/api/v1/conversations/{conv_id}/context",
        headers=headers_b,
    )
    assert resp.status_code == 404


def test_context_endpoint_returns_pg_state(client, monkeypatch) -> None:
    test_client, sent = client
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake_redis)

    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, "ctx-user@test.com"
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db: Session = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        assert conv is not None
        plan = _region_revenue_plan()
        state = AnalysisState(
            query_plan=plan,
            last_sql="SELECT region FROM sales",
            turn_count=2,
            last_user_question="Show revenue by region",
        )
        _seed_pg_state(db, conv, state)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    resp = test_client.get(
        f"/api/v1/conversations/{conv_id}/context",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == str(conv_id)
    assert body["last_sql"] == "SELECT region FROM sales"
    assert body["turn_count"] == 2
    assert body["analysis_state"]["query_plan"]["dimensions"] == ["Region"]
    assert "messages" not in body


def test_logout_login_resume(monkeypatch) -> None:
    """Same conversation_id loads PG state on each session (simulated re-login)."""
    tenant_id = uuid4()
    user_id = uuid4()
    conv_id = uuid4()
    load_count = 0

    class FakeStateService:
        def load(self, db, *, conversation_id, user):
            nonlocal load_count
            load_count += 1
            return AnalysisState(
                last_sql="SELECT region FROM sales",
                turn_count=load_count,
                last_user_question="Show revenue by region",
                query_plan=_region_revenue_plan(),
            )

        def save(self, *args, **kwargs):
            return AnalysisState()

    monkeypatch.setattr(workflow_mod, "ConversationStateService", FakeStateService)

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            if "Classify" in prompt or "intent" in prompt.lower():
                return "sql"
            return "SELECT region FROM sales WHERE region = 'West'"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: {
            "db_type": "postgres",
            "tables": [
                {
                    "table_name": "sales",
                    "schema_name": "public",
                    "columns": [
                        {"name": "region", "data_type": "varchar"},
                        {"name": "revenue", "data_type": "numeric"},
                    ],
                }
            ],
        },
    )
    monkeypatch.setattr(
        workflow_mod.QueryExecutor,
        "run",
        lambda self, state: {"rows": [{"region": "West"}], "steps": ["execute_query"]},
    )
    monkeypatch.setattr(workflow_mod, "_persist_analysis_state", lambda **k: None)

    fake_user = _make_user(tenant_id, user_id)

    class FakeSession:
        def get(self, model, pk):
            if model is User and pk == user_id:
                return fake_user
            return None

        def close(self) -> None:
            pass

    monkeypatch.setattr(workflow_mod, "SessionLocal", FakeSession)

    for question in ("Only West", "Compare with last year"):
        result = run_question(
            AgentQuestion(
                tenant_id=tenant_id,
                user_id=user_id,
                question=question,
                connection_id=uuid4(),
                conversation_id=conv_id,
            )
        )
        assert result.intent == "sql"

    assert load_count == 2


def test_recent_turns_are_kept_bounded_and_shown_to_the_model() -> None:
    from app.agent.analysis_state import (
        MAX_RECENT_TURNS,
        MAX_TURN_ANSWER_LEN,
        format_analysis_state_context,
        merge_workflow_into_state,
    )

    state = AnalysisState()
    for i in range(MAX_RECENT_TURNS + 2):
        state = merge_workflow_into_state(
            state,
            question=f"question {i}",
            final={"answer": f"answer {i} " + "x" * 2000},
        )
    assert len(state.recent_turns) == MAX_RECENT_TURNS
    assert state.recent_turns[0].question == "question 2"
    assert all(len(t.answer) <= MAX_TURN_ANSWER_LEN for t in state.recent_turns)

    ctx = format_analysis_state_context(state)
    assert "Recent conversation (oldest first):" in ctx
    assert "User: question 6" in ctx and "Assistant: answer 6" in ctx
    assert "question 1" not in ctx
    # Survives the JSON round trip used for persistence.
    from app.agent.analysis_state import analysis_state_from_json

    assert analysis_state_from_json(analysis_state_to_json(state)).recent_turns == state.recent_turns
