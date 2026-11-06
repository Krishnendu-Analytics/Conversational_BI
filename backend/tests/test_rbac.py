"""RBAC enforcement tests."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import User


def _auth_headers(client: TestClient, email: str, password: str) -> dict[str, str]:
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _set_role(email: str, role: str, client: TestClient) -> None:
    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db: Session = next(gen)
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role
        db.commit()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_signup_user_is_admin(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "admin@acme.com", "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "admin@acme.com", "otp_code": sent["otp"]},
    )
    assert verify.status_code == 200
    me = test_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {verify.json()['access_token']}"},
    )
    assert me.json()["role"] == "admin"


def test_viewer_cannot_create_connection(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "viewer@acme.com", "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "viewer@acme.com", "otp_code": sent["otp"]},
    )
    assert verify.status_code == 200
    _set_role("viewer@acme.com", "viewer", test_client)
    headers = _auth_headers(test_client, "viewer@acme.com", "Password@123")

    resp = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "Test",
            "db_type": "postgres",
            "connection_url": "postgresql://u:p@localhost:5432/db",
        },
    )
    assert resp.status_code == 403


def test_viewer_cannot_create_metric(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "v2@acme.com", "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "v2@acme.com", "otp_code": sent["otp"]},
    )
    assert verify.status_code == 200
    _set_role("v2@acme.com", "viewer", test_client)
    headers = _auth_headers(test_client, "v2@acme.com", "Password@123")

    resp = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": "00000000-0000-0000-0000-000000000001",
            "name": "revenue",
            "expression": "SUM(amt)",
        },
    )
    assert resp.status_code == 403
