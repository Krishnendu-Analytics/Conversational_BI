"""Dashboard open speed: Redis result cache, the widget cap, and saving
results back to the right widget."""

import json
from uuid import uuid4

import pytest

from app.agent import dashboard as ad
from app.models import Dashboard, DashboardWidget, User


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> bool:
        self.store[key] = value
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake)
    return fake


def test_widget_rows_come_from_cache_until_refresh(monkeypatch, fake_redis) -> None:
    calls: list[str] = []

    def run(connection_id, sql, **_k):
        calls.append(sql)
        return [{"value": 42}]

    monkeypatch.setattr(ad, "run_readonly_query", run)
    monkeypatch.setattr(ad.SQLSafetyPipeline, "validate", lambda self, sql: sql)
    tenant = uuid4()

    first, _ = ad._validate_and_run("c1", {}, "postgres", "SELECT 1", tenant_id=tenant)
    second, _ = ad._validate_and_run("c1", {}, "postgres", "SELECT 1", tenant_id=tenant)
    assert first == second == [{"value": 42}]
    assert len(calls) == 1  # second open served from Redis

    ad._validate_and_run("c1", {}, "postgres", "SELECT 1", tenant_id=tenant, use_cache=False)
    assert len(calls) == 2  # Refresh always re-queries

    ad._validate_and_run("c2", {}, "postgres", "SELECT 1", tenant_id=tenant)
    assert len(calls) == 3  # cache is per connection


def test_cache_disabled_by_zero_ttl(monkeypatch, fake_redis) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DASHBOARD_RESULT_CACHE_SECONDS", "0")
    get_settings.cache_clear()
    try:
        calls = []
        monkeypatch.setattr(ad, "run_readonly_query", lambda *a, **k: calls.append(1) or [])
        monkeypatch.setattr(ad.SQLSafetyPipeline, "validate", lambda self, sql: sql)
        for _ in range(2):
            ad._validate_and_run("c", {}, "postgres", "SELECT 1", tenant_id=uuid4())
        assert len(calls) == 2 and fake_redis.store == {}
    finally:
        get_settings.cache_clear()


def test_cap_limits_queries_but_keeps_widgets_without_sql(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DASHBOARD_MAX_WIDGETS_PER_REFRESH", "2")
    monkeypatch.setenv("DASHBOARD_WIDGET_MAX_CONCURRENCY", "1")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(
            ad, "_execute_dashboard_widget", lambda w, **_k: {**w, "rows": [{"v": 1}]}
        )
        specs = [{"id": f"s{i}", "sql": "SELECT 1"} for i in range(4)] + [
            {"id": "insight", "type": "ai_insight"}
        ]
        out = ad._execute_dashboard_widgets_bounded(
            specs, sql_connection_id="c", schema={}, db_type="postgres", tenant_id=uuid4(),
            filters=[], semantic=None, source_table=None, dashboard_id="d",
        )
        # Only 2 queries run; the rest stay on the dashboard, marked stale.
        assert [w["id"] for w in out] == ["s0", "s1", "s2", "s3", "insight"]
        assert [bool(w.get("stale")) for w in out] == [False, False, True, True, False]
        assert "rows" not in out[2]  # not executed: keeps whatever it had stored
    finally:
        get_settings.cache_clear()


def test_results_are_saved_to_the_matching_widget(monkeypatch) -> None:
    from app.services import dashboard_service
    from tests.test_helpers_discovery import make_test_db, seed_connection

    db = make_test_db()
    tenant, conn = seed_connection(db)
    user = User(tenant_id=tenant.id, email="d@acme.com", role="admin", password_hash="x")
    db.add(user)
    db.flush()
    dash = Dashboard(tenant_id=tenant.id, user_id=user.id, connection_id=conn.id, name="D")
    db.add(dash)
    db.flush()
    chart = DashboardWidget(dashboard_id=dash.id, tenant_id=tenant.id, type="chart", title="C", sql_template="SELECT 1")
    insight = DashboardWidget(dashboard_id=dash.id, tenant_id=tenant.id, type="ai_insight", title="AI")
    db.add_all([chart, insight])
    db.commit()

    # Execution returns the insight first and drops the chart (pruned):
    # positions no longer line up with the stored rows.
    monkeypatch.setattr(
        dashboard_service,
        "execute_dashboard",
        lambda *_a, **_k: {"widgets": [{"id": str(insight.id), "type": "ai_insight", "content": "Summary", "facts_hash": "h1"}]},
    )
    monkeypatch.setattr(dashboard_service, "load_tenant_semantic_catalog", lambda *a, **k: {})
    dashboard_service.execute_dashboard_widgets(db, dash.id, conn.id, {}, "postgres", user=user)

    db.refresh(chart)
    db.refresh(insight)
    assert json.loads(insight.config_json)["facts_hash"] == "h1"
    assert "facts_hash" not in json.loads(chart.config_json or "{}")
