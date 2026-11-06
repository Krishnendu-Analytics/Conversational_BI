"""P0-6: Connection-scoped semantic CRUD."""

from __future__ import annotations

from uuid import uuid4

from app.tools.customer_db import ConnectionTestResult
from tests.test_tenant_isolation import _create_connection, _signup_and_token


def _stub_provider():
    class Stub:
        def test_connection(self) -> ConnectionTestResult:
            return ConnectionTestResult(ok=True, message="ok", read_only_verified=True)

    return Stub()


def test_create_metric_requires_connection_id(client, monkeypatch) -> None:
    test_client, sent = client
    headers, _, _, _ = _signup_and_token(test_client, sent, "sem-admin@test.com")
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: _stub_provider(),
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)

    missing = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={"name": "Revenue", "expression": "SUM(amount)"},
    )
    assert missing.status_code == 422

    ok = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(amount)",
        },
    )
    assert ok.status_code == 201


def test_semantic_catalog_requires_connection_id(client, monkeypatch) -> None:
    test_client, sent = client
    headers, _, _, _ = _signup_and_token(test_client, sent, "sem-user@test.com")
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: _stub_provider(),
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)

    missing = test_client.get("/api/v1/semantic", headers=headers)
    assert missing.status_code == 422
    body = missing.json()
    assert body["detail"][0]["loc"] == ["query", "connection_id"]
    assert body["detail"][0]["type"] == "missing"

    ok = test_client.get(
        f"/api/v1/semantic?connection_id={conn_id}",
        headers=headers,
    )
    assert ok.status_code == 200


def test_metric_crud_isolated_per_connection(client, monkeypatch) -> None:
    test_client, sent = client
    headers, _, _, _ = _signup_and_token(test_client, sent, "sem-iso@test.com")
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: _stub_provider(),
    )
    conn_a = _create_connection(test_client, headers, monkeypatch, name="DB A")
    conn_b = _create_connection(test_client, headers, monkeypatch, name="DB B")

    create_a = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_a),
            "name": "Revenue",
            "expression": "SUM(a.amount)",
        },
    )
    assert create_a.status_code == 201

    list_b = test_client.get(
        f"/api/v1/semantic/metrics?connection_id={conn_b}",
        headers=headers,
    )
    assert list_b.status_code == 200
    assert list_b.json() == []

    list_a = test_client.get(
        f"/api/v1/semantic/metrics?connection_id={conn_a}",
        headers=headers,
    )
    assert len(list_a.json()) == 1
