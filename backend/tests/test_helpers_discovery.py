"""Shared stubs for schema discovery and semantic pipeline tests."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.encryption import CredentialEncryption
from app.models import Connection, Tenant
from app.tools.customer_db import ConnectionTestResult


def make_test_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def seed_connection(
    db: Session,
    *,
    tenant_name: str = "Acme",
    conn_name: str = "Customer DB",
    db_type: str = "postgres",
    tenant: Tenant | None = None,
) -> tuple[Tenant, Connection]:
    if tenant is None:
        tenant = Tenant(name=tenant_name)
        db.add(tenant)
        db.flush()
    encryptor = CredentialEncryption()
    conn = Connection(
        tenant_id=tenant.id,
        name=conn_name,
        db_type=db_type,
        encrypted_url=encryptor.encrypt(
            "postgresql://reader:reader@localhost:5433/customerdb"
        ),
    )
    db.add(conn)
    db.commit()
    db.refresh(tenant)
    db.refresh(conn)
    return tenant, conn


def multi_schema_tables() -> dict:
    """acct_us + billing_eu with duplicate bare table names and cross-schema FK."""
    return {
        "tables": [
            {
                "schema_name": "acct_us",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "order_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    },
                    {
                        "name": "revenue_amt",
                        "data_type": "numeric",
                        "nullable": False,
                        "is_pk": False,
                    },
                    {
                        "name": "invoice_id",
                        "data_type": "integer",
                        "nullable": True,
                        "is_pk": False,
                    },
                ],
                "foreign_keys": [
                    {
                        "column": "invoice_id",
                        "referred_schema": "billing_eu",
                        "referred_table": "invoices",
                        "referred_column": "invoice_id",
                    }
                ],
            },
            {
                "schema_name": "billing_eu",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "order_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    },
                    {
                        "name": "note",
                        "data_type": "varchar",
                        "nullable": True,
                        "is_pk": False,
                    },
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "billing_eu",
                "table_name": "invoices",
                "columns": [
                    {
                        "name": "invoice_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    },
                    {
                        "name": "region",
                        "data_type": "varchar",
                        "nullable": True,
                        "is_pk": False,
                    },
                ],
                "foreign_keys": [],
            },
        ]
    }


class StubMultiSchemaProvider:
    """Provider stub for acct_us / billing_eu without public or dbo."""

    def __init__(
        self,
        fake_tables: dict,
        *,
        deny_schemas: set[str] | None = None,
    ) -> None:
        self._tables = fake_tables
        self._deny = deny_schemas or set()

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(ok=True, message="Connection successful")

    def list_schemas(self) -> list[str]:
        names = {
            t.get("schema_name")
            for t in self._tables.get("tables", [])
            if t.get("schema_name")
        }
        return sorted(names)

    def default_schema(self) -> str:
        return sorted(self.list_schemas())[0]

    def introspect_schema(self, schemas: list[str] | None = None) -> dict:
        target = set(schemas or self.list_schemas())
        tables = []
        for table in self._tables.get("tables", []):
            schema_name = table.get("schema_name")
            if schema_name not in target:
                continue
            if schema_name in self._deny:
                raise PermissionError(f"access denied to schema {schema_name}")
            tables.append({**table, "schema_name": schema_name})
        return {"tables": tables}

    def execute_read_only_query(self, sql: str) -> list[dict]:
        return [{"region": "EMEA", "revenue": 1000.0}]


def patch_provider(monkeypatch, provider: StubMultiSchemaProvider) -> None:
    factory = lambda _db_type, _url: provider  # noqa: E731
    monkeypatch.setattr("app.routes.connections.ProviderFactory.create", factory)
    monkeypatch.setattr("app.tools.customer_db.ProviderFactory.create", factory)
    monkeypatch.setattr(
        "app.services.schema_discovery_service.ProviderFactory.create", factory
    )


def disable_semantic_llm(monkeypatch) -> None:
    """Prevent duplicate heuristic + LLM suggestions in tests."""
    monkeypatch.setattr(
        "app.services.semantic_discovery.get_settings",
        lambda: type("Settings", (), {"llm_api_key": ""})(),
    )


def confirm_metric(db: Session, metric_id: UUID) -> None:
    from app.models import Metric

    row = db.get(Metric, metric_id)
    assert row is not None
    row.status = "confirmed"
    db.commit()


def confirm_dimension(db: Session, dimension_id: UUID) -> None:
    from app.models import Dimension

    row = db.get(Dimension, dimension_id)
    assert row is not None
    row.status = "confirmed"
    db.commit()
