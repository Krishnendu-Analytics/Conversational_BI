"""AI usage page API (/api/v1/usage)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.llm.usage import bucket_start
from app.main import app
from app.models import LLMUsage, Tenant, User
from app.services.usage_service import resolve_window

NOW = datetime(2026, 9, 29, 8, 7, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("key", "step"),
    [("1h", 300), ("3h", 300), ("12h", 900), ("1d", 3600), ("1w", 21600), ("30d", 86400)],
)
def test_preset_windows_pick_readable_point_spacing(key: str, step: int) -> None:
    window = resolve_window(key, now=NOW)
    assert window.step == step
    assert window.end == NOW
    assert window.points <= 73


def test_custom_window_validation() -> None:
    with pytest.raises(ValueError):
        resolve_window("custom", start=NOW, end=NOW - timedelta(hours=1))
    with pytest.raises(ValueError):
        resolve_window("custom", start=NOW - timedelta(days=100), end=NOW)
    with pytest.raises(ValueError):
        resolve_window("custom")
    window = resolve_window("custom", start=NOW - timedelta(hours=2), end=NOW)
    assert window.step == 300


def _db() -> tuple[Session, object]:
    gen = app.dependency_overrides[get_db]()
    return next(gen), gen


def _signup(client: TestClient, sent: dict[str, str], email: str) -> dict[str, str]:
    client.post(
        "/api/v1/auth/signup",
        json={"tenant_name": "Acme", "email": email, "password": "Password@123"},
    )
    verify = client.post("/api/v1/auth/verify-otp", json={"email": email, "otp_code": sent["otp"]})
    return {"Authorization": f"Bearer {verify.json()['access_token']}"}


def _usage(tenant_id, user_id, *, minutes_ago: int, feature: str, prompt: int, completion: int) -> LLMUsage:
    return LLMUsage(
        tenant_id=tenant_id,
        user_id=user_id,
        bucket_start=bucket_start(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)),
        model="gpt-x",
        feature=feature,
        calls=1,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=0,
    )


def test_usage_org_and_me_scopes(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    headers = _signup(test_client, sent, "admin@acme.com")

    db, gen = _db()
    admin = db.scalar(select(User).where(User.email == "admin@acme.com"))
    colleague = User(
        tenant_id=admin.tenant_id, email="hr@acme.com", password_hash="x", is_verified=True, role="analyst"
    )
    other_tenant = Tenant(name="Other")
    db.add_all([colleague, other_tenant])
    db.flush()
    outsider = User(
        tenant_id=other_tenant.id, email="x@other.com", password_hash="x", is_verified=True
    )
    db.add(outsider)
    db.flush()
    db.add_all(
        [
            _usage(admin.tenant_id, admin.id, minutes_ago=10, feature="chat", prompt=100, completion=20),
            _usage(admin.tenant_id, admin.id, minutes_ago=30, feature="schema_discovery", prompt=500, completion=50),
            _usage(admin.tenant_id, colleague.id, minutes_ago=20, feature="chat", prompt=40, completion=10),
            # Outside the 1h window: counted all-time only.
            _usage(admin.tenant_id, admin.id, minutes_ago=60 * 30, feature="chat", prompt=1000, completion=0),
            # Another organization: never visible.
            _usage(other_tenant.id, outsider.id, minutes_ago=5, feature="chat", prompt=9999, completion=9),
        ]
    )
    db.commit()
    admin_id = str(admin.id)

    org = test_client.get("/api/v1/usage?range=1h", headers=headers)
    assert org.status_code == 200, org.text
    body = org.json()
    assert body["scope"] == "organization"
    assert body["totals"] == {"tokens": 720, "input_tokens": 640, "output_tokens": 80, "requests": 3}
    assert body["all_time"]["tokens"] == 1720
    assert sum(p["tokens"] for p in body["series"]) == 720
    assert [f["feature"] for f in body["features"]] == ["schema_discovery", "chat"]
    assert body["features"][1]["label"] == "Chat"
    assert [u["email"] for u in body["users"]] == ["admin@acme.com", "hr@acme.com"]
    assert "cost" not in org.text

    me = test_client.get("/api/v1/usage?range=1h&scope=me", headers=headers).json()
    assert me["totals"]["tokens"] == 670
    assert me["users"] is None

    admin_row = db.get(User, admin.id)
    admin_row.role = "analyst"
    db.commit()
    assert test_client.get("/api/v1/usage?scope=organization", headers=headers).status_code == 403
    default = test_client.get("/api/v1/usage?range=1h", headers=headers).json()
    assert default["scope"] == "me"
    assert admin_id not in {u["user_id"] for u in (default["users"] or [])}

    bad = test_client.get("/api/v1/usage?range=custom", headers=headers)
    assert bad.status_code == 400
    try:
        next(gen)
    except StopIteration:
        pass
