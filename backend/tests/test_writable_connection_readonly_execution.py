"""Writable DB accounts may connect; the app still rejects mutation SQL."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agent.dashboard import _validate_and_run
from app.agent.workflow import QueryExecutor
from app.tools.customer_db import ConnectionTestResult, PostgreSQLProvider
from app.tools.sql_safety import SQLSafetyPipeline, assert_read_only_sql
import app.agent.workflow as workflow_mod


def _schema() -> dict:
    return {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [{"name": "amount"}, {"name": "order_id"}],
                "foreign_keys": [],
            }
        ],
    }


def _signup_headers(
    test_client: TestClient,
    sent: dict[str, str],
    email: str,
    tenant_name: str = "Acme",
) -> dict[str, str]:
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": tenant_name, "email": email, "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    token = verify.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


MUTATIONS = {
    "INSERT": "INSERT INTO orders (amount) VALUES (1)",
    "UPDATE": "UPDATE orders SET amount = 1",
    "DELETE": "DELETE FROM orders",
    "CREATE": "CREATE TABLE extra (id integer)",
    "ALTER": "ALTER TABLE orders ADD COLUMN extra integer",
    "DROP": "DROP TABLE orders",
    "TRUNCATE": "TRUNCATE TABLE orders",
    "MERGE": (
        "MERGE INTO orders t USING orders s ON t.amount = s.amount "
        "WHEN MATCHED THEN UPDATE SET amount = 1"
    ),
    "GRANT": "GRANT SELECT ON orders TO public",
    "REVOKE": "REVOKE SELECT ON orders FROM public",
}


def test_a_write_capable_connection_can_be_added(client, monkeypatch) -> None:
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "writable-a@acme.com")
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: type(
            "Stub",
            (),
            {
                "test_connection": staticmethod(
                    lambda: ConnectionTestResult(
                        ok=True,
                        message="Connection successful",
                        read_only_verified=False,
                    )
                )
            },
        )(),
    )
    resp = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Writable Customer DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Writable Customer DB"
    assert "connection_url" not in body


def test_b_select_allowed_by_pipeline_and_execution_guard() -> None:
    sql = "SELECT amount FROM orders"
    validated = SQLSafetyPipeline(schema=_schema(), db_type="postgres").validate(sql)
    assert "SELECT" in validated.upper()
    assert_read_only_sql(validated, db_type="postgres")


@pytest.mark.parametrize("kind,sql", list(MUTATIONS.items()), ids=list(MUTATIONS))
def test_c_to_k_mutations_rejected_by_pipeline_and_guard(kind: str, sql: str) -> None:
    with pytest.raises(ValueError):
        SQLSafetyPipeline(schema=_schema(), db_type="postgres").validate(sql)
    with pytest.raises(ValueError):
        assert_read_only_sql(sql, db_type="postgres")
    provider = PostgreSQLProvider("postgresql://unused:unused@localhost/db")
    with pytest.raises(ValueError):
        provider.execute_read_only_query(sql, db_type="postgres")


def test_l_dashboard_execution_cannot_bypass(monkeypatch) -> None:
    called = {"query": False}

    def spy_query(*_a, **_k):
        called["query"] = True
        return []

    monkeypatch.setattr("app.agent.dashboard.run_readonly_query", spy_query)
    with pytest.raises(ValueError):
        _validate_and_run(
            str(uuid4()),
            _schema(),
            "postgres",
            "DELETE FROM orders",
            tenant_id=uuid4(),
        )
    assert called["query"] is False


def test_m_follow_up_query_executor_cannot_bypass(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *_a, **_k: _schema(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        workflow_mod,
        "_recover_sql_after_execution_error",
        lambda **_k: (None, None),
    )

    def spy_run(connection_id: str, sql: str, *, tenant_id=None, **kwargs):
        executed.append(sql)
        return []

    monkeypatch.setattr(workflow_mod, "run_readonly_query", spy_run)

    result = QueryExecutor().run(
        {
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
            "sql": "UPDATE orders SET amount = 0",
        }
    )
    assert executed == []
    assert result.get("rows") == []
    error = (result.get("error") or "").lower()
    assert error


def test_n_write_capable_connection_stays_tenant_scoped(client, monkeypatch) -> None:
    test_client, sent = client
    headers_a = _signup_headers(test_client, sent, "iso-a@acme.com", tenant_name="AcmeIso")
    headers_b = _signup_headers(test_client, sent, "iso-b@other.com", tenant_name="OtherIso")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: type(
            "Stub",
            (),
            {
                "test_connection": staticmethod(
                    lambda: ConnectionTestResult(
                        ok=True, message="ok", read_only_verified=False
                    )
                )
            },
        )(),
    )
    created = test_client.post(
        "/api/v1/connections",
        headers=headers_a,
        json={
            "name": "Tenant A DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert created.status_code == 201
    conn_id = created.json()["id"]

    listed_b = test_client.get("/api/v1/connections", headers=headers_b)
    assert listed_b.status_code == 200
    assert listed_b.json() == []

    other = test_client.get(f"/api/v1/connections/{conn_id}/schema", headers=headers_b)
    assert other.status_code in {403, 404}
