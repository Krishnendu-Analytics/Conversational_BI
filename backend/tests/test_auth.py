"""
Auth tests for signup / OTP / login flow.

Run from the project root:
  pytest -q
"""

from __future__ import annotations
from uuid import UUID


import pytest
from app.security import PASSWORD_POLICY_MESSAGE
from fastapi.testclient import TestClient


def test_signup_verify_login_and_me(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    signup = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "ali@acme.com", "password": "Password@123"},
    )
    assert signup.status_code == 201
    assert sent["to_email"] == "ali@acme.com"
    otp = sent["otp"]

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "ali@acme.com", "otp_code": otp},
    )
    assert verify.status_code == 200
    body = verify.json()
    token = body["access_token"]
    assert body["refresh_token"]
    UUID(body["tenant_id"])
    UUID(body["user_id"])

    me = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "ali@acme.com"
    UUID(me.json()["tenant_id"])

    login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "ali@acme.com", "password": "Password@123"},
    )
    assert login.status_code == 200
    assert login.json()["access_token"]
    assert login.json()["refresh_token"]


def test_refresh_reissues_access_and_keeps_refresh(
    client: tuple[TestClient, dict[str, str]],
) -> None:
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "ref@acme.com", "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "ref@acme.com", "otp_code": sent["otp"]},
    )
    old_access = verify.json()["access_token"]
    old_refresh = verify.json()["refresh_token"]

    refreshed = test_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert refreshed.status_code == 200
    new_access = refreshed.json()["access_token"]
    new_refresh = refreshed.json()["refresh_token"]
    assert new_access != old_access
    assert new_refresh == old_refresh

    me = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200

    reused = test_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert reused.status_code == 200
    assert reused.json()["refresh_token"] == old_refresh


def test_tenants_can_reuse_same_name(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, _sent = client
    first = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "a@acme.com", "password": "Password@123"},
    )
    assert first.status_code == 201

    second = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "b@acme.com", "password": "Password@123"},
    )
    assert second.status_code == 201


def test_unverified_signup_refresh_keeps_latest_otp(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    first = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "retry@acme.com", "password": "Password@123"},
    )
    assert first.status_code == 201
    old_otp = sent["otp"]

    second = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme Renamed", "email": "retry@acme.com", "password": "Password@456"},
    )
    assert second.status_code == 201
    new_otp = sent["otp"]
    assert new_otp != old_otp or True  # may collide rarely; verify behavior below

    bad = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "retry@acme.com", "otp_code": old_otp},
    )
    # If OTPs collided (1 in 1e6), both work; otherwise old must fail.
    if old_otp != new_otp:
        assert bad.status_code == 400

    good = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "retry@acme.com", "otp_code": new_otp},
    )
    assert good.status_code == 200

    # Password from second signup must work
    login = test_client.post("/api/v1/auth/login",
    json={"email": "retry@acme.com", "password": "Password@456"},
)
    assert login.status_code == 200


def test_verified_signup_rejected(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "v@acme.com", "password": "Password@123"},
    )
    test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "v@acme.com", "otp_code": sent["otp"]},
    )
    again = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme2", "email": "v@acme.com", "password": "Password@123"},
    )
    assert again.status_code == 400


def test_resend_otp_generic_and_cooldown(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "r@acme.com", "password": "Password@123"},
    )

    unknown = test_client.post("/api/v1/auth/resend-otp", json={"email": "nobody@acme.com"})
    assert unknown.status_code == 200

    cooled = test_client.post("/api/v1/auth/resend-otp", json={"email": "r@acme.com"})
    assert cooled.status_code == 429

    from app.routes import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "otp_resend_cooldown_seconds", 0)
    resent = test_client.post("/api/v1/auth/resend-otp", json={"email": "r@acme.com"})
    assert resent.status_code == 200
    assert sent["to_email"] == "r@acme.com"


def test_otp_login_for_verified_user(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After signup+verify, user can request a login OTP and sign in with it."""
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "otp@acme.com", "password": "Password@123"},
    )
    test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "otp@acme.com", "otp_code": sent["otp"]},
    )

    from app.routes import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "otp_resend_cooldown_seconds", 0)

    missing = test_client.post(
        "/api/v1/auth/request-login-otp",
        json={"email": "nobody@acme.com"},
    )
    assert missing.status_code == 404
    assert "Please register" in missing.json()["detail"]

    resent = test_client.post(
        "/api/v1/auth/request-login-otp",
        json={"email": "otp@acme.com"},
    )
    assert resent.status_code == 200
    login_otp = sent["otp"]

    otp_login = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "otp@acme.com", "otp_code": login_otp},
    )
    assert otp_login.status_code == 200
    assert otp_login.json()["access_token"]
    assert otp_login.json()["email"] == "otp@acme.com"


def test_me_requires_token(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, _sent = client
    response = test_client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_wrong_otp_is_rejected(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, _sent = client
    signup = test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Beta", "email": "beta@acme.com", "password": "Password@123"},
    )
    assert signup.status_code == 201

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "beta@acme.com", "otp_code": "000000"},
    )
    assert verify.status_code == 400


def test_logout_revokes_refresh_token(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": "out@acme.com", "password": "Password@123"},
    )
    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "out@acme.com", "otp_code": sent["otp"]},
    )
    access = verify.json()["access_token"]
    refresh = verify.json()["refresh_token"]

    logout = test_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access}"},
        json={"refresh_token": refresh},
    )
    assert logout.status_code == 200
    assert logout.json()["message"] == "Logged out successfully."

    reused = test_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh},
    )
    assert reused.status_code == 401

    no_auth = test_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh},
    )
    assert no_auth.status_code == 401


def test_signup_rejects_weak_passwords(
    client: tuple[TestClient, dict[str, str]],
) -> None:
    test_client, _sent = client
    weak = [
        "Ab1!",
        "12345678!",
        "Secret!!",
        "Secret12",
    ]
    for password in weak:
        response = test_client.post(
            "/api/v1/auth/signup",
            json={
                "tenant_name": "Acme",
                "email": "weak@acme.com",
                "password": password,
            },
        )
        assert response.status_code == 422, password
        assert PASSWORD_POLICY_MESSAGE in str(response.json())


def test_signup_accepts_complex_password(
    client: tuple[TestClient, dict[str, str]],
) -> None:
    test_client, _sent = client
    response = test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "Acme",
            "email": "strong@acme.com",
            "password": "Secret1!",
        },
    )
    assert response.status_code == 201
