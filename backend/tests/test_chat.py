"""WebSocket chat: auth, ask → done, ConnectionManager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.agent.types import AgentResult
from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import Conversation, User
from app.routes.chat_websocket import ConnectionManager, WS_CLOSE_UNAUTHORIZED
from app.tools.customer_db import ConnectionTestResult
from tests.test_rbac import _set_role


def _signup_token(
    test_client: TestClient,
    sent: dict[str, str],
    email: str,
) -> str:
    signup = test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "Acme",
            "email": email,
            "password": "Password@123",
        },
    )
    assert signup.status_code == 201

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": email, "otp_code": sent["otp"]},
    )
    assert verify.status_code == 200
    return verify.json()["access_token"]


def _create_connection(test_client: TestClient, token: str, monkeypatch) -> str:
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda _db_type, _url: type(
            "Stub",
            (),
            {
                "test_connection": staticmethod(
                    lambda: ConnectionTestResult(ok=True, message="ok")
                )
            },
        )(),
    )
    create = test_client.post(
        "/api/v1/connections",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Customer DB",
            "db_type": "postgres",
            "connection_url": "postgresql://customer:customer@localhost:5433/customerdb",
        },
    )
    assert create.status_code == 201
    return create.json()["id"]


def test_ws_rejects_missing_token(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, _sent = client
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with test_client.websocket_connect("/api/v1/ws/chat"):
            pass
    assert exc_info.value.code == WS_CLOSE_UNAUTHORIZED


def test_ws_rejects_invalid_token(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, _sent = client
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with test_client.websocket_connect("/api/v1/ws/chat?token=not-a-jwt"):
            pass
    assert exc_info.value.code == WS_CLOSE_UNAUTHORIZED


def test_ws_ask_returns_done(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    token = _signup_token(test_client, sent, "chat@acme.com")
    connection_id = _create_connection(test_client, token, monkeypatch)

    def fake_run_question(payload):
        return AgentResult(
            answer="Revenue was $42",
            sql="SELECT 42 AS revenue",
            rows=[{"revenue": 42}],
            steps=["classify_intent", "generate_sql", "execute_query", "generate_insight"],
            intent="sql",
            run_id="run-test-1",
        )

    monkeypatch.setattr(
        "app.routes.chat_websocket.run_question",
        fake_run_question,
    )

    with test_client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert "user_id" in ready
        assert "tenant_id" in ready

        conv_id = str(uuid4())
        ws.send_json(
            {
                "type": "ask",
                "question": "What was revenue?",
                "connection_id": connection_id,
                "conversation_id": conv_id,
            }
        )

        done = ws.receive_json()
        assert done["type"] == "done"
        assert done["answer"] == "Revenue was $42"
        assert done["sql"] == "SELECT 42 AS revenue"
        assert done["rows"] == [{"revenue": 42}]
        assert done["intent"] == "sql"
        assert done["run_id"] == "run-test-1"
        assert done["conversation_id"] == conv_id
        assert "classify_intent" in done["steps"]


def test_ws_unknown_connection_error(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    token = _signup_token(test_client, sent, "chat2@acme.com")
    # Ensure auth path works; no connection created.
    monkeypatch.setattr(
        "app.routes.chat_websocket.run_question",
        lambda _p: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    with test_client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json(
            {
                "type": "ask",
                "question": "hello",
                "connection_id": str(uuid4()),
            }
        )
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["error"] == "Connection not found"


def test_ws_ping_pong(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    token = _signup_token(test_client, sent, "ping@acme.com")
    with test_client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_connection_manager_tracks_rooms() -> None:
    mgr = ConnectionManager()

    class FakeWs:
        pass

    ws = FakeWs()
    room = ConnectionManager.room_key("t1", user_id="u1")
    mgr.attach(ws, room)  # type: ignore[arg-type]
    assert mgr.active_count(room) == 1
    assert mgr.active_count() == 1
    mgr.disconnect(ws, room)  # type: ignore[arg-type]
    assert mgr.active_count(room) == 0
    assert mgr.active_count() == 0


def _grant_self_connection(test_client: TestClient, token: str, connection_id: str) -> None:
    """Members only reach connections their departments were granted."""
    headers = {"Authorization": f"Bearer {token}"}
    me = test_client.get("/api/v1/auth/me", headers=headers).json()
    dept = test_client.post(
        "/api/v1/org/departments", headers=headers, json={"name": "Analysts"}
    ).json()
    test_client.put(
        f"/api/v1/org/departments/{dept['id']}/members",
        headers=headers,
        json={"user_ids": [me["id"]]},
    ).raise_for_status()
    test_client.put(
        f"/api/v1/org/departments/{dept['id']}/grants",
        headers=headers,
        json={"grants": [{"connection_id": connection_id}]},
    ).raise_for_status()


def _ws_ask(
    test_client: TestClient,
    token: str,
    connection_id: str,
    *,
    conversation_id: str | None = None,
    question: str = "What was revenue?",
) -> dict:
    payload: dict = {
        "type": "ask",
        "question": question,
        "connection_id": connection_id,
    }
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id

    with test_client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json(payload)
        return ws.receive_json()


def test_ws_analyst_ask_allowed(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    token = _signup_token(test_client, sent, "analyst@acme.com")
    connection_id = _create_connection(test_client, token, monkeypatch)
    _grant_self_connection(test_client, token, connection_id)
    _set_role("analyst@acme.com", "analyst", test_client)
    run_calls: list = []

    def fake_run_question(payload):
        run_calls.append(payload)
        return AgentResult(
            answer="OK",
            sql="SELECT 1",
            rows=[{"value": 1}],
            steps=["execute_query"],
            intent="sql",
            run_id="run-analyst",
        )

    monkeypatch.setattr("app.routes.chat_websocket.run_question", fake_run_question)
    response = _ws_ask(test_client, token, connection_id)
    assert response["type"] == "done"
    assert len(run_calls) == 1


def test_ws_viewer_ask_denied(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    token = _signup_token(test_client, sent, "viewer-ws@acme.com")
    connection_id = _create_connection(test_client, token, monkeypatch)
    _set_role("viewer-ws@acme.com", "viewer", test_client)

    monkeypatch.setattr(
        "app.routes.chat_websocket.run_question",
        lambda _p: (_ for _ in ()).throw(AssertionError("run_question must not run")),
    )
    monkeypatch.setattr(
        "app.tools.customer_db.run_readonly_query",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("SQL must not run")),
    )

    response = _ws_ask(test_client, token, connection_id)
    assert response["type"] == "error"
    assert response["error"] == "Insufficient permissions"
    assert "sql" not in response
    assert "rows" not in response


def test_ws_cross_tenant_conversation_denied(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    token_a = _signup_token(test_client, sent, "ws-a@acme.com")
    token_b = _signup_token(test_client, sent, "ws-b@other.com")
    conn_a = _create_connection(test_client, token_a, monkeypatch)

    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db: Session = next(gen)
    try:
        user_b = db.scalar(select(User).where(User.email == "ws-b@other.com"))
        assert user_b is not None
        foreign_conversation = Conversation(
            tenant_id=user_b.tenant_id,
            user_id=user_b.id,
            title="Tenant B chat",
        )
        db.add(foreign_conversation)
        db.commit()
        db.refresh(foreign_conversation)
        foreign_id = str(foreign_conversation.id)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    monkeypatch.setattr(
        "app.routes.chat_websocket.run_question",
        lambda _p: (_ for _ in ()).throw(AssertionError("run_question must not run")),
    )

    response = _ws_ask(
        test_client,
        token_a,
        conn_a,
        conversation_id=foreign_id,
    )
    assert response["type"] == "error"
    assert response["error"] == "Conversation not found"
    assert "Tenant B" not in str(response)


def test_ws_expired_token_rejected(client: tuple[TestClient, dict[str, str]]) -> None:
    test_client, sent = client
    token = _signup_token(test_client, sent, "expired@acme.com")
    override = app.dependency_overrides.get(get_db)
    assert override is not None
    gen = override()
    db: Session = next(gen)
    try:
        user = db.scalar(select(User).where(User.email == "expired@acme.com"))
        assert user is not None
        settings = get_settings()
        expired = jwt.encode(
            {
                "sub": str(user.id),
                "tenant_id": str(user.tenant_id),
                "email": user.email,
                "type": "access",
                "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
            },
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with test_client.websocket_connect(f"/api/v1/ws/chat?token={expired}"):
            pass
    assert exc_info.value.code == WS_CLOSE_UNAUTHORIZED
