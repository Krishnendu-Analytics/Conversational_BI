from uuid import UUID

from fastapi.testclient import TestClient

from app.tools.customer_db import ConnectionTestResult


def _stub_provider(ok: bool, message: str = "Connection successful"):
    class StubProvider:
        def test_connection(self) -> ConnectionTestResult:
            return ConnectionTestResult(ok=ok, message=message)

    return StubProvider()


def test_create_mssql_connection(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client

    signup = test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "MSSQL Acme",
            "email": "mssql-create@acme.com",
            "password": "Password@123",
        },
    )
    assert signup.status_code == 201

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={
            "email": "mssql-create@acme.com",
            "otp_code": sent["otp"],
        },
    )
    assert verify.status_code == 200

    headers = {"Authorization": f"Bearer {verify.json()['access_token']}"}

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(ok=True),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Customer MSSQL",
            "db_type": "mssql",
            "connection_url": (
                "mssql://sa:Password%40123"
                "@localhost:1433/customerdb"
            ),
        },
    )

    assert create.status_code == 201

    body = create.json()
    UUID(body["id"])
    assert body["name"] == "Customer MSSQL"
    assert body["db_type"] == "mssql"
    assert "connection_url" not in body
    assert "encrypted_url" not in body

    response = test_client.get("/api/v1/connections", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    connection = data[0]
    assert connection["name"] == "Customer MSSQL"
    assert connection["db_type"] == "mssql"
    assert "connection_url" not in connection
    assert "encrypted_url" not in connection


def test_mssql_invalid_url(
    client: tuple[TestClient, dict[str, str]],
) -> None:
    test_client, sent = client

    signup = test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "Invalid MSSQL Acme",
            "email": "invalidmssql@acme.com",
            "password": "Password@123",
        },
    )
    assert signup.status_code == 201

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={
            "email": "invalidmssql@acme.com",
            "otp_code": sent["otp"],
        },
    )
    assert verify.status_code == 200

    headers = {"Authorization": f"Bearer {verify.json()['access_token']}"}

    response = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Invalid MSSQL",
            "db_type": "mssql",
            "connection_url": (
                "postgresql://customer:customer"
                "@localhost:5433/customerdb"
            ),
        },
    )

    assert response.status_code == 422
