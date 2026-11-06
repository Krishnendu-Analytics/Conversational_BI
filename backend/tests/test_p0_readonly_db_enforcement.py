"""P0-4: Customer DB read-only verification at connection test."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.tools.customer_db import ConnectionTestResult


def _signup_headers(test_client: TestClient, sent: dict[str, str], email: str) -> dict[str, str]:
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": email, "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    return {"Authorization": f"Bearer {verify.json()['access_token']}"}


def _stub(read_only_verified: bool | None) -> object:
    class Stub:
        def test_connection(self) -> ConnectionTestResult:
            return ConnectionTestResult(
                ok=True,
                message="Connection successful",
                read_only_verified=read_only_verified,
            )

    return Stub()


def test_verified_read_only_allows_create(client, monkeypatch) -> None:
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "readonly@acme.com")
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: _stub(True),
    )
    resp = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Read DB",
            "db_type": "postgres",
            "connection_url": "postgresql://ro:ro@localhost/db",
        },
    )
    assert resp.status_code == 201


def test_verified_write_allows_create(client, monkeypatch) -> None:
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "write@acme.com")
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: _stub(False),
    )
    resp = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Write DB",
            "db_type": "postgres",
            "connection_url": "postgresql://rw:rw@localhost/db",
        },
    )
    assert resp.status_code == 201


def test_unknown_readonly_allows_with_warning(client, monkeypatch) -> None:
    test_client, sent = client
    headers = _signup_headers(test_client, sent, "unknown@acme.com")
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _t, _u: _stub(None),
    )
    resp = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Unknown DB",
            "db_type": "postgres",
            "connection_url": "postgresql://u:u@localhost/db",
        },
    )
    assert resp.status_code == 201


def test_postgres_inconclusive_inspection_returns_none(monkeypatch) -> None:
    from app.tools.customer_db import PostgreSQLProvider

    class BrokenInspect(PostgreSQLProvider):
        def _inspect_postgres_read_only(self, cur) -> bool | None:  # noqa: ANN001
            return None

    provider = BrokenInspect("postgresql://localhost/db")
    monkeypatch.setattr(
        provider,
        "test_connection",
        lambda: ConnectionTestResult(True, "Connection successful", None),
    )
    result = provider.test_connection()
    assert result.read_only_verified is None
