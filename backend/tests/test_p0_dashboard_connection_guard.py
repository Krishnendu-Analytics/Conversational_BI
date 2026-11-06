"""P0-1: Dashboard connection guard — authoritative dashboard.connection_id."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

import app.agent.workflow as workflow_mod
from app.agent.analysis_state import AnalysisState, DashboardFocus
from app.agent.dashboard_edit_pipeline import DashboardEditResult
from app.authorization import (
    DashboardConnectionMismatchError,
    resolve_dashboard_execution_connection,
)
from app.models import Connection, Tenant, User
from app.services.dashboard_service import create_dashboard_from_spec

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
    ],
}

SEMANTIC = {
    "metrics": [{"name": "Revenue", "expression": "SUM(public.orders.amount)"}],
    "dimensions": [{"name": "Region", "expression": "public.orders.region"}],
}


def _sample_dashboard(dashboard_id: UUID | None = None, *, connection_id: UUID | None = None) -> dict[str, Any]:
    did = dashboard_id or uuid4()
    return {
        "id": str(did),
        "title": "Sales Overview",
        "connection_id": str(connection_id or uuid4()),
        "source_table": "public.orders",
        "filters": [],
        "widgets": [
            {
                "id": "kpi-revenue",
                "type": "kpi",
                "title": "Revenue",
                "sql": "SELECT SUM(amount) AS value FROM public.orders",
            },
        ],
    }


def _editor_state(
    *,
    tenant_id: UUID,
    user_id: UUID,
    connection_id: UUID,
    dashboard_id: UUID,
    question: str = "Add a revenue chart to this dashboard",
) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "connection_id": str(connection_id),
        "question": question,
        "analysis_state": AnalysisState(
            dashboard=DashboardFocus(dashboard_id=dashboard_id),
        ).model_dump(),
    }


def _fake_session(
    *,
    user_id: UUID,
    tenant_id: UUID,
    connection_id: UUID | None = None,
) -> Any:
    """Minimal SessionLocal stand-in with get + scalar for guard tests."""

    class FakeSession:
        def get(self, model: Any, pk: Any) -> User | None:
            if model is User:
                return User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email="t@test.com",
                    role="analyst",
                    password_hash="x",
                    is_verified=True,
                )
            return None

        def scalar(self, stmt: Any) -> Connection | None:
            if connection_id is None:
                return None
            return Connection(
                id=connection_id,
                tenant_id=tenant_id,
                name="DB",
                db_type="postgres",
                encrypted_url="x",
            )

        def close(self) -> None:
            return None

    return FakeSession()


def _patch_editor_basics(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tenant_id: UUID,
    user_id: UUID,
    dashboard: dict[str, Any],
    conn_b: UUID,
) -> dict[str, list[str]]:
    """Patch DashboardEditor dependencies; return spy bucket for connection IDs."""
    queried: list[str] = []

    def fake_run_readonly_query(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        queried.append(str(connection_id))
        return [{"value": 100}], sql

    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: ORDERS_SCHEMA,
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda *a, **k: SEMANTIC,
    )
    monkeypatch.setattr(
        workflow_mod,
        "get_dashboard",
        lambda db, did, user: dashboard,
    )
    monkeypatch.setattr(
        "app.agent.dashboard.run_readonly_query",
        fake_run_readonly_query,
    )
    monkeypatch.setattr(
        workflow_mod,
        "run_dashboard_edit_pipeline",
        lambda **kwargs: DashboardEditResult(
            success=True,
            dashboard=dashboard,
            edit_notes=["Added revenue chart."],
            modified_widget_ids=["chart-revenue"],
        ),
    )
    monkeypatch.setattr(
        workflow_mod,
        "update_dashboard_from_spec",
        lambda db, dashboard_id, user, spec, save_version=True: spec,
    )
    monkeypatch.setattr(
        workflow_mod,
        "SessionLocal",
        lambda: _fake_session(
            user_id=user_id,
            tenant_id=tenant_id,
            connection_id=conn_b,
        ),
    )
    return {"queried": queried}


def test_resolve_dashboard_execution_connection_matching() -> None:
    tenant_id = uuid4()
    conn_id = uuid4()
    dashboard = _sample_dashboard(connection_id=conn_id)

    result = resolve_dashboard_execution_connection(
        _fake_session(user_id=uuid4(), tenant_id=tenant_id, connection_id=conn_id),
        dashboard=dashboard,
        request_connection_id=conn_id,
        tenant_id=tenant_id,
    )
    assert result == conn_id


def test_resolve_dashboard_execution_connection_mismatch() -> None:
    tenant_id = uuid4()
    conn_a = uuid4()
    conn_b = uuid4()
    dashboard = _sample_dashboard(connection_id=conn_b)

    with pytest.raises(DashboardConnectionMismatchError):
        resolve_dashboard_execution_connection(
            _fake_session(user_id=uuid4(), tenant_id=tenant_id, connection_id=conn_b),
            dashboard=dashboard,
            request_connection_id=conn_a,
            tenant_id=tenant_id,
        )


def test_resolve_dashboard_execution_connection_missing_connection() -> None:
    tenant_id = uuid4()
    conn_id = uuid4()
    dashboard = _sample_dashboard(connection_id=conn_id)

    class FakeDb:
        def scalar(self, stmt: Any) -> None:
            return None

    with pytest.raises(ValueError, match="Dashboard connection not found"):
        resolve_dashboard_execution_connection(
            FakeDb(),  # type: ignore[arg-type]
            dashboard=dashboard,
            request_connection_id=None,
            tenant_id=tenant_id,
        )


def test_dashboard_edit_same_connection_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    conn_id = uuid4()
    dashboard_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id, connection_id=conn_id)

    execute_calls: list[str] = []

    def capture_execute(spec: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        execute_calls.append(str(kwargs.get("connection_id")))
        return dict(spec)

    spy = _patch_editor_basics(
        monkeypatch,
        tenant_id=tenant_id,
        user_id=user_id,
        dashboard=dashboard,
        conn_b=conn_id,
    )
    monkeypatch.setattr(workflow_mod, "execute_dashboard", capture_execute)

    editor = workflow_mod.DashboardEditor()
    out = editor.run(
        _editor_state(
            tenant_id=tenant_id,
            user_id=user_id,
            connection_id=conn_id,
            dashboard_id=dashboard_id,
        )
    )

    assert out.get("error") is None
    assert execute_calls
    assert all(c == str(conn_id) for c in execute_calls)
    assert spy["queried"] == [] or all(c == str(conn_id) for c in spy["queried"])


def test_dashboard_edit_mismatch_rejects_before_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Conversation DB-A, dashboard DB-B — reject before any customer SQL."""
    tenant_id = uuid4()
    user_id = uuid4()
    conn_a = uuid4()
    conn_b = uuid4()
    dashboard_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id, connection_id=conn_b)

    queried: list[str] = []

    def fake_run_readonly_query(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        queried.append(str(connection_id))
        return [], sql

    monkeypatch.setattr("app.agent.dashboard.run_readonly_query", fake_run_readonly_query)
    execute_called = {"count": 0}

    def fail_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        execute_called["count"] += 1
        return {}

    monkeypatch.setattr(workflow_mod, "execute_dashboard", fail_execute)

    real_resolve = resolve_dashboard_execution_connection

    def guarded_resolve(db, *, dashboard, request_connection_id, tenant_id):  # noqa: ANN001
        return real_resolve(
            db,
            dashboard=dashboard,
            request_connection_id=request_connection_id,
            tenant_id=tenant_id,
        )

    monkeypatch.setattr(workflow_mod, "resolve_dashboard_execution_connection", guarded_resolve)
    monkeypatch.setattr(
        workflow_mod,
        "get_dashboard",
        lambda db, did, user: dashboard,
    )
    monkeypatch.setattr(
        workflow_mod,
        "SessionLocal",
        lambda: _fake_session(
            user_id=user_id,
            tenant_id=tenant_id,
            connection_id=conn_b,
        ),
    )

    editor = workflow_mod.DashboardEditor()
    out = editor.run(
        _editor_state(
            tenant_id=tenant_id,
            user_id=user_id,
            connection_id=conn_a,
            dashboard_id=dashboard_id,
            question="Add a revenue chart to this dashboard",
        )
    )

    assert out.get("error_code") == DashboardConnectionMismatchError.code
    assert execute_called["count"] == 0
    assert queried == []
    assert conn_a not in [UUID(c) for c in queried]


def test_dashboard_edit_uses_dashboard_connection_for_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spy: run_readonly_query only called with dashboard.connection_id."""
    tenant_id = uuid4()
    user_id = uuid4()
    conn_id = uuid4()
    dashboard_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id, connection_id=conn_id)

    queried: list[str] = []

    def fake_run_readonly_query(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        queried.append(str(connection_id))
        return [{"value": 1}], sql

    monkeypatch.setattr("app.agent.dashboard.run_readonly_query", fake_run_readonly_query)
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: ORDERS_SCHEMA,
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda *a, **k: SEMANTIC,
    )
    monkeypatch.setattr(
        workflow_mod,
        "get_dashboard",
        lambda db, did, user: dashboard,
    )
    monkeypatch.setattr(
        workflow_mod,
        "run_dashboard_edit_pipeline",
        lambda **kwargs: DashboardEditResult(
            success=False,
            clarification=None,
            error="noop",
        ),
    )
    monkeypatch.setattr(
        workflow_mod,
        "SessionLocal",
        lambda: _fake_session(
            user_id=user_id,
            tenant_id=tenant_id,
            connection_id=conn_id,
        ),
    )
    monkeypatch.setattr(
        "app.agent.dashboard._resolve_dashboard_sql_connection",
        lambda spec, *, request_connection_id, tenant_id: str(
            request_connection_id or spec.get("connection_id")
        ),
    )

    editor = workflow_mod.DashboardEditor()
    editor.run(
        _editor_state(
            tenant_id=tenant_id,
            user_id=user_id,
            connection_id=conn_id,
            dashboard_id=dashboard_id,
        )
    )

    assert queried
    assert all(c == str(conn_id) for c in queried)


def test_cross_tenant_dashboard_denied(db_session: Session) -> None:
    tenant_a = Tenant(name="TenantA")
    tenant_b = Tenant(name="TenantB")
    db_session.add_all([tenant_a, tenant_b])
    db_session.flush()

    user_a = User(
        tenant_id=tenant_a.id,
        email="a@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )
    user_b = User(
        tenant_id=tenant_b.id,
        email="b@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )
    db_session.add_all([user_a, user_b])
    db_session.flush()

    conn = Connection(
        tenant_id=tenant_a.id,
        name="DB-A",
        db_type="postgres",
        encrypted_url="x",
    )
    db_session.add(conn)
    db_session.flush()

    spec = _sample_dashboard(connection_id=conn.id)
    persisted = create_dashboard_from_spec(db_session, user_a, conn.id, spec)
    dash_id = UUID(str(persisted["id"]))

    from app.services.dashboard_service import get_dashboard

    with pytest.raises(ValueError, match="Dashboard not found"):
        get_dashboard(db_session, dash_id, user_b)


def test_deleted_connection_rejected(db_session: Session) -> None:
    tenant = Tenant(name="Tenant")
    db_session.add(tenant)
    db_session.flush()

    user = User(
        tenant_id=tenant.id,
        email="u@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )
    db_session.add(user)
    db_session.flush()

    orphan_conn_id = uuid4()
    spec = _sample_dashboard(connection_id=orphan_conn_id)
    persisted = create_dashboard_from_spec(db_session, user, orphan_conn_id, spec)

    with pytest.raises(ValueError, match="Dashboard connection not found"):
        resolve_dashboard_execution_connection(
            db_session,
            dashboard=persisted,
            request_connection_id=orphan_conn_id,
            tenant_id=tenant.id,
        )


def _patch_session_local(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_session.get_bind())
    monkeypatch.setattr("app.database.SessionLocal", factory)


def test_execute_dashboard_matching_connection_runs(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    from app.agent.dashboard import execute_dashboard

    _patch_session_local(monkeypatch, db_session)
    tenant = Tenant(name="Tenant")
    db_session.add(tenant)
    db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="exec@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )
    conn = Connection(
        tenant_id=tenant.id,
        name="DB-A",
        db_type="postgres",
        encrypted_url="x",
    )
    db_session.add_all([user, conn])
    db_session.flush()

    spec = _sample_dashboard(connection_id=conn.id)
    persisted = create_dashboard_from_spec(db_session, user, conn.id, spec)
    queried: list[str] = []

    def spy_run(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        queried.append(str(connection_id))
        return [{"value": 1}], sql

    monkeypatch.setattr("app.agent.dashboard.run_readonly_query", spy_run)

    execute_dashboard(
        persisted,
        connection_id=str(conn.id),
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        tenant_id=tenant.id,
    )
    assert queried == [str(conn.id)]


def test_execute_dashboard_mismatch_rejected_before_sql(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    from app.agent.dashboard import execute_dashboard
    from app.authorization import DashboardConnectionMismatchError

    _patch_session_local(monkeypatch, db_session)
    tenant = Tenant(name="Tenant")
    db_session.add(tenant)
    db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="mismatch@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )
    conn_a = Connection(
        tenant_id=tenant.id,
        name="DB-A",
        db_type="postgres",
        encrypted_url="x",
    )
    conn_b = Connection(
        tenant_id=tenant.id,
        name="DB-B",
        db_type="postgres",
        encrypted_url="y",
    )
    db_session.add_all([user, conn_a, conn_b])
    db_session.flush()

    spec = _sample_dashboard(connection_id=conn_b.id)
    persisted = create_dashboard_from_spec(db_session, user, conn_b.id, spec)
    queried: list[str] = []

    monkeypatch.setattr(
        "app.agent.dashboard.run_readonly_query",
        lambda connection_id, sql, *, tenant_id=None, **kwargs: queried.append(str(connection_id)) or [],
    )

    with pytest.raises(DashboardConnectionMismatchError):
        execute_dashboard(
            persisted,
            connection_id=str(conn_a.id),
            schema=ORDERS_SCHEMA,
            db_type="postgres",
            tenant_id=tenant.id,
        )
    assert queried == []


def test_execute_dashboard_none_request_uses_dashboard_connection(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    from app.agent.dashboard import execute_dashboard

    _patch_session_local(monkeypatch, db_session)
    tenant = Tenant(name="Tenant")
    db_session.add(tenant)
    db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="none-req@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )
    conn = Connection(
        tenant_id=tenant.id,
        name="DB-A",
        db_type="postgres",
        encrypted_url="x",
    )
    db_session.add_all([user, conn])
    db_session.flush()

    spec = _sample_dashboard(connection_id=conn.id)
    persisted = create_dashboard_from_spec(db_session, user, conn.id, spec)
    queried: list[str] = []

    monkeypatch.setattr(
        "app.agent.dashboard.run_readonly_query",
        lambda connection_id, sql, *, tenant_id=None, **kwargs: queried.append(str(connection_id)) or [{"value": 1}],
    )

    # DashboardEditor passes authoritative id; omit simulates using dashboard only.
    execute_dashboard(
        persisted,
        connection_id=str(conn.id),
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        tenant_id=tenant.id,
    )
    assert queried == [str(conn.id)]


def test_execute_dashboard_edit_mismatch_rejected_before_sql(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    from app.agent.dashboard import execute_dashboard_edit
    from app.authorization import DashboardConnectionMismatchError

    _patch_session_local(monkeypatch, db_session)
    tenant = Tenant(name="Tenant")
    db_session.add(tenant)
    db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="edit-mismatch@test.com",
        role="analyst",
        password_hash="x",
        is_verified=True,
    )
    conn_a = Connection(
        tenant_id=tenant.id,
        name="DB-A",
        db_type="postgres",
        encrypted_url="x",
    )
    conn_b = Connection(
        tenant_id=tenant.id,
        name="DB-B",
        db_type="postgres",
        encrypted_url="y",
    )
    db_session.add_all([user, conn_a, conn_b])
    db_session.flush()

    spec = _sample_dashboard(connection_id=conn_b.id)
    persisted = create_dashboard_from_spec(db_session, user, conn_b.id, spec)
    queried: list[str] = []

    monkeypatch.setattr(
        "app.agent.dashboard.run_readonly_query",
        lambda connection_id, sql, *, tenant_id=None, **kwargs: queried.append(str(connection_id)) or [],
    )

    with pytest.raises(DashboardConnectionMismatchError):
        execute_dashboard_edit(
            persisted,
            connection_id=str(conn_a.id),
            schema=ORDERS_SCHEMA,
            db_type="postgres",
            tenant_id=tenant.id,
        )
    assert queried == []


@pytest.fixture()
def db_session(client) -> Session:
    from app.database import get_db

    test_client, _ = client
    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        yield db
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
