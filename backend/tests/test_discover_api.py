from uuid import UUID

from fastapi.testclient import TestClient

from app.tools.customer_db import ConnectionTestResult


def _signup_and_headers(
    test_client: TestClient,
    sent: dict[str, str],
    email: str,
) -> dict[str, str]:
    signup = test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "Acme",
            "email": email,
            "password": "Password@123",
        },
    )
    assert signup.status_code == 201

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={
            "email": email,
            "otp_code": sent["otp"],
        },
    )
    assert verify.status_code == 200

    return {
        "Authorization": f"Bearer {verify.json()['access_token']}"
    }


def _stub_provider(fake_tables: dict):
    class StubProvider:
        def test_connection(self):
            return ConnectionTestResult(
                ok=True,
                message="Connection successful",
            )

        def list_schemas(self):
            names = {
                t.get("schema_name", "public")
                for t in fake_tables.get("tables", [])
            }
            return sorted(names) or ["public"]

        def default_schema(self):
            return "public"

        def introspect_schema(self, schemas=None):
            target = set(schemas or ["public"])
            tables = []
            for table in fake_tables.get("tables", []):
                schema_name = table.get("schema_name", "public")
                if schema_name not in target:
                    continue
                tables.append({**table, "schema_name": schema_name})
            return {"tables": tables}

        def execute_read_only_query(self, sql: str):
            return [{"cnt": 0}]

    return StubProvider


def _patch_provider(monkeypatch, fake_tables: dict) -> None:
    stub = lambda _db_type, _url: _stub_provider(fake_tables)()
    monkeypatch.setattr("app.routes.connections.ProviderFactory.create", stub)
    monkeypatch.setattr("app.tools.customer_db.ProviderFactory.create", stub)
    monkeypatch.setattr(
        "app.services.schema_discovery_service.ProviderFactory.create", stub
    )


def test_discover_schema_endpoint(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client

    headers = _signup_and_headers(
        test_client,
        sent,
        "discover@acme.com",
    )

    fake_tables = {
        "tables": [
            {
                "schema_name": "public",
                "table_name": "customers",
                "columns": [
                    {
                        "name": "customer_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    }
                ],
                "foreign_keys": [],
            }
        ]
    }

    _patch_provider(monkeypatch, fake_tables)

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Customer DB",
            "db_type": "postgres",
            "connection_url": "postgresql://reader:reader@localhost:5433/customerdb",
        },
    )

    assert create.status_code == 201

    connection_id = create.json()["id"]
    UUID(connection_id)

    response = test_client.post(
        f"/api/v1/connections/{connection_id}/discover",
        headers=headers,
    )

    assert response.status_code == 200

    body = response.json()

    assert body["connection_id"] == connection_id
    assert len(body["tables"]) == 1
    assert body["tables"][0]["table_name"] == "customers"
    assert body["tables"][0]["schema_name"] == "public"
    assert body["schemas_used"] == ["public"]


def test_list_schemas_endpoint(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "schemas-list@acme.com")

    fake_tables = {
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "order_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    }
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "sales",
                "table_name": "deals",
                "columns": [
                    {
                        "name": "amount",
                        "data_type": "numeric",
                        "nullable": True,
                        "is_pk": False,
                    }
                ],
                "foreign_keys": [],
            },
        ]
    }

    _patch_provider(monkeypatch, fake_tables)

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Multi DB",
            "db_type": "postgres",
            "connection_url": "postgresql://reader:reader@localhost:5433/customerdb",
        },
    )
    assert create.status_code == 201
    connection_id = create.json()["id"]

    listed = test_client.get(
        f"/api/v1/connections/{connection_id}/schemas",
        headers=headers,
    )
    assert listed.status_code == 200
    body = listed.json()
    assert body["connection_id"] == connection_id
    assert body["default_schema"] == "public"
    assert body["schemas"] == ["public", "sales"]

    discover = test_client.post(
        f"/api/v1/connections/{connection_id}/discover",
        headers=headers,
        json={"scope": "all"},
    )
    assert discover.status_code == 200
    assert set(discover.json()["schemas_used"]) == {"public", "sales"}
    assert len(discover.json()["tables"]) == 2


def test_get_schema_after_discover(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    """T-35: GET /connections/{id}/schema returns persisted discovery."""
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "schema-get@acme.com")

    fake_tables = {
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "order_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    }
                ],
                "foreign_keys": [],
            }
        ]
    }

    _patch_provider(monkeypatch, fake_tables)

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Orders DB",
            "db_type": "postgres",
            "connection_url": "postgresql://reader:reader@localhost:5433/customerdb",
        },
    )
    assert create.status_code == 201
    connection_id = create.json()["id"]

    # Create auto-discovers when the provider succeeds.
    after_create = test_client.get(
        f"/api/v1/connections/{connection_id}/schema",
        headers=headers,
    )
    assert after_create.status_code == 200
    assert len(after_create.json()["tables"]) == 1

    discover = test_client.post(
        f"/api/v1/connections/{connection_id}/discover",
        headers=headers,
    )
    assert discover.status_code == 200

    listed = test_client.get(
        f"/api/v1/connections/{connection_id}/schema",
        headers=headers,
    )
    assert listed.status_code == 200
    body = listed.json()
    assert body["connection_id"] == connection_id
    assert len(body["tables"]) == 1
    assert body["tables"][0]["table_name"] == "orders"
    assert body["tables"][0]["schema_name"] == "public"
