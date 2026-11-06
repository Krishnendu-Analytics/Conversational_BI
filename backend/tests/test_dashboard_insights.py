"""Tests for persistent AI Insights dashboard widget."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.agent.dashboard import execute_dashboard
from app.agent.dashboard_insight_facts import (
    InsightFactStatus,
    extract_dashboard_insight_facts,
    facts_fingerprint,
    render_deterministic_insight_markdown,
    validate_narrative_numbers,
)
from app.agent.dashboard_insight_narrator import narrate_dashboard_insights
from app.agent.dashboard_insight_service import (
    ensure_ai_insight_widget,
    refresh_ai_insight_widgets,
)


def _dashboard(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(uuid4()),
        "title": "Sales",
        "filters": [],
        "widgets": [
            {
                "id": "kpi-revenue",
                "type": "kpi",
                "title": "Revenue",
                "value": 1000,
                "rows": [{"value": 1000}],
            },
            {
                "id": "chart-region",
                "type": "chart",
                "title": "Revenue by Region",
                "chart": "bar",
                "rows": [
                    {"category": "West", "value": 600},
                    {"category": "East", "value": 400},
                ],
            },
            {
                "id": "insight-1",
                "type": "ai_insight",
                "title": "AI Insights",
            },
        ],
    }
    base.update(overrides)
    return base


def test_extract_facts_from_kpi_and_bar() -> None:
    facts = extract_dashboard_insight_facts(_dashboard())
    assert facts.status == InsightFactStatus.OK
    assert len(facts.kpis) == 1
    assert facts.kpis[0].value == 1000
    assert len(facts.categories) == 1
    assert facts.categories[0].top[0].label == "West"
    assert facts.categories[0].concentration_top1_pct == 60.0


def test_extract_trend_change_pct() -> None:
    dash = _dashboard(
        widgets=[
            {
                "id": "trend-1",
                "type": "chart",
                "title": "Trend",
                "chart": "line",
                "rows": [
                    {"period": "2024-01", "value": 100},
                    {"period": "2024-02", "value": 110},
                    {"period": "2024-03", "value": 150},
                ],
            },
            {"id": "insight-1", "type": "ai_insight", "title": "AI Insights"},
        ]
    )
    facts = extract_dashboard_insight_facts(dash)
    assert len(facts.trends) == 1
    assert facts.trends[0].direction == "up"
    assert facts.trends[0].change_pct == 50.0


def test_extract_anomaly_when_enough_points() -> None:
    rows = [{"period": f"p{i}", "value": 100 + (50 if i == 7 else 0)} for i in range(8)]
    dash = _dashboard(
        widgets=[
            {"id": "trend-1", "type": "chart", "title": "Trend", "chart": "line", "rows": rows},
            {"id": "insight-1", "type": "ai_insight", "title": "AI Insights"},
        ]
    )
    facts = extract_dashboard_insight_facts(dash)
    assert facts.trends[0].potential_anomalies
    highlights = " ".join(facts.ranked_highlights)
    assert "Potential anomaly" in highlights


def test_insufficient_data_no_kpis_no_rows() -> None:
    facts = extract_dashboard_insight_facts(
        {"id": "d1", "widgets": [{"id": "insight-1", "type": "ai_insight", "title": "AI Insights"}]}
    )
    assert facts.status == InsightFactStatus.INSUFFICIENT_DATA
    content = render_deterministic_insight_markdown(facts)
    assert "Not enough dashboard data" in content


def test_insufficient_data_single_kpi_only() -> None:
    facts = extract_dashboard_insight_facts(
        {
            "id": "d1",
            "widgets": [
                {"id": "k1", "type": "kpi", "title": "Revenue", "value": 50, "rows": [{"value": 50}]},
                {"id": "insight-1", "type": "ai_insight", "title": "AI Insights"},
            ],
        }
    )
    assert facts.status == InsightFactStatus.INSUFFICIENT_DATA
    assert "breakdown or trend" in (facts.insufficient_reason or "")


def test_partial_data_some_widgets_error() -> None:
    facts = extract_dashboard_insight_facts(
        {
            "id": "d1",
            "widgets": [
                {"id": "k1", "type": "kpi", "title": "Revenue", "value": 10, "rows": [{"value": 10}]},
                {
                    "id": "c1",
                    "type": "chart",
                    "title": "By Region",
                    "chart": "bar",
                    "rows": [{"category": "A", "value": 1}],
                },
                {"id": "c2", "type": "chart", "title": "Bad", "chart": "bar", "error": "fail"},
                {"id": "insight-1", "type": "ai_insight", "title": "AI Insights"},
            ],
        }
    )
    assert facts.status == InsightFactStatus.PARTIAL_DATA
    assert facts.widget_errors


def test_facts_hash_changes_on_filter() -> None:
    unfiltered = extract_dashboard_insight_facts(_dashboard())
    filtered = extract_dashboard_insight_facts(
        _dashboard(filters=[{"field": "Region", "operator": "eq", "value": "West"}])
    )
    assert facts_fingerprint(unfiltered) != facts_fingerprint(filtered)


def test_narrator_receives_facts_not_raw_rows() -> None:
    captured: list[str] = []

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            captured.append(prompt)
            return "## AI Insights\n\n- Revenue is 1,000.\n- West leads."

    facts = extract_dashboard_insight_facts(_dashboard())
    content, mode = narrate_dashboard_insights(facts, FakeLLM())
    assert mode == "llm"
    assert captured
    assert "SELECT" not in captured[0]
    assert "kpis" in captured[0]
    assert content


def test_narrator_stub_uses_deterministic_fallback() -> None:
    facts = extract_dashboard_insight_facts(_dashboard())

    class StubLLM:
        def complete(self, prompt: str) -> str:
            return "[llm stub]"

    content, mode = narrate_dashboard_insights(facts, StubLLM())
    assert mode == "deterministic_fallback"
    # No "## AI Insights" heading here — the widget itself already shows
    # that title, so the content should go straight to the bullets.
    assert not content.startswith("#")
    assert "- " in content


def test_llm_invented_number_rejected() -> None:
    facts = extract_dashboard_insight_facts(_dashboard())
    bad = "## AI Insights\n\n- Revenue hit 123456789 units."
    assert not validate_narrative_numbers(bad, facts)


def test_refresh_ai_insight_widgets_updates_content() -> None:
    dash = _dashboard()
    refresh_ai_insight_widgets(dash, llm=None)
    insight = next(w for w in dash["widgets"] if w["type"] == "ai_insight")
    assert insight.get("content")
    assert insight.get("config", {}).get("facts_hash")


def test_ensure_ai_insight_widget_appends_default() -> None:
    dash = {"widgets": [{"id": "k1", "type": "kpi", "title": "K", "position": {"y": 0, "h": 3}}]}
    ensure_ai_insight_widget(dash)
    assert any(w["type"] == "ai_insight" for w in dash["widgets"])


def test_ranked_highlights_prioritize_trends_over_kpis() -> None:
    dash = _dashboard(
        widgets=[
            {"id": "k1", "type": "kpi", "title": "Revenue", "value": 100, "rows": [{"value": 100}]},
            {
                "id": "t1",
                "type": "chart",
                "title": "Trend",
                "chart": "line",
                "rows": [
                    {"period": "a", "value": 10},
                    {"period": "b", "value": 20},
                    {"period": "c", "value": 40},
                ],
            },
            {"id": "insight-1", "type": "ai_insight", "title": "AI Insights"},
        ]
    )
    facts = extract_dashboard_insight_facts(dash)
    assert facts.ranked_highlights
    assert "Trend" in facts.ranked_highlights[0]


def test_filter_lifecycle_e2e() -> None:
    dash = _dashboard()
    refresh_ai_insight_widgets(dash, llm=None)
    insight = next(w for w in dash["widgets"] if w["type"] == "ai_insight")
    hash_before = insight["config"]["facts_hash"]
    content_before = insight["content"]

    dash["filters"] = [{"field": "Region", "operator": "eq", "value": "West"}]
    refresh_ai_insight_widgets(dash, llm=None, force=True)
    insight = next(w for w in dash["widgets"] if w["type"] == "ai_insight")
    hash_filtered = insight["config"]["facts_hash"]
    assert hash_filtered != hash_before

    dash["filters"] = []
    refresh_ai_insight_widgets(dash, llm=None, force=True)
    insight = next(w for w in dash["widgets"] if w["type"] == "ai_insight")
    assert insight["config"]["facts_hash"] == hash_before
    assert insight["content"] == content_before


def test_insights_work_when_redis_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr("app.redis_client.ConversationMemory.set_state", boom)
    dash = _dashboard()
    refresh_ai_insight_widgets(dash, llm=None)
    insight = next(w for w in dash["widgets"] if w["type"] == "ai_insight")
    assert insight.get("content")
    assert "- " in insight["content"]


def test_execute_dashboard_refreshes_insights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.agent.dashboard._validate_and_run",
        lambda *a, **k: ([{"value": 100}], "SELECT 1"),
    )
    monkeypatch.setattr("app.llm.client.get_llm", lambda: None)
    spec = {
        "title": "T",
        "widgets": [
            {"id": "k1", "type": "kpi", "title": "K", "sql": "SELECT 1 AS value"},
        ],
    }
    ensure_ai_insight_widget(spec)
    result = execute_dashboard(
        spec,
        connection_id=str(uuid4()),
        schema={"db_type": "postgres", "tables": []},
        db_type="postgres",
        tenant_id=uuid4(),
    )
    assert any(w.get("type") == "ai_insight" and w.get("content") for w in result["widgets"])


def test_widget_to_dict_exposes_content() -> None:
    from app.models import DashboardWidget
    from app.services.dashboard_service import _widget_to_dict

    widget = DashboardWidget(
        id=uuid4(),
        dashboard_id=uuid4(),
        tenant_id=uuid4(),
        type="ai_insight",
        title="AI Insights",
        config_json=json.dumps({"content": "Hello insight", "insight_status": "ok"}),
        position_json="{}",
    )
    data = _widget_to_dict(widget)
    assert data["content"] == "Hello insight"
    assert data["insight_status"] == "ok"


def test_cross_tenant_insight_not_leaked(client) -> None:
    from sqlalchemy import select

    from app.database import get_db
    from app.models import Tenant, User
    from app.services.dashboard_service import create_dashboard_from_spec, get_dashboard

    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "InsightTenantA",
            "email": "insight-a@test.com",
            "password": "Password@123",
        },
    )
    verify_a = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "insight-a@test.com", "otp_code": sent["otp"]},
    )
    assert verify_a.status_code == 200

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        user_a = db.scalar(select(User).where(User.email == "insight-a@test.com"))
        assert user_a is not None
        tenant_b = Tenant(name="InsightTenantB")
        db.add(tenant_b)
        db.flush()
        user_b = User(
            tenant_id=tenant_b.id,
            email="insight-b@test.com",
            role="analyst",
            password_hash="x",
            is_verified=True,
        )
        db.add(user_b)
        db.flush()
        spec = _dashboard()
        refresh_ai_insight_widgets(spec, llm=None)
        insight = next(w for w in spec["widgets"] if w["type"] == "ai_insight")
        assert insight.get("content")
        persisted = create_dashboard_from_spec(db, user_a, uuid4(), spec)
        dash_id = UUID(str(persisted["id"]))
        with pytest.raises(ValueError, match="Dashboard not found"):
            get_dashboard(db, dash_id, user_b)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_execute_dashboard_widgets_persists_insight_config(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from app.database import get_db
    from app.models import Connection, DashboardWidget, User
    from app.services.dashboard_service import (
        create_dashboard_from_spec,
        execute_dashboard_widgets,
    )

    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "PersistInsight",
            "email": "persist-insight@test.com",
            "password": "Password@123",
        },
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "persist-insight@test.com", "otp_code": sent["otp"]},
    )
    assert verify.status_code == 200

    monkeypatch.setattr(
        "app.agent.dashboard._validate_and_run",
        lambda *a, **k: ([{"value": 1000}], "SELECT 1 AS value"),
    )
    monkeypatch.setattr("app.llm.client.get_llm", lambda: None)

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        user = db.scalar(select(User).where(User.email == "persist-insight@test.com"))
        assert user is not None
        spec = {
            "title": "Persist",
            "widgets": [
                {
                    "id": str(uuid4()),
                    "type": "kpi",
                    "title": "Revenue",
                    "sql": "SELECT 1 AS value",
                    "value": 1000,
                    "rows": [{"value": 1000}],
                },
                {
                    "id": str(uuid4()),
                    "type": "ai_insight",
                    "title": "AI Insights",
                },
            ],
        }
        connection_id = uuid4()
        db.add(
            Connection(
                id=connection_id,
                tenant_id=user.tenant_id,
                name="Insight DB",
                db_type="postgres",
                encrypted_url="x",
            )
        )
        db.flush()
        persisted = create_dashboard_from_spec(db, user, connection_id, spec)
        dash_id = UUID(str(persisted["id"]))
        execute_dashboard_widgets(
            db,
            dash_id,
            connection_id,
            {"db_type": "postgres", "tables": []},
            "postgres",
            user=user,
        )
        insight_widget = db.scalar(
            select(DashboardWidget).where(
                DashboardWidget.dashboard_id == dash_id,
                DashboardWidget.type == "ai_insight",
            )
        )
        assert insight_widget is not None
        config = json.loads(insight_widget.config_json or "{}")
        assert config.get("content")
        assert config.get("facts_hash")
        assert config.get("insight_status")
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
