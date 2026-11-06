"""Cross-tenant and cross-user resource isolation tests."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import User
from app.redis_client import ConversationMemory
from app.security import PasswordManager, TokenManager
from app.tools.customer_db import ConnectionTestResult
from tests.test_conversation_memory import FakeRedis


def _signup_and_token(
    test_client: TestClient,
    sent: dict[str, str],
    email: str,
    *,
    tenant_name: str = "Acme",
) -> tuple[dict[str, str], str, UUID, UUID]:
    signup = test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": tenant_name,
            "email": email,
            "password": "Password@123",
        },
    )
    assert signup.status_code == 201

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    assert verify.status_code == 200
    body = verify.json()
    token = body["access_token"]
    return (
        {"Authorization": f"Bearer {token}"},
        token,
        UUID(body["tenant_id"]),
        UUID(body["user_id"]),
    )


def _create_connection(
    test_client: TestClient,
    headers: dict[str, str],
    monkeypatch,
    *,
    name: str = "Customer DB",
) -> UUID:
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
            "name": name,
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert create.status_code == 201
    return UUID(create.json()["id"])


def _create_conversation(
    test_client: TestClient,
    headers: dict[str, str],
    *,
    connection_id: UUID | None = None,
    title: str = "Private chat",
) -> UUID:
    payload: dict = {"title": title}
    if connection_id is not None:
        payload["connection_id"] = str(connection_id)
    create = test_client.post(
        "/api/v1/conversations",
        headers=headers,
        json=payload,
    )
    assert create.status_code == 201
    return UUID(create.json()["id"])


def _create_dashboard(
    test_client: TestClient,
    headers: dict[str, str],
    *,
    connection_id: UUID,
    name: str = "Private dashboard",
) -> UUID:
    create = test_client.post(
        "/api/v1/dashboards",
        headers=headers,
        json={
            "name": name,
            "connection_id": str(connection_id),
            "spec": {"title": name, "widgets": []},
        },
    )
    assert create.status_code == 201
    return UUID(create.json()["id"])


def _add_colleague_in_tenant(
    test_client: TestClient,
    *,
    tenant_id: UUID,
    email: str,
    password: str = "Password@123",
) -> tuple[dict[str, str], UUID]:
    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db: Session = next(gen)
    try:
        user = User(
            tenant_id=tenant_id,
            email=email,
            role="analyst",
            password_hash=PasswordManager.hash(password),
            is_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = TokenManager.create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
        )
        return {"Authorization": f"Bearer {token}"}, user.id
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


@pytest.fixture()
def tenant_pair(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> dict:
    test_client, sent = client
    headers_a, _token_a, tenant_a, user_a = _signup_and_token(
        test_client, sent, "tenant-a@acme.com", tenant_name="Tenant A"
    )
    headers_b, _token_b, tenant_b, user_b = _signup_and_token(
        test_client, sent, "tenant-b@other.com", tenant_name="Tenant B"
    )
    conn_a = _create_connection(test_client, headers_a, monkeypatch, name="Conn A")
    conn_b = _create_connection(test_client, headers_b, monkeypatch, name="Conn B")
    conv_a = _create_conversation(test_client, headers_a, connection_id=conn_a)
    conv_b = _create_conversation(test_client, headers_b, connection_id=conn_b)
    dash_a = _create_dashboard(test_client, headers_a, connection_id=conn_a)
    dash_b = _create_dashboard(test_client, headers_b, connection_id=conn_b)
    return {
        "client": test_client,
        "headers_a": headers_a,
        "headers_b": headers_b,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "user_a": user_a,
        "user_b": user_b,
        "conn_a": conn_a,
        "conn_b": conn_b,
        "conv_a": conv_a,
        "conv_b": conv_b,
        "dash_a": dash_a,
        "dash_b": dash_b,
    }


def test_conversation_cross_tenant_denied(tenant_pair: dict) -> None:
    client = tenant_pair["client"]
    assert client.get(
        f"/api/v1/conversations/{tenant_pair['conv_a']}",
        headers=tenant_pair["headers_b"],
    ).status_code == 404
    assert client.patch(
        f"/api/v1/conversations/{tenant_pair['conv_a']}",
        headers=tenant_pair["headers_b"],
        json={"title": "stolen"},
    ).status_code == 404
    assert client.delete(
        f"/api/v1/conversations/{tenant_pair['conv_a']}",
        headers=tenant_pair["headers_b"],
    ).status_code == 404


def test_conversation_context_no_redis_fallback(
    tenant_pair: dict,
    monkeypatch,
) -> None:
    client = tenant_pair["client"]
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake_redis)

    orphan_id = uuid4()
    memory = ConversationMemory()
    memory.set(
        tenant_pair["tenant_a"],
        orphan_id,
        last_sql="SELECT secret FROM leaked_table",
        messages=[{"role": "assistant", "content": "secret answer"}],
    )

    resp = client.get(
        f"/api/v1/conversations/{orphan_id}/context",
        headers=tenant_pair["headers_a"],
    )
    assert resp.status_code == 404
    assert "secret" not in resp.text.lower()


def test_conversation_context_cross_tenant_denied(tenant_pair: dict) -> None:
    client = tenant_pair["client"]
    resp = client.get(
        f"/api/v1/conversations/{tenant_pair['conv_a']}/context",
        headers=tenant_pair["headers_b"],
    )
    assert resp.status_code == 404
    assert "secret" not in resp.text.lower()


def test_dashboard_cross_tenant_denied(tenant_pair: dict) -> None:
    client = tenant_pair["client"]
    assert client.get(
        f"/api/v1/dashboards/{tenant_pair['dash_a']}",
        headers=tenant_pair["headers_b"],
    ).status_code == 404
    assert client.post(
        f"/api/v1/dashboards/{tenant_pair['dash_a']}/refresh",
        headers=tenant_pair["headers_b"],
    ).status_code == 404
    assert client.get(
        f"/api/v1/dashboards/{tenant_pair['dash_a']}/versions",
        headers=tenant_pair["headers_b"],
    ).status_code == 404


def test_dashboard_cross_user_same_tenant_denied(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers_a, _token, tenant_id, _user_a = _signup_and_token(
        test_client, sent, "owner@shared.com", tenant_name="Shared Tenant"
    )
    conn = _create_connection(test_client, headers_a, monkeypatch)
    dash = _create_dashboard(test_client, headers_a, connection_id=conn)
    headers_b, _user_b = _add_colleague_in_tenant(
        test_client,
        tenant_id=tenant_id,
        email="colleague@shared.com",
    )

    assert test_client.get(
        f"/api/v1/dashboards/{dash}",
        headers=headers_b,
    ).status_code == 404


def test_dashboard_filter_patch_cross_user_denied(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers_a, _token, tenant_id, _user_a = _signup_and_token(
        test_client, sent, "filter-owner@shared.com", tenant_name="Filter Tenant"
    )
    conn = _create_connection(test_client, headers_a, monkeypatch)
    dash = _create_dashboard(test_client, headers_a, connection_id=conn)
    headers_b, _user_b = _add_colleague_in_tenant(
        test_client,
        tenant_id=tenant_id,
        email="filter-colleague@shared.com",
    )

    patch = test_client.patch(
        f"/api/v1/dashboards/{dash}",
        headers=headers_b,
        json={"filters": [{"field": "Region", "operator": "eq", "value": "West"}]},
    )
    assert patch.status_code == 404


def test_connection_cross_tenant_denied(tenant_pair: dict) -> None:
    client = tenant_pair["client"]
    assert client.get(
        f"/api/v1/connections/{tenant_pair['conn_a']}/schema",
        headers=tenant_pair["headers_b"],
    ).status_code == 404
    assert client.post(
        f"/api/v1/connections/{tenant_pair['conn_a']}/test",
        headers=tenant_pair["headers_b"],
    ).status_code == 404


def test_semantic_metric_cross_tenant_denied(tenant_pair: dict) -> None:
    client = tenant_pair["client"]
    create = client.post(
        "/api/v1/semantic/metrics",
        headers=tenant_pair["headers_a"],
        json={
            "connection_id": str(tenant_pair["conn_a"]),
            "name": "Revenue",
            "expression": "SUM(orders.amount)",
        },
    )
    assert create.status_code == 201
    metric_id = create.json()["id"]

    assert client.get(
        f"/api/v1/semantic/metrics/{metric_id}",
        headers=tenant_pair["headers_b"],
    ).status_code == 404
