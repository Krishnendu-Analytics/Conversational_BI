"""Tests for discover_schema — save introspected tables into schema_tables."""

from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.encryption import CredentialEncryption
from app.models import Connection, SchemaTable, Tenant
from app.tools.customer_db import discover_schema


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _fake_provider(
    fake_tables: dict,
    *,
    schemas: list[str] | None = None,
    default: str = "public",
):
    schema_list = schemas or [default]

    class FakeProvider:
        def list_schemas(self) -> list[str]:
            return list(schema_list)

        def default_schema(self) -> str:
            return default

        def introspect_schema(self, schemas: list[str] | None = None) -> dict:
            target = set(schemas or [default])
            tables = []
            for table in fake_tables.get("tables", []):
                schema_name = table.get("schema_name", default)
                if schema_name not in target:
                    continue
                tables.append({**table, "schema_name": schema_name})
            return {"tables": tables}

    return FakeProvider()


def test_discover_schema_saves_tables_and_drops_stale(monkeypatch) -> None:
    db = _make_db()
    encryptor = CredentialEncryption()

    tenant = Tenant(name="Acme")
    db.add(tenant)
    db.flush()

    conn = Connection(
        tenant_id=tenant.id,
        name="Customer DB",
        db_type="postgres",
        encrypted_url=encryptor.encrypt(
            "postgresql://reader:reader@localhost:5433/customerdb"
        ),
    )
    db.add(conn)
    db.commit()

    # Pretend an old table was cached earlier (should be deleted).
    db.add(
        SchemaTable(
            tenant_id=tenant.id,
            connection_id=conn.id,
            table_name="old_table",
            columns_json='{"columns": [], "foreign_keys": []}',
        )
    )
    db.commit()

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
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "order_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    },
                    {
                        "name": "customer_id",
                        "data_type": "integer",
                        "nullable": True,
                        "is_pk": False,
                    },
                ],
                "foreign_keys": [
                    {
                        "column": "customer_id",
                        "referred_table": "customers",
                        "referred_column": "customer_id",
                    }
                ],
            },
        ]
    }

    monkeypatch.setattr(
        "app.tools.customer_db.ProviderFactory.create",
        lambda _db_type, _url: _fake_provider(fake_tables),
    )

    result = discover_schema(db, conn.id)

    assert result["connection_id"] == conn.id
    assert len(result["tables"]) == 2
    assert result["schemas_used"] == ["public"]

    rows = db.scalars(
        select(SchemaTable).where(SchemaTable.connection_id == conn.id)
    ).all()
    names = {row.table_name for row in rows}
    assert names == {"customers", "orders"}
    assert "old_table" not in names

    orders = next(row for row in rows if row.table_name == "orders")
    assert "customer_id" in orders.columns_json
    assert "referred_table" in orders.columns_json


def test_discover_schema_selected_multi_schema(monkeypatch) -> None:
    db = _make_db()
    encryptor = CredentialEncryption()
    tenant = Tenant(name="Acme")
    db.add(tenant)
    db.flush()
    conn = Connection(
        tenant_id=tenant.id,
        name="Multi",
        db_type="postgres",
        encrypted_url=encryptor.encrypt(
            "postgresql://reader:reader@localhost:5433/customerdb"
        ),
    )
    db.add(conn)
    db.commit()

    fake_tables = {
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {"name": "id", "data_type": "integer", "nullable": False}
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "sales",
                "table_name": "deals",
                "columns": [
                    {"name": "amount", "data_type": "numeric", "nullable": True}
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "hr",
                "table_name": "employees",
                "columns": [
                    {"name": "name", "data_type": "text", "nullable": True}
                ],
                "foreign_keys": [],
            },
        ]
    }

    monkeypatch.setattr(
        "app.tools.customer_db.ProviderFactory.create",
        lambda _db_type, _url: _fake_provider(
            fake_tables, schemas=["public", "sales", "hr"]
        ),
    )

    result = discover_schema(
        db, conn.id, scope="selected", schemas=["sales", "hr"]
    )
    assert result["schemas_used"] == ["sales", "hr"]
    names = {(t["schema_name"], t["table_name"]) for t in result["tables"]}
    assert names == {("sales", "deals"), ("hr", "employees")}

    rows = db.scalars(
        select(SchemaTable).where(SchemaTable.connection_id == conn.id)
    ).all()
    assert {(row.schema_name, row.table_name) for row in rows} == {
        ("sales", "deals"),
        ("hr", "employees"),
    }


def test_discover_schema_missing_connection() -> None:
    db = _make_db()
    try:
        discover_schema(db, uuid4())
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not found" in str(exc).lower()
