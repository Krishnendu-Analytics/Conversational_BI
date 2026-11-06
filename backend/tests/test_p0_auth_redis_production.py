"""P0-C: Production auth rate limiting requires Redis — no silent downgrade."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.redis_client import AuthRateLimitUnavailableError, RateLimiter, auth_rate_limit_status


def test_production_config_rejects_memory_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()

    class NoRedis:
        def _connect(self):
            return None

    monkeypatch.setattr("app.redis_client.get_redis", lambda: NoRedis())
    RateLimiter._memory.clear()

    limiter = RateLimiter()
    with pytest.raises(AuthRateLimitUnavailableError):
        limiter.ensure_distributed_backend()


def test_production_login_fails_closed_without_redis(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, sent = client
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()

    class NoRedis:
        def _connect(self):
            return None

    monkeypatch.setattr("app.redis_client.get_redis", lambda: NoRedis())

    email = "prod-login@test.com"
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": email, "password": "Password@123"},
    )
    test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )

    resp = test_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrong"},
    )
    assert resp.status_code == 503


def test_health_reports_auth_rate_limit_status(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _ = client
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()

    health = test_client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert "auth_rate_limits_ok" in body
    assert body["auth_rate_limits_mode"] == "development"


def test_auth_rate_limit_status_flags_production_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()

    class NoRedis:
        def _connect(self):
            return None

    monkeypatch.setattr("app.redis_client.get_redis", lambda: NoRedis())

    status = auth_rate_limit_status()
    assert status["ok"] is False
    assert status["redis_available"] is False
