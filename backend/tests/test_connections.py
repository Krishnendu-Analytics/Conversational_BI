from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Connection,
    Conversation,
    Dashboard,
    DashboardWidget,
    Dimension,
    Metric,
    SchemaDiscoveryRun,
    SchemaTable,
    SemanticEntity,
    SemanticRelationship,
    User,
)
from app.services.conversation_service import ensure_conversation
from app.services.dashboard_service import create_dashboard_from_spec
from app.tools.customer_db import ConnectionTestResult


def _open_db(test_client: TestClient) -> tuple[Session, object]:
    override = test_client.app.dependency_overrides[get_db]
    gen = override()
    return next(gen), gen


def _close_db(gen: object) -> None:
    try:
        next(gen)  # type: ignore[call-overload]
    except StopIteration:
        pass


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

    return {"Authorization": f"Bearer {verify.json()['access_token']}"}


def _stub_provider(
    ok: bool,
    message: str = "Connection successful",
    *,
    read_only_verified: bool | None = True,
):
    class StubProvider:
        def test_connection(self) -> ConnectionTestResult:
            return ConnectionTestResult(
                ok=ok,
                message=message,
                read_only_verified=read_only_verified if ok else None,
            )

    return StubProvider()


def test_create_list_connection_password_not_leaked(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "conn@acme.com")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(ok=True),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Customer DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )

    assert create.status_code == 201

    body = create.json()

    UUID(body["id"])
    assert body["name"] == "Customer DB"
    assert body["db_type"] == "postgres"

    # Password must never be returned
    assert "connection_url" not in body
    assert "encrypted_url" not in body

    response = test_client.get(
        "/api/v1/connections",
        headers=headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1

    connection = data[0]

    assert connection["name"] == "Customer DB"
    assert connection["db_type"] == "postgres"

    assert "connection_url" not in connection
    assert "encrypted_url" not in connection


def test_create_connection_rejects_failed_test(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "fail@acme.com")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(
            ok=False,
            message="Connection failed: OperationalError",
        ),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Bad DB",
            "db_type": "postgres",
            "connection_url": "postgresql://bad:bad@localhost:5433/nowhere",
        },
    )

    assert create.status_code == 400
    assert "Connection failed" in create.json()["detail"]

    listed = test_client.get("/api/v1/connections", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []


def test_create_mysql_connection(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "mysql@acme.com")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(ok=True),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Customer MySQL",
            "db_type": "mysql",
            "connection_url": "mysql://customer:customer@localhost:3307/customerdb",
        },
    )

    assert create.status_code == 201
    body = create.json()
    UUID(body["id"])
    assert body["name"] == "Customer MySQL"
    assert body["db_type"] == "mysql"
    assert "connection_url" not in body
    assert "encrypted_url" not in body

    listed = test_client.get("/api/v1/connections", headers=headers)
    assert listed.status_code == 200
    data = listed.json()
    assert len(data) == 1
    assert data[0]["db_type"] == "mysql"


def test_create_mssql_connection(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "mssql@acme.com")

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
            "connection_url": "mssql://sa:Password%40123@localhost:1433/customerdb",
        },
    )

    assert create.status_code == 201
    body = create.json()
    UUID(body["id"])
    assert body["name"] == "Customer MSSQL"
    assert body["db_type"] == "mssql"
    assert "connection_url" not in body
    assert "encrypted_url" not in body

    listed = test_client.get("/api/v1/connections", headers=headers)
    assert listed.status_code == 200
    data = listed.json()
    assert len(data) == 1
    assert data[0]["db_type"] == "mssql"


def test_test_connection_endpoint_ok(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "test-connection@acme.com")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(ok=True),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Customer DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert create.status_code == 201

    connection_id = create.json()["id"]
    UUID(connection_id)

    response = test_client.post(
        f"/api/v1/connections/{connection_id}/test",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Connection successful"


def test_create_connection_allows_verified_write_capable_user(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "write-user@acme.com")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(
            ok=True, read_only_verified=False
        ),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Writable DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )

    assert create.status_code == 201
    assert create.json()["name"] == "Writable DB"


def test_update_and_delete_connection(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "edit-conn@acme.com")

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(ok=True),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Customer DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert create.status_code == 201
    connection_id = create.json()["id"]

    rename = test_client.patch(
        f"/api/v1/connections/{connection_id}",
        headers=headers,
        json={"name": "Analytics DB"},
    )
    assert rename.status_code == 200
    assert rename.json()["name"] == "Analytics DB"
    assert "encrypted_url" not in rename.json()

    recredential = test_client.patch(
        f"/api/v1/connections/{connection_id}",
        headers=headers,
        json={
            "name": "Analytics DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert recredential.status_code == 200
    assert recredential.json()["db_type"] == "postgres"

    delete = test_client.delete(
        f"/api/v1/connections/{connection_id}",
        headers=headers,
    )
    assert delete.status_code == 204

    listed = test_client.get("/api/v1/connections", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []


def test_delete_connection_cascades_dashboards_and_semantic_rows(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    email = "delete-used-conn@acme.com"
    headers = _signup_and_headers(test_client, sent, email)

    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: _stub_provider(ok=True),
    )

    create = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Analytics DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert create.status_code == 201
    connection_id = UUID(create.json()["id"])

    db, gen = _open_db(test_client)
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        tenant_id = user.tenant_id
        conversation_id = uuid4()
        ensure_conversation(
            db,
            conversation_id=conversation_id,
            user=user,
            connection_id=connection_id,
            title="Used connection chat",
        )
        persisted = create_dashboard_from_spec(
            db,
            user,
            connection_id,
            {
                "title": "Sales",
                "widgets": [
                    {
                        "id": str(uuid4()),
                        "type": "kpi",
                        "title": "Revenue",
                        "sql": "SELECT 1 AS value",
                    }
                ],
            },
            conversation_id=conversation_id,
        )
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.dashboard_id = UUID(persisted["id"])
        db.add(
            SchemaTable(
                tenant_id=tenant_id,
                connection_id=connection_id,
                schema_name="public",
                table_name="orders",
                columns_json="{}",
            )
        )
        db.add(
            SchemaDiscoveryRun(
                tenant_id=tenant_id,
                connection_id=connection_id,
                status="completed",
            )
        )
        db.add(
            Metric(
                tenant_id=tenant_id,
                connection_id=connection_id,
                name="Revenue",
                expression="SUM(orders.amount)",
            )
        )
        db.add(
            Dimension(
                tenant_id=tenant_id,
                connection_id=connection_id,
                name="Region",
                expression="orders.region",
            )
        )
        db.add(
            SemanticEntity(
                tenant_id=tenant_id,
                connection_id=connection_id,
                name="Order",
                source_table="public.orders",
            )
        )
        db.add(
            SemanticRelationship(
                tenant_id=tenant_id,
                connection_id=connection_id,
                from_entity="Order",
                to_entity="Customer",
            )
        )
        db.commit()
        dashboard_id = UUID(persisted["id"])
    finally:
        _close_db(gen)

    deleted = test_client.delete(
        f"/api/v1/connections/{connection_id}",
        headers=headers,
    )
    assert deleted.status_code == 204

    listed = test_client.get("/api/v1/connections", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []

    db, gen = _open_db(test_client)
    try:
        assert db.get(Connection, connection_id) is None
        assert db.get(Dashboard, dashboard_id) is None
        assert (
            db.scalar(select(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard_id))
            is None
        )
        assert db.scalar(select(SchemaTable).where(SchemaTable.connection_id == connection_id)) is None
        assert (
            db.scalar(
                select(SchemaDiscoveryRun).where(SchemaDiscoveryRun.connection_id == connection_id)
            )
            is None
        )
        assert db.scalar(select(Metric).where(Metric.connection_id == connection_id)) is None
        assert db.scalar(select(Dimension).where(Dimension.connection_id == connection_id)) is None
        assert (
            db.scalar(select(SemanticEntity).where(SemanticEntity.connection_id == connection_id))
            is None
        )
        assert (
            db.scalar(
                select(SemanticRelationship).where(
                    SemanticRelationship.connection_id == connection_id
                )
            )
            is None
        )
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.connection_id is None
        assert conversation.dashboard_id is None
        assert conversation.deleted_at is not None
    finally:
        _close_db(gen)

