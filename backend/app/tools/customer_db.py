"""Talk to the customer's database (schema + SELECT queries).
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import UUID
from urllib.parse import unquote, urlparse

import psycopg
import pymysql
import pymssql
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import json

from app.encryption import CredentialEncryption
from app.query_execution import (
    QueryCategory,
    QueryExecutionError,
    QueryExecutionLimits,
    fetch_bounded_rows,
    limits_for_category,
    log_query_event,
    query_execution_slot,
    sql_fingerprint,
    wrap_provider_timeout_error,
)
from app.schema_profiling import profile_table
from app.models import Connection, SchemaTable
from app.schemas import DatabaseType, SchemaTablePayload
from app.serialize import json_safe

logger = logging.getLogger(__name__)

_POOL_SIZE = 5
_POOL_MAX_OVERFLOW = 10
_POOL_RECYCLE_SECONDS = 3600
# Postgres connections aren't pinged on every checkout (a ping costs a full
# network round trip per query on a remote database). A connection the
# server has dropped is retried once instead, so they can stay pooled long:
# opening a new one to a hosted database can take seconds.
_PG_POOL_RECYCLE_SECONDS = 1800
_MAX_CACHED_ENGINES = 32


def sqlalchemy_url_for_customer_db(connection_string: str, dialect: str) -> str:
    """Rewrite a customer URL into a SQLAlchemy dialect URL."""
    url = (connection_string or "").strip()
    key = (dialect or "").lower().strip()
    if key in {"postgres", "postgresql"}:
        if url.startswith("postgresql+psycopg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://") :]
        return url
    if key == "mysql":
        if url.startswith("mysql+pymysql://"):
            return url
        if url.startswith("mysql://"):
            return "mysql+pymysql://" + url[len("mysql://") :]
        return url
    if key in {"mssql", "sqlserver"}:
        if url.startswith("mssql+pymssql://"):
            return url
        if url.startswith("mssql://"):
            return "mssql+pymssql://" + url[len("mssql://") :]
        return url
    return url


class CustomerDBPool:
    """LRU cache of SQLAlchemy engines, one per customer connection URL."""

    _lock = threading.Lock()
    _pools: OrderedDict[str, Engine] = OrderedDict()

    @classmethod
    def get_engine(cls, connection_string: str, dialect: str) -> Engine:
        url = sqlalchemy_url_for_customer_db(connection_string, dialect)
        with cls._lock:
            engine = cls._pools.get(url)
            if engine is not None:
                cls._pools.move_to_end(url)
                return engine
            options: dict[str, Any] = {
                "pool_recycle": _POOL_RECYCLE_SECONDS,
                "pool_pre_ping": True,
            }
            if dialect == "postgres":
                # Read-only SELECTs need no transaction: autocommit skips the
                # BEGIN and the rollback on return. A dead pooled connection
                # is retried once in execute_read_only_query instead.
                options = {
                    "pool_recycle": _PG_POOL_RECYCLE_SECONDS,
                    "pool_pre_ping": False,
                    "isolation_level": "AUTOCOMMIT",
                }
            engine = create_engine(
                url,
                pool_size=_POOL_SIZE,
                max_overflow=_POOL_MAX_OVERFLOW,
                echo=False,
                **options,
            )
            while len(cls._pools) >= _MAX_CACHED_ENGINES:
                _old_url, old_engine = cls._pools.popitem(last=False)
                try:
                    old_engine.dispose()
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to dispose evicted customer DB engine")
            cls._pools[url] = engine
            return engine

    @classmethod
    def checkout(cls, connection_string: str, dialect: str) -> Any:
        """Borrow a DBAPI connection from the pool (close() returns it)."""
        return cls.get_engine(connection_string, dialect).raw_connection()

    @classmethod
    @contextmanager
    def connection(cls, connection_string: str, dialect: str) -> Iterator[Any]:
        conn = cls.checkout(connection_string, dialect)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                logger.debug("Customer DB pooled connection close failed", exc_info=True)

    @classmethod
    def close_all(cls) -> None:
        """Dispose all cached engines (app shutdown)."""
        with cls._lock:
            engines = list(cls._pools.values())
            cls._pools.clear()
        for engine in engines:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001
                logger.warning("Failed to dispose customer DB engine on shutdown")


@dataclass(frozen=True)
class ConnectionTestResult:
    """Outcome of ``DatabaseProvider.test_connection``."""

    ok: bool
    message: str
    read_only_verified: bool | None = None


class UnsupportedDatabaseError(NotImplementedError):
    """Raised when a db engine is accepted by the API but not implemented yet."""


def _coerce_connection_id(connection_id: str | UUID) -> UUID:
    if isinstance(connection_id, UUID):
        return connection_id
    return UUID(str(connection_id))


_WRITE_TABLE_PRIVS = frozenset({"INSERT", "UPDATE", "DELETE", "TRUNCATE"})


def _connection_result_with_readonly(
    ok: bool,
    message: str,
    read_only_verified: bool | None,
) -> ConnectionTestResult:
    return ConnectionTestResult(
        ok=ok,
        message=message,
        read_only_verified=read_only_verified,
    )



def _assert_execution_read_only(sql: str, db_type: str | None) -> None:
    """Final SELECT-only check before any customer SQL hits a database."""
    from app.tools.sql_safety import assert_read_only_sql

    assert_read_only_sql(sql, db_type=db_type)


class DatabaseProvider(ABC):
    """Common interface for customer DB engines (postgres / mysql / mssql)."""

    def __init__(self, connection_url: str) -> None:
        self.connection_url = connection_url

    @abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """Verify credentials and reachability. Does not run user SQL."""

    @abstractmethod
    def list_schemas(self) -> list[str]:
        """User-visible schemas/databases available for discovery."""

    @abstractmethod
    def default_schema(self) -> str:
        """Schema used when discover scope is ``default``."""

    @abstractmethod
    def introspect_schema(self, schemas: list[str] | None = None) -> dict:
        """Return {"tables": [...]} with schema_name on each table."""

    @abstractmethod
    def execute_read_only_query(
        self,
        sql: str,
        *,
        limits: QueryExecutionLimits | None = None,
        db_type: str | None = None,
    ) -> list[dict]:
        """Run a validated SELECT against the customer DB."""
        _assert_execution_read_only(sql, db_type)


class PostgreSQLProvider(DatabaseProvider):
    """Postgres adapter.

    Real ``test_connection`` and ``introspect_schema``.
    """

    def test_connection(self) -> ConnectionTestResult:
        # validate reachability + credentials using a short-lived connection.
        # We intentionally run a harmless SELECT 1 (no tenant data).
        try:
            with psycopg.connect(self.connection_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    _ = cur.fetchone()
                    read_only = self._inspect_postgres_read_only(cur)

            return _connection_result_with_readonly(
                True, "Connection successful", read_only
            )
        except Exception as exc:
            # Avoid leaking connection details/passwords from exception text.
            return _connection_result_with_readonly(
                False,
                f"Connection failed: {type(exc).__name__}",
                None,
            )

    def _inspect_postgres_read_only(self, cur) -> bool | None:  # noqa: ANN001
        """Conservative metadata inspection; False only when write is conclusive."""
        try:
            cur.execute("SELECT current_user")
            row = cur.fetchone()
            if not row:
                return None
            role = str(row[0])

            cur.execute(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                  AND table_type = 'BASE TABLE'
                  AND table_schema = 'public'
                LIMIT 100
                """
            )
            tables = cur.fetchall()
            if not tables:
                return None

            for schema_name, table_name in tables:
                for priv in _WRITE_TABLE_PRIVS:
                    cur.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        (role, f"{schema_name}.{table_name}", priv),
                    )
                    if cur.fetchone()[0]:
                        return False

            cur.execute(
                "SELECT has_schema_privilege(%s, %s, 'CREATE')",
                (role, "public"),
            )
            if cur.fetchone()[0]:
                return False

            return True
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Schema discovery
    #
    # Goal: return this shape:
    # {
    #   "tables": [
    #     {
    #       "table_name": "orders",
    #       "columns": [
    #         {"name": "...", "data_type": "...", "nullable": true/false, "is_pk": true/false}
    #       ],
    #       "foreign_keys": [
    #         {"column": "...", "referred_table": "...", "referred_column": "..."}
    #       ]
    #     }
    #   ]
    # }
    #
    # Steps we take:
    #   1) list table names
    #   2) for each table: list columns
    #   3) for each table: list primary-key columns
    #   4) for each table: list foreign keys
    #   5) build the dict above
    # ------------------------------------------------------------------

    def default_schema(self) -> str:
        return "public"

    def list_schemas(self) -> list[str]:
        with psycopg.connect(self.connection_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name NOT IN (
                        'pg_catalog', 'information_schema', 'pg_toast'
                    )
                      AND schema_name NOT LIKE 'pg\\_toast%' ESCAPE '\\'
                      AND schema_name NOT LIKE 'pg\\_temp%' ESCAPE '\\'
                      AND schema_name NOT LIKE 'pg\\_toast_temp%' ESCAPE '\\'
                    ORDER BY schema_name
                    """
                )
                return [row[0] for row in cur.fetchall()]

    def introspect_schema(self, schemas: list[str] | None = None) -> dict:
        target = schemas or [self.default_schema()]
        with psycopg.connect(self.connection_url) as conn:
            with conn.cursor() as cur:
                tables: list[dict] = []
                for schema_name in target:
                    for table_name in self._pg_list_tables(cur, schema_name):
                        columns = self._pg_list_columns(cur, schema_name, table_name)
                        pk_names = self._pg_list_primary_keys(
                            cur, schema_name, table_name
                        )
                        foreign_keys = self._pg_list_foreign_keys(
                            cur, schema_name, table_name
                        )
                        pk_set = set(pk_names)
                        for col in columns:
                            col["is_pk"] = col["name"] in pk_set
                        tables.append(
                            {
                                "schema_name": schema_name,
                                "table_name": table_name,
                                "columns": columns,
                                "foreign_keys": foreign_keys,
                            }
                        )
        return {"tables": tables}

    def _pg_list_tables(self, cur, schema_name: str) -> list[str]:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema_name,),
        )
        return [row[0] for row in cur.fetchall()]

    def _pg_list_columns(self, cur, schema_name: str, table_name: str) -> list[dict]:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema_name, table_name),
        )
        columns: list[dict] = []
        for name, data_type, is_nullable in cur.fetchall():
            columns.append(
                {
                    "name": name,
                    "data_type": data_type,
                    "nullable": is_nullable.upper() == "YES",
                    "is_pk": False,
                }
            )
        return columns

    def _pg_list_primary_keys(
        self, cur, schema_name: str, table_name: str
    ) -> list[str]:
        cur.execute(
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            JOIN pg_namespace n ON t.relnamespace = n.oid
            JOIN pg_attribute a
                ON a.attrelid = t.oid
               AND a.attnum = ANY (c.conkey)
            WHERE c.contype = 'p'
              AND n.nspname = %s
              AND t.relname = %s
            ORDER BY a.attnum
            """,
            (schema_name, table_name),
        )
        return [row[0] for row in cur.fetchall()]

    def _pg_list_foreign_keys(
        self, cur, schema_name: str, table_name: str
    ) -> list[dict]:
        cur.execute(
            """
            SELECT
                src.attname,
                tgt_ns.nspname,
                tgt.relname,
                tgt_col.attname
            FROM pg_constraint c
            JOIN pg_class src_table ON c.conrelid = src_table.oid
            JOIN pg_namespace src_ns ON src_table.relnamespace = src_ns.oid
            JOIN pg_class tgt ON c.confrelid = tgt.oid
            JOIN pg_namespace tgt_ns ON tgt.relnamespace = tgt_ns.oid
            JOIN pg_attribute src
                ON src.attrelid = src_table.oid
               AND src.attnum = ANY (c.conkey)
            JOIN pg_attribute tgt_col
                ON tgt_col.attrelid = tgt.oid
               AND tgt_col.attnum = ANY (c.confkey)
            WHERE c.contype = 'f'
              AND src_ns.nspname = %s
              AND src_table.relname = %s
            """,
            (schema_name, table_name),
        )
        foreign_keys = []
        for column, ref_schema, referred_table, referred_column in cur.fetchall():
            qualified = (
                referred_table
                if ref_schema == schema_name
                else f"{ref_schema}.{referred_table}"
            )
            foreign_keys.append(
                {
                    "column": column,
                    "referred_table": qualified,
                    "referred_column": referred_column,
                }
            )
        return foreign_keys

    def execute_read_only_query(
        self,
        sql: str,
        *,
        limits: QueryExecutionLimits | None = None,
        db_type: str | None = None,
    ) -> list[dict]:
        """Execute a validated read-only SQL query."""
        _assert_execution_read_only(sql, db_type)
        from psycopg.rows import dict_row

        limits = limits or limits_for_category(QueryCategory.ANALYTICS)
        timeout_ms = limits.timeout_seconds * 1000


        def run(conn: Any) -> list[dict]:
            driver = conn.driver_connection
            with driver.cursor(row_factory=dict_row) as cur:
                try:
                    # One network round trip for both statements; remote
                    # databases cost ~100 ms+ per trip.
                    with driver.pipeline():
                        cur.execute(f"SET statement_timeout = '{timeout_ms}'")
                        cur.execute(sql)
                except psycopg.OperationalError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise wrap_provider_timeout_error(exc, limits=limits) from exc
                try:
                    return fetch_bounded_rows(
                        cur,
                        max_rows=limits.max_rows,
                        max_payload_bytes=limits.max_payload_bytes,
                    )
                except QueryExecutionError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise wrap_provider_timeout_error(exc, limits=limits) from exc

        for attempt in range(2):
            with CustomerDBPool.connection(self.connection_url, "postgres") as conn:
                try:
                    return run(conn)
                except psycopg.OperationalError as exc:
                    # A pooled connection the server already closed: drop it
                    # and retry once on a fresh one. Anything else is real.
                    driver = conn.driver_connection
                    if attempt == 0 and (driver.closed or driver.broken):
                        conn.invalidate()
                        continue
                    raise wrap_provider_timeout_error(exc, limits=limits) from exc
        raise QueryExecutionError("Query failed")


class MySQLProvider(DatabaseProvider):
    """MySQL adapter with real ``test_connection`` (query/schema later)."""

    def _connect(self):
        parsed = urlparse(self.connection_url)
        return pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "").lstrip("/") or None,
            connect_timeout=5,
        )

    def test_connection(self) -> ConnectionTestResult:
        # validate reachability + credentials using a short-lived connection.
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    _ = cur.fetchone()
                    read_only = self._inspect_mysql_read_only(cur)

            return _connection_result_with_readonly(
                True, "Connection successful", read_only
            )
        except Exception as exc:
            return _connection_result_with_readonly(
                False,
                f"Connection failed: {type(exc).__name__}",
                None,
            )

    def _inspect_mysql_read_only(self, cur) -> bool | None:  # noqa: ANN001
        try:
            cur.execute("SHOW GRANTS FOR CURRENT_USER()")
            grants = [str(row[0]) for row in cur.fetchall()]
            if not grants:
                return None
            for grant in grants:
                upper = grant.upper()
                if "ALL PRIVILEGES" in upper and "ON *.*" in upper:
                    return False
                if any(
                    verb in upper
                    for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER")
                ):
                    return False
            if any("SELECT" in g.upper() for g in grants):
                return True
            return None
        except Exception:
            return None

    def default_schema(self) -> str:
        parsed = urlparse(self.connection_url)
        return (parsed.path or "").lstrip("/") or ""

    def list_schemas(self) -> list[str]:
        skip = {"information_schema", "mysql", "performance_schema", "sys"}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES")
                names = [row[0] for row in cur.fetchall()]
        return [n for n in names if n not in skip]

    def introspect_schema(self, schemas: list[str] | None = None) -> dict:
        default = self.default_schema()
        target = schemas or ([default] if default else [])
        if not target:
            raise ValueError("MySQL connection has no database in the URL")

        with self._connect() as conn:
            with conn.cursor() as cur:
                tables: list[dict] = []
                for schema_name in target:
                    for table_name in self._mysql_list_tables(cur, schema_name):
                        columns = self._mysql_list_columns(
                            cur, schema_name, table_name
                        )
                        pk_names = self._mysql_list_primary_keys(
                            cur, schema_name, table_name
                        )
                        foreign_keys = self._mysql_list_foreign_keys(
                            cur, schema_name, table_name
                        )
                        pk_set = set(pk_names)
                        for col in columns:
                            col["is_pk"] = col["name"] in pk_set
                        tables.append(
                            {
                                "schema_name": schema_name,
                                "table_name": table_name,
                                "columns": columns,
                                "foreign_keys": foreign_keys,
                            }
                        )
        return {"tables": tables}

    def _mysql_list_tables(self, cur, schema_name: str) -> list[str]:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema_name,),
        )
        return [row[0] for row in cur.fetchall()]

    def _mysql_list_columns(
        self, cur, schema_name: str, table_name: str
    ) -> list[dict]:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema_name, table_name),
        )
        columns: list[dict] = []
        for name, data_type, is_nullable in cur.fetchall():
            columns.append(
                {
                    "name": name,
                    "data_type": data_type,
                    "nullable": is_nullable.upper() == "YES",
                    "is_pk": False,
                }
            )
        return columns

    def _mysql_list_primary_keys(
        self, cur, schema_name: str, table_name: str
    ) -> list[str]:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = %s
              AND table_name = %s
              AND constraint_name = 'PRIMARY'
            ORDER BY ordinal_position
            """,
            (schema_name, table_name),
        )
        return [row[0] for row in cur.fetchall()]

    def _mysql_list_foreign_keys(
        self, cur, schema_name: str, table_name: str
    ) -> list[dict]:
        cur.execute(
            """
            SELECT
                column_name,
                referenced_table_schema,
                referenced_table_name,
                referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = %s
              AND table_name = %s
              AND referenced_table_name IS NOT NULL
            ORDER BY ordinal_position
            """,
            (schema_name, table_name),
        )
        foreign_keys: list[dict] = []
        for column, ref_schema, referred_table, referred_column in cur.fetchall():
            qualified = (
                referred_table
                if (ref_schema or schema_name) == schema_name
                else f"{ref_schema}.{referred_table}"
            )
            foreign_keys.append(
                {
                    "column": column,
                    "referred_table": qualified,
                    "referred_column": referred_column,
                }
            )
        return foreign_keys

    def execute_read_only_query(
        self,
        sql: str,
        *,
        limits: QueryExecutionLimits | None = None,
        db_type: str | None = None,
    ) -> list[dict]:
        """Execute a validated read-only SQL query (DictCursor → JSON-safe rows)."""
        _assert_execution_read_only(sql, db_type)
        limits = limits or limits_for_category(QueryCategory.ANALYTICS)
        timeout_ms = limits.timeout_seconds * 1000

        with CustomerDBPool.connection(self.connection_url, "mysql") as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                try:
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
                except Exception:  # noqa: BLE001 — older MySQL may not support it
                    pass
                try:
                    cur.execute(sql)
                except Exception as exc:  # noqa: BLE001
                    raise wrap_provider_timeout_error(exc, limits=limits) from exc
                try:
                    return fetch_bounded_rows(
                        cur,
                        max_rows=limits.max_rows,
                        max_payload_bytes=limits.max_payload_bytes,
                    )
                except QueryExecutionError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise wrap_provider_timeout_error(exc, limits=limits) from exc


class SQLServerProvider(DatabaseProvider):
    """SQL Server (mssql) adapter with real ``test_connection`` (query/schema later)."""

    def _connect(self):
        parsed = urlparse(self.connection_url)
        return pymssql.connect(
            server=parsed.hostname or "localhost",
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "").lstrip("/") or None,
            port=parsed.port or 1433,
            login_timeout=5,
        )

    def test_connection(self) -> ConnectionTestResult:
        # validate reachability + credentials using a short-lived connection.
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    _ = cur.fetchone()
                    read_only = self._inspect_mssql_read_only(cur)

            return _connection_result_with_readonly(
                True, "Connection successful", read_only
            )
        except Exception as exc:
            return _connection_result_with_readonly(
                False,
                f"Connection failed: {type(exc).__name__}",
                None,
            )

    def _inspect_mssql_read_only(self, cur) -> bool | None:  # noqa: ANN001
        try:
            for role in ("db_owner", "db_datawriter", "db_ddladmin"):
                cur.execute("SELECT IS_ROLEMEMBER(%s)", (role,))
                row = cur.fetchone()
                if row and int(row[0]) == 1:
                    return False

            cur.execute(
                """
                SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'),
                       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE'),
                       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE'),
                       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')
                """
            )
            row = cur.fetchone()
            if row and any(int(v or 0) == 1 for v in row):
                return False
            return True
        except Exception:
            return None

    def default_schema(self) -> str:
        return "dbo"

    def list_schemas(self) -> list[str]:
        skip = {
            "sys",
            "INFORMATION_SCHEMA",
            "guest",
            "db_owner",
            "db_accessadmin",
            "db_securityadmin",
            "db_ddladmin",
            "db_backupoperator",
            "db_datareader",
            "db_datawriter",
            "db_denydatareader",
            "db_denydatawriter",
        }
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT name
                    FROM sys.schemas
                    ORDER BY name
                    """
                )
                names = [row[0] for row in cur.fetchall()]
        return [n for n in names if n not in skip]

    def introspect_schema(self, schemas: list[str] | None = None) -> dict:
        target = schemas or [self.default_schema()]
        with self._connect() as conn:
            with conn.cursor() as cur:
                tables: list[dict] = []
                for schema_name in target:
                    for table_name in self._mssql_list_tables(cur, schema_name):
                        columns = self._mssql_list_columns(
                            cur, schema_name, table_name
                        )
                        pk_names = self._mssql_list_primary_keys(
                            cur, schema_name, table_name
                        )
                        foreign_keys = self._mssql_list_foreign_keys(
                            cur, schema_name, table_name
                        )
                        pk_set = set(pk_names)
                        for col in columns:
                            col["is_pk"] = col["name"] in pk_set
                        tables.append(
                            {
                                "schema_name": schema_name,
                                "table_name": table_name,
                                "columns": columns,
                                "foreign_keys": foreign_keys,
                            }
                        )
        return {"tables": tables}

    def _mssql_list_tables(self, cur, schema_name: str) -> list[str]:
        cur.execute(
            """
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """,
            (schema_name,),
        )
        return [row[0] for row in cur.fetchall()]

    def _mssql_list_columns(
        self, cur, schema_name: str, table_name: str
    ) -> list[dict]:
        cur.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (schema_name, table_name),
        )
        columns: list[dict] = []
        for name, data_type, is_nullable in cur.fetchall():
            columns.append(
                {
                    "name": name,
                    "data_type": data_type,
                    "nullable": is_nullable.upper() == "YES",
                    "is_pk": False,
                }
            )
        return columns

    def _mssql_list_primary_keys(
        self, cur, schema_name: str, table_name: str
    ) -> list[str]:
        cur.execute(
            """
            SELECT c.name
            FROM sys.tables AS t
            JOIN sys.schemas AS s
              ON s.schema_id = t.schema_id
            JOIN sys.indexes AS i
              ON i.object_id = t.object_id
             AND i.is_primary_key = 1
            JOIN sys.index_columns AS ic
              ON ic.object_id = i.object_id
             AND ic.index_id = i.index_id
            JOIN sys.columns AS c
              ON c.object_id = ic.object_id
             AND c.column_id = ic.column_id
            WHERE s.name = %s
              AND t.name = %s
            ORDER BY ic.key_ordinal
            """,
            (schema_name, table_name),
        )
        return [row[0] for row in cur.fetchall()]

    def _mssql_list_foreign_keys(
        self, cur, schema_name: str, table_name: str
    ) -> list[dict]:
        cur.execute(
            """
            SELECT
                parent_column.name,
                referenced_schema.name AS referred_schema,
                referenced_table.name AS referred_table,
                referenced_column.name AS referred_column
            FROM sys.foreign_key_columns AS fkc
            JOIN sys.tables AS parent_table
              ON parent_table.object_id = fkc.parent_object_id
            JOIN sys.schemas AS parent_schema
              ON parent_schema.schema_id = parent_table.schema_id
            JOIN sys.columns AS parent_column
              ON parent_column.object_id = fkc.parent_object_id
             AND parent_column.column_id = fkc.parent_column_id
            JOIN sys.tables AS referenced_table
              ON referenced_table.object_id = fkc.referenced_object_id
            JOIN sys.schemas AS referenced_schema
              ON referenced_schema.schema_id = referenced_table.schema_id
            JOIN sys.columns AS referenced_column
              ON referenced_column.object_id = fkc.referenced_object_id
             AND referenced_column.column_id = fkc.referenced_column_id
            WHERE parent_schema.name = %s
              AND parent_table.name = %s
            ORDER BY fkc.constraint_object_id, fkc.constraint_column_id
            """,
            (schema_name, table_name),
        )
        foreign_keys: list[dict] = []
        for column, ref_schema, referred_table, referred_column in cur.fetchall():
            qualified = (
                referred_table
                if ref_schema == schema_name
                else f"{ref_schema}.{referred_table}"
            )
            foreign_keys.append(
                {
                    "column": column,
                    "referred_table": qualified,
                    "referred_column": referred_column,
                }
            )
        return foreign_keys

    def execute_read_only_query(
        self,
        sql: str,
        *,
        limits: QueryExecutionLimits | None = None,
        db_type: str | None = None,
    ) -> list[dict]:
        """Execute a validated read-only SQL query against SQL Server."""
        _assert_execution_read_only(sql, db_type)
        limits = limits or limits_for_category(QueryCategory.ANALYTICS)
        timeout_ms = limits.timeout_seconds * 1000

        with CustomerDBPool.connection(self.connection_url, "mssql") as conn:
            conn.query_timeout = limits.timeout_seconds
            with conn.cursor(as_dict=True) as cur:
                try:
                    cur.execute(f"SET LOCK_TIMEOUT {timeout_ms};")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    cur.execute(sql)
                except Exception as exc:  # noqa: BLE001
                    raise wrap_provider_timeout_error(exc, limits=limits) from exc
                try:
                    return fetch_bounded_rows(
                        cur,
                        max_rows=limits.max_rows,
                        max_payload_bytes=limits.max_payload_bytes,
                    )
                except QueryExecutionError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise wrap_provider_timeout_error(exc, limits=limits) from exc


class ProviderFactory:
    """Pick a concrete provider by ``db_type``."""

    _REGISTRY: dict[str, type[DatabaseProvider]] = {
        DatabaseType.postgres.value: PostgreSQLProvider,
        DatabaseType.mysql.value: MySQLProvider,
        DatabaseType.mssql.value: SQLServerProvider,
    }

    @classmethod
    def create(cls, db_type: str | DatabaseType, connection_url: str) -> DatabaseProvider:
        key = db_type.value if isinstance(db_type, DatabaseType) else str(db_type).lower().strip()
        if key in {DatabaseType.csv.value, DatabaseType.excel.value}:
            from app.tools.duckdb_provider import DuckDBFileProvider

            return DuckDBFileProvider(connection_url)
        provider_cls = cls._REGISTRY.get(key)
        if provider_cls is None:
            raise ValueError(f"Unsupported database type: {db_type}")
        return provider_cls(connection_url)


def run_readonly_query(
    connection_id: str,
    sql: str,
    *,
    tenant_id: UUID,
    category: QueryCategory = QueryCategory.ANALYTICS,
    user_id: str | None = None,
    conversation_id: str | None = None,
    dashboard_id: str | None = None,
) -> list[dict]:
    """Load a saved connection, decrypt its URL, and run a read-only query.

    Notes:
    - Callers (chat, dashboard, follow-up) must still run `SQLSafetyPipeline`
      first for schema/LIMIT checks.
    - This function also enforces a final SELECT-only execution guard so
      mutation SQL cannot reach the customer database if a caller skips the
      pipeline.
    - Credentials stay encrypted at rest and are decrypted only in-memory.
    - ``tenant_id`` must match the connection owner (defense in depth).
    - Department grants are enforced here too (``app.access``): a non-admin
      requester must be granted the connection, and every table the SQL
      reads must sit in a granted schema.
    - Resource policy (timeout, row/payload limits, concurrency) is enforced here.
    """
    import time

    from app.database import SessionLocal

    tenant_str = str(tenant_id)
    conn_id_str = str(connection_id)
    limits = limits_for_category(category)
    sql_hash = sql_fingerprint(sql)
    started = time.perf_counter()

    db = SessionLocal()
    try:
        conn = db.scalar(
            select(Connection).where(Connection.id == _coerce_connection_id(connection_id))
        )
        if conn is None:
            raise ValueError("Connection not found")
        if conn.tenant_id != tenant_id:
            raise ValueError("Connection not found")

        db_type = str(conn.db_type)
        _assert_execution_read_only(sql, db_type)
        # Department grants: connection + every referenced table's schema.
        from app.access import enforce_query_access

        enforce_query_access(db, conn=conn, sql=sql, user_id=user_id)

        encryptor = CredentialEncryption()
        url = encryptor.decrypt(conn.encrypted_url)

        log_query_event(
            "query_start",
            tenant_id=tenant_str,
            connection_id=conn_id_str,
            conversation_id=conversation_id,
            dashboard_id=dashboard_id,
            category=category.value,
            sql_hash=sql_hash,
        )

        with query_execution_slot(
            tenant_id=tenant_str,
            user_id=user_id,
            category=category,
        ):
            provider = ProviderFactory.create(conn.db_type, url)
            rows = provider.execute_read_only_query(
                sql,
                limits=limits,
                db_type=db_type,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        result_bytes = sum(len(str(k)) + len(str(v)) for row in rows for k, v in row.items())
        log_query_event(
            "query_complete",
            tenant_id=tenant_str,
            connection_id=conn_id_str,
            conversation_id=conversation_id,
            dashboard_id=dashboard_id,
            category=category.value,
            sql_hash=sql_hash,
            duration_ms=duration_ms,
            row_count=len(rows),
            result_bytes=result_bytes,
            outcome="ok",
        )
        return rows
    except QueryExecutionError as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        log_query_event(
            "query_resource_limit" if exc.code != "query_timeout" else "query_timeout",
            tenant_id=tenant_str,
            connection_id=conn_id_str,
            conversation_id=conversation_id,
            dashboard_id=dashboard_id,
            category=category.value,
            sql_hash=sql_hash,
            duration_ms=duration_ms,
            outcome=exc.code,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.perf_counter() - started) * 1000
        wrapped = wrap_provider_timeout_error(exc, limits=limits)
        if isinstance(wrapped, QueryExecutionError):
            log_query_event(
                "query_timeout" if wrapped.code == "query_timeout" else "query_resource_limit",
                tenant_id=tenant_str,
                connection_id=conn_id_str,
                conversation_id=conversation_id,
                dashboard_id=dashboard_id,
                category=category.value,
                sql_hash=sql_hash,
                duration_ms=duration_ms,
                outcome=wrapped.code,
            )
            raise wrapped from exc
        log_query_event(
            "query_failed",
            tenant_id=tenant_str,
            connection_id=conn_id_str,
            conversation_id=conversation_id,
            dashboard_id=dashboard_id,
            category=category.value,
            sql_hash=sql_hash,
            duration_ms=duration_ms,
            outcome=type(exc).__name__,
        )
        raise
    finally:
        db.close()


def run_system_query(
    connection_id: str,
    sql: str,
    *,
    tenant_id: UUID,
    category: QueryCategory = QueryCategory.PROFILING,
) -> list[dict]:
    """Run internally generated SELECT-only SQL with profiling/metadata limits."""
    from app.tools.sql_safety import StatementValidator

    StatementValidator().validate(sql)
    return run_readonly_query(
        connection_id,
        sql,
        tenant_id=tenant_id,
        category=category,
    )


def resolve_discover_schemas(
    provider: DatabaseProvider,
    *,
    scope: str = "default",
    schemas: list[str] | None = None,
) -> list[str]:
    """Map discover scope → concrete schema name list."""
    available = provider.list_schemas()
    default = provider.default_schema()
    scope_lc = (scope or "default").lower().strip()

    if scope_lc == "all":
        return available or ([default] if default else [])

    if scope_lc == "selected":
        requested = [s.strip() for s in (schemas or []) if s and s.strip()]
        if not requested:
            raise ValueError("Select at least one schema when scope is 'selected'")
        allowed = {a.lower(): a for a in available}
        resolved: list[str] = []
        for name in requested:
            match = allowed.get(name.lower())
            if match is None:
                raise ValueError(f"Schema '{name}' is not available on this connection")
            if match not in resolved:
                resolved.append(match)
        return resolved

    # default
    if default and any(a.lower() == default.lower() for a in available):
        for a in available:
            if a.lower() == default.lower():
                return [a]
    if default:
        return [default]
    return available[:1] if available else []


def discover_schema(
    db: Session,
    connection_id: UUID,
    *,
    scope: str = "default",
    schemas: list[str] | None = None,
    tenant_id: UUID | None = None,
    profile: bool | None = None,
) -> dict:
    """Load a saved connection, introspect its DB, and cache the schema."""
    from app.services.schema_discovery_service import run_schema_discovery

    conn = db.scalar(
        select(Connection).where(Connection.id == _coerce_connection_id(connection_id))
    )
    if conn is None:
        raise ValueError("Connection not found")
    tid = tenant_id or conn.tenant_id
    do_profile = conn.profile_on_discover if profile is None else profile
    result = run_schema_discovery(
        db,
        connection_id=connection_id,
        tenant_id=tid,
        scope=scope,
        schemas=schemas,
        profile=do_profile,
        include_views=conn.include_views,
    )
    return {
        "connection_id": result.connection_id,
        "db_type": result.db_type,
        "tables": result.tables,
        "schemas_used": result.schemas_used,
        "status": result.status,
        "warnings": result.warnings,
    }


def get_stored_schema(
    db: Session,
    connection_id: UUID,
    *,
    tenant_id: UUID | None = None,
) -> dict:
    """Load previously discovered schema from schema_tables (platform DB)."""
    from app.services.schema_discovery_service import get_stored_schema_for_tenant

    conn = db.scalar(
        select(Connection).where(Connection.id == _coerce_connection_id(connection_id))
    )
    if conn is None:
        raise ValueError("Connection not found")
    return get_stored_schema_for_tenant(
        db, connection_id, tenant_id or conn.tenant_id
    )


def list_connection_schemas(db: Session, connection_id: UUID) -> dict:
    """List discoverable schemas for a saved connection."""
    conn = db.scalar(
        select(Connection).where(Connection.id == _coerce_connection_id(connection_id))
    )
    if conn is None:
        raise ValueError("Connection not found")
    encryptor = CredentialEncryption()
    url = encryptor.decrypt(conn.encrypted_url)
    provider = ProviderFactory.create(conn.db_type, url)
    return {
        "connection_id": conn.id,
        "schemas": provider.list_schemas(),
        "default_schema": provider.default_schema(),
    }
