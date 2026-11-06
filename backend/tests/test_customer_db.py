"""Unit tests for the DatabaseProvider interface + factory."""

import pymssql
import pymysql
import pytest

from app.schemas import DatabaseType
from app.tools.customer_db import (
    ConnectionTestResult,
    CustomerDBPool,
    MySQLProvider,
    PostgreSQLProvider,
    ProviderFactory,
    SQLServerProvider,
    sqlalchemy_url_for_customer_db,
)


def test_factory_returns_postgres_provider() -> None:
    provider = ProviderFactory.create(
        DatabaseType.postgres,
        "postgresql://customer:customer@localhost:5433/customerdb",
    )
    assert isinstance(provider, PostgreSQLProvider)
    assert provider.connection_url.startswith("postgresql://")


def test_factory_returns_mysql_provider() -> None:
    provider = ProviderFactory.create("mysql", "mysql://user:pass@localhost:3306/db")
    assert isinstance(provider, MySQLProvider)


def test_factory_returns_mssql_provider() -> None:
    provider = ProviderFactory.create("mssql", "mssql://user:pass@localhost:1433/db")
    assert isinstance(provider, SQLServerProvider)


def test_factory_rejects_unknown_db_type() -> None:
    with pytest.raises(ValueError, match="Unsupported database type"):
        ProviderFactory.create("oracle", "oracle://localhost/db")


def test_postgres_test_connection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def execute(self, query: str) -> None:
            assert "SELECT 1" in query.upper()

        def fetchone(self):
            return (1,)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_connect(url: str, **_kwargs):
        assert url.startswith("postgresql://")
        return FakeConn()

    monkeypatch.setattr("app.tools.customer_db.psycopg.connect", fake_connect)

    provider = ProviderFactory.create(
        "postgres",
        "postgresql://customer:customer@localhost:5433/customerdb",
    )

    result = provider.test_connection()
    assert isinstance(result, ConnectionTestResult)
    assert result.ok is True


def test_mysql_test_connection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def execute(self, query: str) -> None:
            assert "SELECT 1" in query.upper()

        def fetchone(self):
            return (1,)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_connect(**kwargs):
        assert kwargs["host"] == "localhost"
        assert kwargs["port"] == 3307
        assert kwargs["user"] == "customer"
        assert kwargs["password"] == "customer"
        assert kwargs["database"] == "customerdb"
        return FakeConn()

    monkeypatch.setattr("app.tools.customer_db.pymysql.connect", fake_connect)

    provider = ProviderFactory.create(
        "mysql",
        "mysql://customer:customer@localhost:3307/customerdb",
    )

    result = provider.test_connection()
    assert isinstance(result, ConnectionTestResult)
    assert result.ok is True
    assert result.message == "Connection successful"


def test_mysql_test_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_connect(**_kwargs):
        raise pymysql.err.OperationalError(2003, "Can't connect")

    monkeypatch.setattr("app.tools.customer_db.pymysql.connect", fake_connect)

    provider = ProviderFactory.create(
        "mysql",
        "mysql://bad:bad@localhost:3307/nowhere",
    )

    result = provider.test_connection()
    assert result.ok is False
    assert "OperationalError" in result.message


def test_mssql_test_connection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def execute(self, query: str) -> None:
            assert "SELECT 1" in query.upper()

        def fetchone(self):
            return (1,)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_connect(**kwargs):
        assert kwargs["server"] == "localhost"
        assert kwargs["port"] == 1433
        assert kwargs["user"] == "sa"
        assert kwargs["password"] == "Password@123"
        assert kwargs["database"] == "customerdb"
        return FakeConn()

    monkeypatch.setattr("app.tools.customer_db.pymssql.connect", fake_connect)

    provider = ProviderFactory.create(
        "mssql",
        "mssql://sa:Password%40123@localhost:1433/customerdb",
    )

    result = provider.test_connection()
    assert isinstance(result, ConnectionTestResult)
    assert result.ok is True
    assert result.message == "Connection successful"


def test_mssql_test_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_connect(**_kwargs):
        raise pymssql.OperationalError("Adaptive Server connection failed")

    monkeypatch.setattr("app.tools.customer_db.pymssql.connect", fake_connect)

    provider = ProviderFactory.create(
        "mssql",
        "mssql://bad:bad@localhost:1433/nowhere",
    )

    result = provider.test_connection()
    assert result.ok is False
    assert "OperationalError" in result.message


def test_postgres_introspect_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake a tiny DB: customers + orders (with one FK)."""

    class FakeCursor:
        def __init__(self) -> None:
            self._rows: list = []

        def execute(self, query: str, params=None) -> None:
            q = " ".join(query.lower().split())
            params = params or ()

            # Step 1: list tables (schema, then table_name)
            if "from information_schema.tables" in q:
                self._rows = [("customers",), ("orders",)]
                return

            # Step 3: primary keys (pg_constraint)
            if "contype = 'p'" in q or 'contype = "p"' in q:
                table = params[1] if len(params) > 1 else params[0]
                if table == "customers":
                    self._rows = [("customer_id",)]
                elif table == "orders":
                    self._rows = [("order_id",)]
                else:
                    self._rows = []
                return

            # Step 4: foreign keys (pg_constraint)
            if "contype = 'f'" in q or 'contype = "f"' in q:
                table = params[1] if len(params) > 1 else params[0]
                if table == "orders":
                    self._rows = [
                        ("customer_id", "public", "customers", "customer_id")
                    ]
                else:
                    self._rows = []
                return

            # Step 2: columns
            if "from information_schema.columns" in q:
                table = params[1] if len(params) > 1 else params[0]
                if table == "customers":
                    self._rows = [
                        ("customer_id", "integer", "NO"),
                        ("customer_name", "character varying", "NO"),
                    ]
                elif table == "orders":
                    self._rows = [
                        ("order_id", "integer", "NO"),
                        ("customer_id", "integer", "YES"),
                    ]
                else:
                    self._rows = []
                return

            self._rows = []

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "app.tools.customer_db.psycopg.connect",
        lambda *_a, **_k: FakeConn(),
    )

    provider = PostgreSQLProvider(
        "postgresql://reader:reader@localhost:5433/customerdb"
    )
    result = provider.introspect_schema()

    assert "tables" in result
    by_name = {t["table_name"]: t for t in result["tables"]}
    assert set(by_name) == {"customers", "orders"}

    customers = by_name["customers"]
    assert customers["columns"][0]["name"] == "customer_id"
    assert customers["columns"][0]["is_pk"] is True
    assert customers["foreign_keys"] == []

    orders = by_name["orders"]
    assert orders["columns"][0]["is_pk"] is True
    assert orders["foreign_keys"] == [
        {
            "column": "customer_id",
            "referred_table": "customers",
            "referred_column": "customer_id",
        }
    ]


def test_sqlalchemy_url_rewrite() -> None:
    assert (
        sqlalchemy_url_for_customer_db(
            "postgresql://u:p@localhost/db", "postgres"
        )
        == "postgresql+psycopg://u:p@localhost/db"
    )
    assert (
        sqlalchemy_url_for_customer_db("mysql://u:p@localhost/db", "mysql")
        == "mysql+pymysql://u:p@localhost/db"
    )
    assert (
        sqlalchemy_url_for_customer_db("mssql://u:p@localhost/db", "mssql")
        == "mssql+pymssql://u:p@localhost/db"
    )


def test_customer_db_pool_reuses_engine() -> None:
    CustomerDBPool.close_all()
    try:
        url = "postgresql://customer:customer@localhost:5433/customerdb"
        first = CustomerDBPool.get_engine(url, "postgres")
        second = CustomerDBPool.get_engine(url, "postgres")
        assert first is second
        assert len(CustomerDBPool._pools) == 1
    finally:
        CustomerDBPool.close_all()
        assert CustomerDBPool._pools == {}
