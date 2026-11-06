"""Dashboard filter validation and SQL application tests."""

from __future__ import annotations

from uuid import UUID, uuid4

import sqlglot

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.dashboard import execute_dashboard
from app.agent.dashboard_filters import (
    apply_dashboard_filters,
    apply_filters_to_sql,
    validate_dashboard_filters,
)
from app.database import get_db
from app.main import app
from app.models import DashboardWidget, Dimension, User
from app.tools.customer_db import ConnectionTestResult


ORDERS_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "schema_name": "public",
            "table_name": "orders",
            "columns": [
                {"name": "order_id", "data_type": "integer"},
                {"name": "region", "data_type": "varchar"},
                {"name": "amount", "data_type": "numeric"},
            ],
        },
        {
            "schema_name": "public",
            "table_name": "customers",
            "columns": [
                {"name": "customer_id", "data_type": "integer"},
                {"name": "name", "data_type": "varchar"},
            ],
        },
    ],
}

SEMANTIC = {
    "metrics": [],
    "dimensions": [
        {"name": "Region", "expression": "public.orders.region"},
    ],
}


def test_apply_filter_changes_sql() -> None:
    base = "SELECT COUNT(*) AS value FROM public.orders"
    filtered = apply_dashboard_filters(
        base,
        filters=[{"field": "Region", "operator": "eq", "value": "West"}],
        semantic=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        source_table="public.orders",
    )
    assert "WHERE" in filtered.upper()
    assert "West" in filtered
    assert "public.orders.region" in filtered.lower() or "region" in filtered.lower()


def test_filter_value_sql_injection_escaped() -> None:
    base = "SELECT COUNT(*) AS value FROM public.orders"
    filtered = apply_dashboard_filters(
        base,
        filters=[
            {
                "field": "Region",
                "operator": "eq",
                "value": "West'; DROP TABLE orders;--",
            }
        ],
        semantic=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        source_table="public.orders",
    )
    assert "= 'West''; DROP TABLE orders;--'" in filtered or "='West''" in filtered.replace(" ", "")


def test_unknown_dimension_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown dimension"):
        validate_dashboard_filters(
            [{"field": "NotADimension", "operator": "eq", "value": "X"}],
            SEMANTIC,
        )


def test_non_applicable_filter_skipped() -> None:
    base = "SELECT COUNT(*) AS value FROM public.customers"
    filtered = apply_dashboard_filters(
        base,
        filters=[{"field": "Region", "operator": "eq", "value": "West"}],
        semantic=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        source_table="public.customers",
    )
    assert filtered == base


def test_filter_changes_mocked_results(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        calls.append(sql)
        if "West" in sql:
            return [{"value": 10}]
        return [{"value": 99}]

    monkeypatch.setattr("app.agent.dashboard.run_readonly_query", fake_run)

    spec = {
        "source_table": "public.orders",
        "filters": [{"field": "Region", "operator": "eq", "value": "West"}],
        "widgets": [
            {
                "id": "kpi-1",
                "type": "kpi",
                "title": "Total",
                "sql": "SELECT COUNT(*) AS value FROM public.orders",
            }
        ],
    }

    west = execute_dashboard(
        spec,
        connection_id="conn-1",
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        tenant_id=uuid4(),
        semantic=SEMANTIC,
    )
    assert west["widgets"][0]["value"] == 10
    assert len(calls) == 1
    assert "West" in calls[0]

    spec["filters"] = [{"field": "Region", "operator": "eq", "value": "East"}]
    calls.clear()
    east = execute_dashboard(
        spec,
        connection_id="conn-1",
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        tenant_id=uuid4(),
        semantic=SEMANTIC,
    )
    assert east["widgets"][0]["value"] == 99
    assert "East" in calls[0]


def test_apply_filters_to_sql_merges_existing_where() -> None:
    base = "SELECT COUNT(*) AS value FROM public.orders WHERE amount IS NOT NULL"
    result = apply_filters_to_sql(
        base,
        ["public.orders.region = 'West'"],
        "postgres",
    )
    parsed = sqlglot.parse_one(result, read="postgres")
    where_sql = parsed.args["where"].sql(dialect="postgres").upper()
    assert "IS NULL" in where_sql
    assert "NOT" in where_sql
    assert "REGION" in where_sql
    assert "WEST" in where_sql


def _signup_headers(
    test_client: TestClient,
    sent: dict[str, str],
    email: str,
) -> dict[str, str]:
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": email, "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    return {"Authorization": f"Bearer {verify.json()['access_token']}"}


def test_sql_template_not_mutated_on_refresh(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "filter-template@acme.com")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: type(
            "Stub",
            (),
            {
                "test_connection": staticmethod(
                    lambda: ConnectionTestResult(ok=True, message="ok")
                )
            },
        )(),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "DB",
            "db_type": "postgres",
            "connection_url": "postgresql://u:p@localhost:5432/db",
        },
    )
    assert create.status_code == 201
    connection_id = create.json()["id"]

    base_sql = "SELECT COUNT(*) AS value FROM public.orders"
    dash = test_client.post(
        "/api/v1/dashboards",
        headers=headers,
        json={
            "name": "Orders",
            "connection_id": connection_id,
            "spec": {
                "title": "Orders",
                "filters": [{"field": "Region", "operator": "eq", "value": "West"}],
                "widgets": [
                    {
                        "id": str(uuid4()),
                        "type": "kpi",
                        "title": "Total",
                        "sql": base_sql,
                    }
                ],
            },
        },
    )
    assert dash.status_code == 201
    dashboard_id = dash.json()["id"]

    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db = next(gen)
    try:
        user = db.scalar(select(User).where(User.email == "filter-template@acme.com"))
        assert user is not None
        db.add(
            Dimension(
                tenant_id=user.tenant_id,
                name="Region",
                expression="public.orders.region",
            )
        )
        db.commit()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    monkeypatch.setattr(
        "app.agent.dashboard.run_readonly_query",
        lambda *_a, **_k: [{"value": 1}],
    )
    monkeypatch.setattr(
        "app.tools.customer_db.get_stored_schema",
        lambda _db, _cid: ORDERS_SCHEMA,
    )

    refreshed = test_client.post(
        f"/api/v1/dashboards/{dashboard_id}/refresh",
        headers=headers,
    )
    assert refreshed.status_code == 200

    gen2 = override()
    db2 = next(gen2)
    try:
        widget = db2.scalar(
            select(DashboardWidget).where(
                DashboardWidget.dashboard_id == UUID(dashboard_id)
            )
        )
        assert widget is not None
        assert widget.sql_template == base_sql
    finally:
        try:
            next(gen2)
        except StopIteration:
            pass
