"""Department-based data access: which connections and schemas a user may use.

Org admins see every connection in their org. Everyone else sees only what
their departments were granted — a whole connection, or specific schemas of
it. A user in several departments gets the union.

Enforcement is layered and never prompt-only:

1. Routes list/open only accessible connections (``accessible_connection``).
2. The agent's schema and semantic catalog are filtered to allowed schemas
   (``filter_schema`` / ``filter_semantic_catalog``), so the SQL safety
   pipeline rejects any other table as "does not exist".
3. ``enforce_query_access`` runs inside ``run_readonly_query`` right before
   customer SQL executes: it re-checks the connection grant and every table
   the SQL touches. This holds even if a caller skipped layers 1–2.

Who is asking comes from the explicit ``user_id`` or, failing that, the
request's usage owner context (set from the bearer token by middleware and by
the chat websocket). With neither, the call is a platform job (schema
discovery, profiling) and is not restricted.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Connection, DepartmentGrant, DepartmentMember, SchemaTable, User

ORG_ADMIN_ROLES = frozenset({"admin"})
# Short enough that a revoked grant stops working almost immediately on other
# workers; this process drops its cache on every grant/membership change.
_SCOPE_TTL_SECONDS = 30.0

ACCESS_DENIED_MESSAGE = "You don't have access to this data"


class AccessDeniedError(ValueError):
    """Raised when a user reaches for a connection, schema or table outside their grants."""

    code = "access_denied"

    def __init__(self, message: str = ACCESS_DENIED_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class AccessScope:
    tenant_id: UUID
    user_id: UUID | None
    unrestricted: bool
    # connection_id → allowed schema names (lowercase), or None for the whole connection.
    grants: Mapping[UUID, frozenset[str] | None] = field(default_factory=dict)

    def can_use_connection(self, connection_id: UUID | str) -> bool:
        if self.unrestricted:
            return True
        return _as_uuid(connection_id) in self.grants

    def allowed_schemas(self, connection_id: UUID | str) -> frozenset[str] | None:
        """Lowercase schema names the user may read; None means every schema."""
        if self.unrestricted:
            return None
        cid = _as_uuid(connection_id)
        if cid not in self.grants:
            return frozenset()
        return self.grants[cid]


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


# ----- scope resolution -----

_cache: dict[tuple[UUID, UUID], tuple[float, AccessScope]] = {}
_cache_lock = threading.Lock()


def invalidate_access_scopes(tenant_id: UUID | None = None) -> None:
    """Drop cached scopes (all, or one org's) after grants/memberships change."""
    with _cache_lock:
        if tenant_id is None:
            _cache.clear()
            return
        for key in [k for k in _cache if k[0] == tenant_id]:
            _cache.pop(key, None)


def _deny_all(tenant_id: UUID, user_id: UUID | None) -> AccessScope:
    return AccessScope(tenant_id=tenant_id, user_id=user_id, unrestricted=False, grants={})


def build_access_scope(db: Session, *, tenant_id: UUID, user_id: UUID) -> AccessScope:
    """Uncached scope computation from the platform DB."""
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None or user.tenant_id != tenant_id:
        return _deny_all(tenant_id, user_id)
    if user.role in ORG_ADMIN_ROLES:
        return AccessScope(tenant_id=tenant_id, user_id=user_id, unrestricted=True)

    rows = db.execute(
        select(DepartmentGrant.connection_id, DepartmentGrant.schema_name)
        .join(DepartmentMember, DepartmentMember.department_id == DepartmentGrant.department_id)
        .where(
            DepartmentMember.user_id == user_id,
            DepartmentMember.tenant_id == tenant_id,
            DepartmentGrant.tenant_id == tenant_id,
        )
    ).all()

    grants: dict[UUID, set[str] | None] = {}
    for connection_id, schema_name in rows:
        if schema_name is None or not str(schema_name).strip():
            grants[connection_id] = None  # whole connection wins over schema grants
            continue
        if connection_id in grants and grants[connection_id] is None:
            continue
        grants.setdefault(connection_id, set()).add(str(schema_name).strip().lower())  # type: ignore[union-attr]

    frozen = {cid: (None if s is None else frozenset(s)) for cid, s in grants.items()}
    return AccessScope(tenant_id=tenant_id, user_id=user_id, unrestricted=False, grants=frozen)


def resolve_access_scope(db: Session, *, tenant_id: UUID, user_id: UUID) -> AccessScope:
    key = (tenant_id, user_id)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    scope = build_access_scope(db, tenant_id=tenant_id, user_id=user_id)
    with _cache_lock:
        _cache[key] = (now + _SCOPE_TTL_SECONDS, scope)
    return scope


def scope_for_user(db: Session, user: User) -> AccessScope:
    return resolve_access_scope(db, tenant_id=user.tenant_id, user_id=user.id)


def current_access_scope(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID | str | None = None,
) -> AccessScope | None:
    """Scope of whoever is asking, or None for platform/background work."""
    if user_id:
        try:
            uid = _as_uuid(user_id)
        except (TypeError, ValueError):
            return _deny_all(tenant_id, None)
        return resolve_access_scope(db, tenant_id=tenant_id, user_id=uid)

    from app.llm.usage import current_usage_owner

    owner = current_usage_owner()
    if owner is None:
        return None
    if owner.tenant_id != tenant_id:
        return _deny_all(tenant_id, owner.user_id)
    return resolve_access_scope(db, tenant_id=tenant_id, user_id=owner.user_id)


# ----- route helpers -----


def accessible_connections(db: Session, user: User) -> list[Connection]:
    rows = db.scalars(
        select(Connection).where(Connection.tenant_id == user.tenant_id).order_by(Connection.name)
    ).all()
    scope = scope_for_user(db, user)
    return [c for c in rows if scope.can_use_connection(c.id)]


def accessible_connection(db: Session, *, connection_id: UUID, user: User) -> Connection | None:
    """The tenant connection if this user may use it, else None (treat as 404)."""
    conn = db.scalar(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.tenant_id == user.tenant_id,
        )
    )
    if conn is None:
        return None
    if not scope_for_user(db, user).can_use_connection(conn.id):
        return None
    return conn


# ----- filtering what the agent and UI see -----


def _schema_allowed(schema_name: Any, allowed: frozenset[str]) -> bool:
    if schema_name is None or not str(schema_name).strip():
        return False
    return str(schema_name).strip().lower() in allowed


def filter_schema(schema: dict[str, Any] | None, allowed: frozenset[str] | None) -> dict[str, Any] | None:
    """Copy of a stored-schema dict keeping only tables in allowed schemas."""
    if schema is None or allowed is None or not isinstance(schema, dict):
        return schema
    tables = []
    for table in schema.get("tables") or []:
        if not isinstance(table, dict) or not _schema_allowed(table.get("schema_name"), allowed):
            continue
        fks = [
            fk
            for fk in (table.get("foreign_keys") or [])
            if not isinstance(fk, dict)
            or not fk.get("referred_schema")
            or _schema_allowed(fk.get("referred_schema"), allowed)
        ]
        tables.append({**table, "foreign_keys": fks})
    out = dict(schema)
    out["tables"] = tables
    out["schemas_used"] = [
        s for s in (schema.get("schemas_used") or []) if _schema_allowed(s, allowed)
    ]
    return out


def _item_schema(item: dict[str, Any]) -> str | None:
    schema = item.get("physical_schema")
    if schema:
        return str(schema)
    table = str(item.get("physical_table") or "")
    if "." in table:
        return table.rsplit(".", 1)[0]
    return None


def filter_semantic_catalog(
    catalog: dict[str, Any] | None, allowed: frozenset[str] | None
) -> dict[str, Any] | None:
    """Drop metrics/dimensions bound to tables outside the allowed schemas."""
    if catalog is None or allowed is None or not isinstance(catalog, dict):
        return catalog

    def keep(item: Any) -> bool:
        if not isinstance(item, dict):
            return True
        schema = _item_schema(item)
        return schema is None or _schema_allowed(schema, allowed)

    out = dict(catalog)
    for key in ("metrics", "dimensions", "reference_metrics"):
        if isinstance(catalog.get(key), list):
            out[key] = [item for item in catalog[key] if keep(item)]
    return out


def scoped_schema_for(
    db: Session,
    schema: dict[str, Any] | None,
    *,
    tenant_id: UUID,
    connection_id: UUID | str,
    user_id: UUID | str | None = None,
) -> dict[str, Any] | None:
    """Filter a connection schema for the current requester (no-op for admins/system)."""
    scope = current_access_scope(db, tenant_id=tenant_id, user_id=user_id)
    if scope is None:
        return schema
    if not scope.can_use_connection(connection_id):
        raise AccessDeniedError()
    return filter_schema(schema, scope.allowed_schemas(connection_id))


# ----- execution-time check -----


def _table_owners(db: Session, *, tenant_id: UUID, connection_id: UUID) -> dict[str, set[str]]:
    """Bare table name (lowercase) → schemas (lowercase) that contain it."""
    owners: dict[str, set[str]] = {}
    rows = db.execute(
        select(SchemaTable.schema_name, SchemaTable.table_name).where(
            SchemaTable.tenant_id == tenant_id,
            SchemaTable.connection_id == connection_id,
        )
    ).all()
    for schema_name, table_name in rows:
        owners.setdefault(str(table_name).lower(), set()).add(str(schema_name or "").lower())
    return owners


def referenced_tables(sql: str, db_type: str | None) -> list[tuple[str | None, str]]:
    """(schema or None, table) for every physical table the SQL reads; CTE names excluded."""
    from sqlglot import exp

    from app.tools.sql_safety import _parse_all_sql, _sqlglot_dialect

    out: list[tuple[str | None, str]] = []
    for statement in _parse_all_sql(sql, _sqlglot_dialect(db_type)):
        cte_names = {str(c.alias).lower() for c in statement.find_all(exp.CTE) if c.alias}
        for table in statement.find_all(exp.Table):
            name = str(table.name or "").lower()
            if not name:
                continue
            schema = str(getattr(table, "db", None) or "").lower() or None
            if schema is None and name in cte_names:
                continue
            out.append((schema, name))
    return out


def assert_tables_allowed(
    tables: Iterable[tuple[str | None, str]],
    *,
    allowed: frozenset[str],
    owners: Mapping[str, set[str]],
) -> None:
    """Every table must sit in an allowed schema. An unqualified name is allowed
    only when every schema that has a table of that name is allowed, so the
    database's search path can never resolve it somewhere else."""
    for schema, name in tables:
        if schema is not None:
            if schema not in allowed:
                raise AccessDeniedError()
            continue
        found_in = owners.get(name)
        if not found_in or not found_in <= allowed:
            raise AccessDeniedError()


def enforce_query_access(
    db: Session,
    *,
    conn: Connection,
    sql: str,
    user_id: UUID | str | None = None,
) -> None:
    """Hard gate before customer SQL runs (see module docstring)."""
    scope = current_access_scope(db, tenant_id=conn.tenant_id, user_id=user_id)
    if scope is None or scope.unrestricted:
        return
    if not scope.can_use_connection(conn.id):
        raise AccessDeniedError()
    allowed = scope.allowed_schemas(conn.id)
    if allowed is None:
        return
    try:
        tables = referenced_tables(sql, str(conn.db_type))
    except Exception as exc:  # noqa: BLE001 — unparseable SQL can't be proven safe
        raise AccessDeniedError() from exc
    assert_tables_allowed(
        tables,
        allowed=allowed,
        owners=_table_owners(db, tenant_id=conn.tenant_id, connection_id=conn.id),
    )


def _allowed_for_current_requester(
    tenant_id: UUID | str, connection_id: UUID | str
) -> frozenset[str] | None:
    """Allowed schemas for the requester in context (None = everything).
    Raises AccessDeniedError when the connection itself isn't granted."""
    from app.llm.usage import current_usage_owner

    if current_usage_owner() is None:
        return None
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        scope = current_access_scope(db, tenant_id=_as_uuid(tenant_id))
    finally:
        db.close()
    if scope is None:
        return None
    if not scope.can_use_connection(connection_id):
        raise AccessDeniedError()
    return scope.allowed_schemas(connection_id)


def scope_agent_schema(
    schema: dict[str, Any] | None, *, tenant_id: UUID | str, connection_id: UUID | str
) -> dict[str, Any] | None:
    """Agent-side schema filter for the requester in context. A connection the
    requester may not use yields no schema at all."""
    if schema is None:
        return None
    try:
        allowed = _allowed_for_current_requester(tenant_id, connection_id)
    except AccessDeniedError:
        return None
    return filter_schema(schema, allowed)


def scope_agent_semantic(
    catalog: dict[str, Any], *, tenant_id: UUID | str, connection_id: UUID | str
) -> dict[str, Any]:
    try:
        allowed = _allowed_for_current_requester(tenant_id, connection_id)
    except AccessDeniedError:
        return {"metrics": [], "dimensions": [], "relationships": []}
    return filter_semantic_catalog(catalog, allowed) or catalog
