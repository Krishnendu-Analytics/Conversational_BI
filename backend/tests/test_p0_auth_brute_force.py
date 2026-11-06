"""P0-5: OTP verify and login brute-force protection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.redis_client import RateLimiter


def _signup(test_client: TestClient, sent: dict[str, str], email: str) -> None:
    test_client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": email, "password": "Password@123"},
    )


def test_otp_lockout_after_max_failures(client) -> None:
    test_client, sent = client
    email = "otp-lock@test.com"
    _signup(test_client, sent, email)
    settings = get_settings()

    for _ in range(settings.otp_verify_max_attempts):
        resp = test_client.post(
            "/api/v1/auth/verify-otp",
            json={"email": email, "otp_code": "000000"},
        )
        assert resp.status_code == 400

    locked = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    assert locked.status_code == 429


def test_new_otp_resets_verify_attempts(client) -> None:
    test_client, sent = client
    email = "otp-reset@test.com"
    _signup(test_client, sent, email)

    test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": "000000"},
    )
    test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": "000000"},
    )

    test_client.post("/api/v1/auth/resend-otp", json={"email": email})
    ok = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    assert ok.status_code == 200


def test_login_rate_limited(client, monkeypatch) -> None:
    test_client, sent = client
    email = "login-limit@test.com"
    _signup(test_client, sent, email)
    test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )

    RateLimiter._memory.clear()
    settings = get_settings()

    for _ in range(settings.login_max_failures):
        bad = test_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password"},
        )
        assert bad.status_code == 401

    blocked = test_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Password@123"},
    )
    assert blocked.status_code == 429


def test_otp_resend_blocked_while_locked(client) -> None:
    test_client, sent = client
    email = "otp-resend-lock@test.com"
    _signup(test_client, sent, email)
    settings = get_settings()

    for _ in range(settings.otp_verify_max_attempts):
        test_client.post(
            "/api/v1/auth/verify-otp",
            json={"email": email, "otp_code": "000000"},
        )

    from app.database import get_db

    override = test_client.app.dependency_overrides[get_db]
    gen = override()
    db = next(gen)
    try:
        from app.routes.auth import find_active_user_by_email

        user = find_active_user_by_email(db, email)
        assert user is not None
        assert user.otp_locked_until is not None
        locked_until = user.otp_locked_until
        attempts_before = user.otp_verify_attempts
        old_hash = user.otp_hash
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    resend = test_client.post("/api/v1/auth/resend-otp", json={"email": email})
    assert resend.status_code == 429

    gen = override()
    db = next(gen)
    try:
        user = find_active_user_by_email(db, email)
        assert user is not None
        assert user.otp_locked_until == locked_until
        assert user.otp_verify_attempts == attempts_before
        assert user.otp_hash == old_hash
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_request_login_otp_blocked_while_locked(client) -> None:
    test_client, sent = client
    email = "otp-request-lock@test.com"
    _signup(test_client, sent, email)
    settings = get_settings()

    for _ in range(settings.otp_verify_max_attempts):
        test_client.post(
            "/api/v1/auth/verify-otp",
            json={"email": email, "otp_code": "000000"},
        )

    blocked = test_client.post(
        "/api/v1/auth/request-login-otp",
        json={"email": email},
    )
    assert blocked.status_code == 429


def test_new_otp_after_lockout_expiry_resets_attempts(client, monkeypatch) -> None:
    test_client, sent = client
    email = "otp-expiry@test.com"
    _signup(test_client, sent, email)
    settings = get_settings()
    monkeypatch.setattr("app.routes.auth.settings.otp_resend_cooldown_seconds", 0)

    for _ in range(settings.otp_verify_max_attempts):
        test_client.post(
            "/api/v1/auth/verify-otp",
            json={"email": email, "otp_code": "000000"},
        )

    from datetime import timedelta

    from app.database import get_db
    from app.routes.auth import find_active_user_by_email, now_utc

    override = test_client.app.dependency_overrides[get_db]
    gen = override()
    db = next(gen)
    try:
        user = find_active_user_by_email(db, email)
        assert user is not None
        user.otp_locked_until = now_utc() - timedelta(minutes=1)
        user.otp_last_sent_at = now_utc() - timedelta(minutes=5)
        db.commit()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    resend = test_client.post("/api/v1/auth/resend-otp", json={"email": email})
    assert resend.status_code == 200

    ok = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    assert ok.status_code == 200

    gen = override()
    db = next(gen)
    try:
        user = find_active_user_by_email(db, email)
        assert user is not None
        assert user.otp_locked_until is None
        assert user.otp_verify_attempts == 0
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_successful_otp_verification_clears_lockout_state(client) -> None:
    test_client, sent = client
    email = "otp-success-clear@test.com"
    _signup(test_client, sent, email)

    test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": "000000"},
    )

    ok = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    assert ok.status_code == 200

    from app.database import get_db
    from app.routes.auth import find_active_user_by_email

    override = test_client.app.dependency_overrides[get_db]
    gen = override()
    db = next(gen)
    try:
        user = find_active_user_by_email(db, email)
        assert user is not None
        assert user.otp_locked_until is None
        assert user.otp_verify_attempts == 0
        assert user.otp_hash is None
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_login_atomic_limit_under_concurrency(monkeypatch) -> None:
    """Concurrent failed logins must not substantially exceed configured limit."""
    import threading

    from app.config import get_settings
    from app.redis_client import RateLimiter

    class AtomicFakeRedis:
        def __init__(self) -> None:
            self._data: dict[str, int] = {}
            self._lock = threading.Lock()

        def incr(self, key: str) -> int:
            with self._lock:
                self._data[key] = self._data.get(key, 0) + 1
                return self._data[key]

        def expire(self, key: str, seconds: int) -> None:
            return None

        def get(self, key: str) -> int | None:
            with self._lock:
                val = self._data.get(key)
                return val if val is not None else None

    fake = AtomicFakeRedis()
    RateLimiter._memory.clear()

    class FakeRedisClient:
        def _connect(self):
            return fake

    monkeypatch.setattr("app.redis_client.get_redis", lambda: FakeRedisClient())
    settings = get_settings()
    limiter = RateLimiter()
    key = "login_fail:concurrent@test.com"
    max_failures = settings.login_max_failures
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        limiter.record_failure(
            key,
            window_seconds=60,
            max_failures=max_failures,
        )

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final_count = fake.get(key) or 0
    assert final_count == 20


def test_independent_users_do_not_share_login_limit(client, monkeypatch) -> None:
    test_client, sent = client
    RateLimiter._memory.clear()
    settings = get_settings()

    for idx, email in enumerate(("user-a@test.com", "user-b@test.com")):
        _signup(test_client, sent, email)
        test_client.post(
            "/api/v1/auth/verify-otp",
            json={"email": email, "otp_code": sent["otp"]},
        )

    for _ in range(settings.login_max_failures):
        test_client.post(
            "/api/v1/auth/login",
            json={"email": "user-a@test.com", "password": "wrong"},
        )

    ok_b = test_client.post(
        "/api/v1/auth/login",
        json={"email": "user-b@test.com", "password": "Password@123"},
    )
    assert ok_b.status_code == 200
