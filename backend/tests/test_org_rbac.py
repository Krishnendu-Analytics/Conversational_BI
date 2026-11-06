"""Organizations, invites and department-scoped data access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import (
    AccessDeniedError,
    AccessScope,
    assert_tables_allowed,
    build_access_scope,
    enforce_query_access,
    filter_schema,
    filter_semantic_catalog,
    referenced_tables,
)
from app.database import get_db
from app.llm.usage import usage_owner
from app.main import app
from app.models import Connection, SchemaTable, Tenant, User

PASSWORD = "Password@123"


# ----- helpers -----


def _db() -> tuple[Session, object]:
    gen = app.dependency_overrides[get_db]()
    return next(gen), gen


def _close(gen) -> None:
    try:
        next(gen)
    except StopIteration:
        pass


def _signup_admin(client: TestClient, sent: dict[str, str], email: str) -> dict[str, str]:
    client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": email, "password": PASSWORD},
    )
    verify = client.post("/api/v1/auth/verify-otp", json={"email": email, "otp_code": sent["otp"]})
    assert verify.status_code == 200
    return {"Authorization": f"Bearer {verify.json()['access_token']}"}


def _login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _token_from(invite_url: str) -> str:
    return parse_qs(urlparse(invite_url).query)["token"][0]


def _make_super_admin(email: str) -> None:
    db, gen = _db()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.is_super_admin = True
        db.commit()
    finally:
        _close(gen)


def _add_connection(tenant_id: UUID, name: str, tables: list[tuple[str, str]]) -> UUID:
    """A saved connection with discovered (schema, table) rows — no real DB."""
    db, gen = _db()
    try:
        conn = Connection(
            tenant_id=tenant_id, name=name, db_type="postgres", encrypted_url="x"
        )
        db.add(conn)
        db.flush()
        for schema, table in tables:
            db.add(
                SchemaTable(
                    tenant_id=tenant_id,
                    connection_id=conn.id,
                    schema_name=schema,
                    table_name=table,
                    columns_json='{"columns": [], "foreign_keys": []}',
                )
            )
        db.commit()
        return conn.id
    finally:
        _close(gen)


def _invite(client: TestClient, headers, email: str, role: str = "analyst", departments=()):
    resp = client.post(
        "/api/v1/org/users/invite",
        headers=headers,
        json={"email": email, "role": role, "department_ids": [str(d) for d in departments]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _accept(client: TestClient, invite_url: str) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/accept-invite",
        json={"token": _token_from(invite_url), "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ----- signup / platform -----


def test_public_signup_can_be_disabled(client, monkeypatch) -> None:
    test_client, _ = client
    monkeypatch.setattr("app.routes.auth.settings.allow_public_signup", False)
    resp = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "x@acme.com", "password": PASSWORD},
    )
    assert resp.status_code == 403
    assert test_client.get("/api/v1/auth/config").json() == {"allow_public_signup": False}


def test_super_admin_creates_org_and_first_admin_joins_by_invite(client) -> None:
    test_client, sent = client
    _signup_admin(test_client, sent, "ops@platform.com")
    _make_super_admin("ops@platform.com")
    ops = _login(test_client, "ops@platform.com")
    assert test_client.get("/api/v1/auth/me", headers=ops).json()["is_super_admin"] is True

    created = test_client.post(
        "/api/v1/platform/orgs",
        headers=ops,
        json={"name": "Globex", "admin_email": "boss@globex.com"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # SendGrid isn't configured in tests, so the link comes back to share by hand.
    assert body["invite"]["email_sent"] is False
    invite_url = body["invite"]["invite_url"]

    preview = test_client.get(f"/api/v1/auth/invites/{_token_from(invite_url)}")
    assert preview.json() == {"email": "boss@globex.com", "tenant_name": "Globex"}

    # Invited-but-not-accepted users can't sign in.
    denied = test_client.post(
        "/api/v1/auth/login", json={"email": "boss@globex.com", "password": PASSWORD}
    )
    assert denied.status_code in (401, 403)

    boss = _accept(test_client, invite_url)
    me = test_client.get("/api/v1/auth/me", headers=boss).json()
    assert me["tenant_name"] == "Globex"
    assert me["role"] == "admin"
    assert me["is_super_admin"] is False

    # Invite links are single-use.
    reused = test_client.post(
        "/api/v1/auth/accept-invite",
        json={"token": _token_from(invite_url), "password": PASSWORD},
    )
    assert reused.status_code == 400

    orgs = test_client.get("/api/v1/platform/orgs", headers=ops).json()
    globex = next(o for o in orgs if o["name"] == "Globex")
    assert globex["admin_emails"] == ["boss@globex.com"]
    assert globex["user_count"] == 1


def test_platform_routes_need_super_admin(client) -> None:
    test_client, sent = client
    admin = _signup_admin(test_client, sent, "admin@acme.com")
    assert test_client.get("/api/v1/platform/orgs", headers=admin).status_code == 403


def test_disabled_org_cannot_sign_in(client) -> None:
    test_client, sent = client
    _signup_admin(test_client, sent, "ops@platform.com")
    _make_super_admin("ops@platform.com")
    ops = _login(test_client, "ops@platform.com")
    body = test_client.post(
        "/api/v1/platform/orgs",
        headers=ops,
        json={"name": "Globex", "admin_email": "boss@globex.com"},
    ).json()
    _accept(test_client, body["invite"]["invite_url"])

    resp = test_client.patch(
        f"/api/v1/platform/orgs/{body['org']['id']}", headers=ops, json={"disabled": True}
    )
    assert resp.json()["disabled"] is True
    login = test_client.post(
        "/api/v1/auth/login", json={"email": "boss@globex.com", "password": PASSWORD}
    )
    assert login.status_code == 403


# ----- org admin -----


def test_members_see_only_their_departments_connections(client) -> None:
    test_client, sent = client
    admin = _signup_admin(test_client, sent, "admin@acme.com")
    tenant_id = UUID(test_client.get("/api/v1/auth/me", headers=admin).json()["tenant_id"])
    hr_conn = _add_connection(tenant_id, "HR", [("hr", "employees")])
    acct_conn = _add_connection(tenant_id, "Accounts", [("finance", "ledger")])

    hr = test_client.post("/api/v1/org/departments", headers=admin, json={"name": "HR"}).json()
    test_client.put(
        f"/api/v1/org/departments/{hr['id']}/grants",
        headers=admin,
        json={"grants": [{"connection_id": str(hr_conn)}]},
    ).raise_for_status()

    invited = _invite(test_client, admin, "hr.person@acme.com", departments=[hr["id"]])
    assert invited["user"]["status"] == "invited"
    member = _accept(test_client, invited["invite"]["invite_url"])

    me = test_client.get("/api/v1/auth/me", headers=member).json()
    assert [d["name"] for d in me["departments"]] == ["HR"]

    names = [c["name"] for c in test_client.get("/api/v1/connections", headers=member).json()]
    assert names == ["HR"]
    assert {c["name"] for c in test_client.get("/api/v1/connections", headers=admin).json()} == {
        "HR",
        "Accounts",
    }
    # The other department's connection is invisible, not just forbidden.
    assert (
        test_client.get(f"/api/v1/connections/{acct_conn}/schema", headers=member).status_code
        == 404
    )
    created = test_client.post(
        "/api/v1/conversations",
        headers=member,
        json={"title": "x", "connection_id": str(acct_conn)},
    )
    assert created.status_code == 404
    # Members can't administer the org.
    assert test_client.get("/api/v1/org/users", headers=member).status_code == 403


def test_schema_grant_filters_schema_endpoint(client) -> None:
    test_client, sent = client
    admin = _signup_admin(test_client, sent, "admin@acme.com")
    tenant_id = UUID(test_client.get("/api/v1/auth/me", headers=admin).json()["tenant_id"])
    conn = _add_connection(tenant_id, "ERP", [("hr", "employees"), ("finance", "ledger")])
    dept = test_client.post("/api/v1/org/departments", headers=admin, json={"name": "HR"}).json()
    test_client.put(
        f"/api/v1/org/departments/{dept['id']}/grants",
        headers=admin,
        json={"grants": [{"connection_id": str(conn), "schema_name": "hr"}]},
    ).raise_for_status()
    invited = _invite(test_client, admin, "hr@acme.com", departments=[dept["id"]])
    member = _accept(test_client, invited["invite"]["invite_url"])

    schema = test_client.get(f"/api/v1/connections/{conn}/schema", headers=member).json()
    assert [t["table_name"] for t in schema["tables"]] == ["employees"]
    full = test_client.get(f"/api/v1/connections/{conn}/schema", headers=admin).json()
    assert {t["table_name"] for t in full["tables"]} == {"employees", "ledger"}


def test_org_keeps_at_least_one_admin(client) -> None:
    test_client, sent = client
    admin = _signup_admin(test_client, sent, "admin@acme.com")
    me = test_client.get("/api/v1/auth/me", headers=admin).json()
    assert (
        test_client.patch(
            f"/api/v1/org/users/{me['id']}", headers=admin, json={"role": "analyst"}
        ).status_code
        == 400
    )
    assert test_client.delete(f"/api/v1/org/users/{me['id']}", headers=admin).status_code == 400


def test_removed_member_loses_access(client) -> None:
    test_client, sent = client
    admin = _signup_admin(test_client, sent, "admin@acme.com")
    invited = _invite(test_client, admin, "temp@acme.com")
    _accept(test_client, invited["invite"]["invite_url"])
    assert (
        test_client.delete(f"/api/v1/org/users/{invited['user']['id']}", headers=admin).status_code
        == 204
    )
    login = test_client.post(
        "/api/v1/auth/login", json={"email": "temp@acme.com", "password": PASSWORD}
    )
    assert login.status_code == 401


def test_cannot_invite_email_from_another_org(client) -> None:
    test_client, sent = client
    admin = _signup_admin(test_client, sent, "admin@acme.com")
    _signup_admin(test_client, sent, "other@globex.com")
    resp = test_client.post(
        "/api/v1/org/users/invite", headers=admin, json={"email": "other@globex.com"}
    )
    assert resp.status_code == 400


def test_invited_users_survive_unverified_cleanup(client) -> None:
    from app.cleanup import soft_delete_stale_unverified_users

    test_client, sent = client
    admin = _signup_admin(test_client, sent, "admin@acme.com")
    invited = _invite(test_client, admin, "slow@acme.com")
    db, gen = _db()
    try:
        user = db.get(User, UUID(invited["user"]["id"]))
        user.created_at = datetime.now(timezone.utc) - timedelta(days=5)
        db.commit()
        soft_delete_stale_unverified_users(db)
        db.refresh(user)
        assert user.deleted_at is None
    finally:
        _close(gen)


# ----- enforcement (pure) -----


def test_referenced_tables_skips_ctes() -> None:
    sql = "WITH t AS (SELECT * FROM hr.employees) SELECT * FROM t JOIN payroll p ON 1=1"
    assert sorted(referenced_tables(sql, "postgres"), key=str) == sorted(
        [("hr", "employees"), (None, "payroll")], key=str
    )


def test_assert_tables_allowed_rules() -> None:
    allowed = frozenset({"hr"})
    owners = {"employees": {"hr"}, "ledger": {"finance"}, "people": {"hr", "finance"}}
    assert_tables_allowed([("hr", "employees"), (None, "employees")], allowed=allowed, owners=owners)
    for tables in (
        [("finance", "ledger")],  # other schema, qualified
        [(None, "ledger")],  # other schema, unqualified
        [(None, "people")],  # ambiguous: could resolve to finance.people
        [(None, "unknown_table")],  # not in the discovered schema at all
    ):
        with pytest.raises(AccessDeniedError):
            assert_tables_allowed(tables, allowed=allowed, owners=owners)


def test_filter_schema_and_semantic_catalog() -> None:
    schema = {
        "tables": [
            {"schema_name": "hr", "table_name": "employees", "foreign_keys": [
                {"column": "dept", "referred_table": "ledger", "referred_column": "id",
                 "referred_schema": "finance"},
            ]},
            {"schema_name": "finance", "table_name": "ledger", "foreign_keys": []},
        ],
        "schemas_used": ["hr", "finance"],
    }
    out = filter_schema(schema, frozenset({"hr"}))
    assert [t["table_name"] for t in out["tables"]] == ["employees"]
    assert out["tables"][0]["foreign_keys"] == []
    assert out["schemas_used"] == ["hr"]
    assert filter_schema(schema, None) is schema

    catalog = {
        "metrics": [
            {"name": "headcount", "physical_schema": "hr"},
            {"name": "revenue", "physical_schema": "finance"},
            {"name": "margin", "physical_table": "finance.ledger"},
            {"name": "custom"},
        ],
        "dimensions": [],
    }
    kept = filter_semantic_catalog(catalog, frozenset({"hr"}))
    assert [m["name"] for m in kept["metrics"]] == ["headcount", "custom"]


# ----- enforcement (against the platform DB) -----


def _seed_member(db: Session, *, role: str = "analyst") -> tuple[User, Connection, Connection]:
    from app.models import Department, DepartmentGrant, DepartmentMember

    tenant = Tenant(name="Acme")
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, email=f"{uuid4()}@acme.com", role=role,
                password_hash="x", is_verified=True)
    db.add(user)
    erp = Connection(tenant_id=tenant.id, name="ERP", db_type="postgres", encrypted_url="x")
    other = Connection(tenant_id=tenant.id, name="Other", db_type="postgres", encrypted_url="x")
    db.add_all([erp, other])
    db.flush()
    for schema, table in [("hr", "employees"), ("finance", "ledger")]:
        db.add(SchemaTable(tenant_id=tenant.id, connection_id=erp.id, schema_name=schema,
                           table_name=table, columns_json="{}"))
    dept = Department(tenant_id=tenant.id, name="HR")
    db.add(dept)
    db.flush()
    db.add(DepartmentMember(department_id=dept.id, user_id=user.id, tenant_id=tenant.id))
    db.add(DepartmentGrant(tenant_id=tenant.id, department_id=dept.id,
                           connection_id=erp.id, schema_name="HR"))
    db.commit()
    return user, erp, other


def test_enforce_query_access_blocks_other_schemas_and_connections(client) -> None:
    db, gen = _db()
    try:
        user, erp, other = _seed_member(db)
        enforce_query_access(db, conn=erp, sql="SELECT * FROM hr.employees", user_id=user.id)
        enforce_query_access(db, conn=erp, sql="SELECT * FROM employees", user_id=user.id)
        for sql in (
            "SELECT * FROM finance.ledger",
            "SELECT * FROM ledger",
            "SELECT * FROM hr.employees e JOIN finance.ledger l ON 1=1",
            "SELECT * FROM hr.employees WHERE id IN (SELECT id FROM finance.ledger)",
        ):
            with pytest.raises(AccessDeniedError):
                enforce_query_access(db, conn=erp, sql=sql, user_id=user.id)
        with pytest.raises(AccessDeniedError):
            enforce_query_access(db, conn=other, sql="SELECT 1", user_id=user.id)

        # Identity from the request context works the same as an explicit user_id.
        with usage_owner(user.tenant_id, user.id):
            with pytest.raises(AccessDeniedError):
                enforce_query_access(db, conn=erp, sql="SELECT * FROM finance.ledger")
        # No requester at all = platform job (discovery/profiling): unrestricted.
        enforce_query_access(db, conn=erp, sql="SELECT * FROM finance.ledger")
    finally:
        _close(gen)


def test_admin_scope_is_unrestricted(client) -> None:
    db, gen = _db()
    try:
        user, erp, other = _seed_member(db, role="admin")
        scope = build_access_scope(db, tenant_id=user.tenant_id, user_id=user.id)
        assert scope.unrestricted
        enforce_query_access(db, conn=other, sql="SELECT * FROM x.y", user_id=user.id)
    finally:
        _close(gen)


def test_whole_connection_grant_wins_over_schema_grant() -> None:
    cid = uuid4()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4(), unrestricted=False,
                        grants={cid: None})
    assert scope.allowed_schemas(cid) is None
    assert scope.allowed_schemas(uuid4()) == frozenset()
