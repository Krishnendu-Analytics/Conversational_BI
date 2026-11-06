"""Tests for attaching extra connections to a conversation (Phase 4) —
both the REST endpoints and the service-level connection-id resolution the
investigator uses."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.tools.customer_db import ConnectionTestResult


def _signup_headers(
    test_client: TestClient,
    sent: dict[str, str],
    email: str,
    *,
    tenant_name: str = "Acme",
) -> dict[str, str]:
    signup = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": tenant_name, "email": email, "password": "Password@123"},
    )
    assert signup.status_code == 201
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    assert verify.status_code == 200
    token = verify.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_connection(
    test_client: TestClient,
    headers: dict[str, str],
    monkeypatch,
    *,
    name: str = "DB",
) -> str:
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: type(
            "Stub",
            (),
            {"test_connection": staticmethod(lambda: ConnectionTestResult(ok=True, message="ok"))},
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
    return create.json()["id"]


def _create_conversation(
    test_client: TestClient, headers: dict[str, str], *, connection_id: str
) -> str:
    create = test_client.post(
        "/api/v1/conversations",
        headers=headers,
        json={"title": "Chat", "connection_id": connection_id},
    )
    assert create.status_code == 201
    return create.json()["id"]


def test_attach_list_and_detach_connection(client, monkeypatch) -> None:
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "owner@acme.com")
    primary = _create_connection(test_client, headers, monkeypatch, name="Primary")
    secondary = _create_connection(test_client, headers, monkeypatch, name="Secondary")
    conversation_id = _create_conversation(test_client, headers, connection_id=primary)

    empty = test_client.get(
        f"/api/v1/conversations/{conversation_id}/connections", headers=headers
    )
    assert empty.status_code == 200
    assert empty.json() == []

    attach = test_client.post(
        f"/api/v1/conversations/{conversation_id}/connections",
        headers=headers,
        json={"connection_id": secondary},
    )
    assert attach.status_code == 201
    assert attach.json()["connection_id"] == secondary

    listed = test_client.get(
        f"/api/v1/conversations/{conversation_id}/connections", headers=headers
    )
    assert listed.status_code == 200
    assert [row["connection_id"] for row in listed.json()] == [secondary]

    # Idempotent: attaching the same connection again doesn't error or duplicate.
    attach_again = test_client.post(
        f"/api/v1/conversations/{conversation_id}/connections",
        headers=headers,
        json={"connection_id": secondary},
    )
    assert attach_again.status_code == 201
    listed_again = test_client.get(
        f"/api/v1/conversations/{conversation_id}/connections", headers=headers
    )
    assert len(listed_again.json()) == 1

    detach = test_client.delete(
        f"/api/v1/conversations/{conversation_id}/connections/{secondary}", headers=headers
    )
    assert detach.status_code == 204

    listed_after = test_client.get(
        f"/api/v1/conversations/{conversation_id}/connections", headers=headers
    )
    assert listed_after.json() == []


def test_detach_nonexistent_connection_is_404(client, monkeypatch) -> None:
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "owner2@acme.com")
    primary = _create_connection(test_client, headers, monkeypatch, name="Primary")
    conversation_id = _create_conversation(test_client, headers, connection_id=primary)

    resp = test_client.delete(
        f"/api/v1/conversations/{conversation_id}/connections/{uuid4()}", headers=headers
    )
    assert resp.status_code == 404


def test_attach_connection_from_another_tenant_is_404(client, monkeypatch) -> None:
    test_client, sent = client
    headers_a = _signup_headers(test_client, sent, "a@acme.com", tenant_name="AcmeA")
    headers_b = _signup_headers(test_client, sent, "b@other.com", tenant_name="AcmeB")

    primary_a = _create_connection(test_client, headers_a, monkeypatch, name="A Primary")
    other_tenant_conn = _create_connection(test_client, headers_b, monkeypatch, name="B Conn")
    conversation_id = _create_conversation(test_client, headers_a, connection_id=primary_a)

    resp = test_client.post(
        f"/api/v1/conversations/{conversation_id}/connections",
        headers=headers_a,
        json={"connection_id": other_tenant_conn},
    )
    assert resp.status_code == 404


def test_resolve_investigation_connection_ids_service(client, monkeypatch) -> None:
    """Service-level check backing the investigator: falls back to just the
    primary connection with nothing attached, includes extras once attached."""
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "svc@acme.com")
    primary = _create_connection(test_client, headers, monkeypatch, name="Primary")
    secondary = _create_connection(test_client, headers, monkeypatch, name="Secondary")
    conversation_id = _create_conversation(test_client, headers, connection_id=primary)

    from app.database import SessionLocal as RealSessionLocal  # noqa: F401
    from app.main import app as fastapi_app
    from app.database import get_db
    from app.services.conversation_connections_service import (
        attach_connection,
        resolve_investigation_connection_ids,
    )

    db_gen = fastapi_app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        me = test_client.get("/api/v1/auth/me", headers=headers).json()
        tenant_id = UUID(me["tenant_id"])

        only_primary = resolve_investigation_connection_ids(
            db,
            tenant_id=tenant_id,
            conversation_id=UUID(conversation_id),
            primary_connection_id=primary,
        )
        assert only_primary == [primary]

        attach_connection(
            db,
            tenant_id=tenant_id,
            conversation_id=UUID(conversation_id),
            connection_id=UUID(secondary),
        )

        with_extra = resolve_investigation_connection_ids(
            db,
            tenant_id=tenant_id,
            conversation_id=UUID(conversation_id),
            primary_connection_id=primary,
        )
        assert with_extra == [primary, secondary]
    finally:
        db.close()
