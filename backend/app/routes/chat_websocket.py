"""Authenticated WebSocket chat — bridges Redis progress to the client.

Protocol
--------
Connect:  WS /api/v1/ws/chat?token=<access_jwt>

Client → server (ask):
  {"type": "ask", "question": "...", "connection_id": "<uuid>",
   "conversation_id": "<uuid|null>", "document_ids": ["<uuid>"],
   "scope": {"source": "auto|documents|database", "document_ids": [...]}}
  ``scope`` is sticky: omit it to keep the conversation's current one.

Server → client:
  - Progress events from Redis (intent_detected, sql_generated, …)
  - Final envelope: {"type": "done", "answer", "sql", "rows", "steps",
                     "error", "intent", "run_id", "chart_suggestion",
                     "rag_chunks", "rag_context", "conversation_id",
                     "answered_from": {"database": bool, "documents": [names]}}
  - Protocol errors: {"type": "error", "error": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.access import accessible_connection
from app.agent.analysis_state import SourceScope
from app.agent.events import progress_channel
from app.agent.service import run_question
from app.agent.types import AgentQuestion, AgentResult
from app.authorization import (
    authorize_conversation_for_ask,
    can_run_analytics,
    resolve_user_from_access_token,
)
from app.config import get_settings
from app.database import get_db
from app.llm.usage import set_usage_feature, set_usage_owner
from app.models import User
from app.serialize import json_safe

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# Custom close codes (4400–4499) for auth / policy failures.
WS_CLOSE_UNAUTHORIZED = 4401


class ChatAskMessage(BaseModel):
    """One user question over the chat socket."""

    type: str = "ask"
    question: str = Field(min_length=1, max_length=4000)
    connection_id: UUID
    conversation_id: UUID | None = None
    document_ids: list[UUID] = Field(default_factory=list)
    scope: SourceScope | None = None


class ConnectionManager:
    """Track active WebSocket clients by tenant + conversation/user room."""

    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}

    @staticmethod
    def room_key(
        tenant_id: UUID | str,
        *,
        conversation_id: UUID | str | None = None,
        user_id: UUID | str | None = None,
    ) -> str:
        scope = str(conversation_id or user_id or "anonymous")
        return f"{tenant_id}:{scope}"

    async def connect(self, websocket: WebSocket, room: str) -> None:
        await websocket.accept()
        self._rooms.setdefault(room, set()).add(websocket)

    def attach(self, websocket: WebSocket, room: str) -> None:
        """Move an already-accepted socket into a room (no re-accept)."""
        self._rooms.setdefault(room, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, room: str | None = None) -> None:
        if room is not None:
            clients = self._rooms.get(room)
            if not clients:
                return
            clients.discard(websocket)
            if not clients:
                self._rooms.pop(room, None)
            return

        empty: list[str] = []
        for key, clients in self._rooms.items():
            clients.discard(websocket)
            if not clients:
                empty.append(key)
        for key in empty:
            self._rooms.pop(key, None)

    def active_count(self, room: str | None = None) -> int:
        if room is None:
            return sum(len(clients) for clients in self._rooms.values())
        return len(self._rooms.get(room, ()))


manager = ConnectionManager()


def _result_to_done(result: AgentResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "done",
        "answer": result.answer,
        "sql": result.sql,
        # Postgres may return datetime / Decimal / UUID in row values.
        "rows": json_safe(result.rows),
        "steps": result.steps,
        "error": result.error,
        "intent": result.intent,
        "run_id": result.run_id,
        "chart_suggestion": result.chart_suggestion,
        "dashboard": json_safe(result.dashboard) if result.dashboard else None,
        "rag_chunks": json_safe(result.rag_chunks),
        "rag_context": result.rag_context,
        "answered_from": result.answered_from,
        "documents_added": result.documents_added,
        "connection_id": result.connection_id,
    }
    if result.chat_grounding is not None:
        payload["grounding"] = result.chat_grounding
    if result.query_clarification is not None:
        payload["query_clarification"] = json_safe(result.query_clarification)
    if result.dataset_id is not None:
        payload["dataset_id"] = result.dataset_id
    if result.dataset_display_name is not None:
        payload["dataset_display_name"] = result.dataset_display_name
    if result.dataset_clarification is not None:
        payload["dataset_clarification"] = json_safe(result.dataset_clarification)
    return payload


async def _forward_redis_progress(
    websocket: WebSocket,
    channel: str,
    stop: asyncio.Event,
) -> None:
    """Subscribe to Redis pub/sub and forward progress JSON to the socket.

    No-op when Redis is unavailable (tests / local without Redis). The
    workflow still completes and the handler sends a final ``done`` event.
    """
    settings = get_settings()
    url = (settings.redis_url or "").strip()
    if not url:
        await stop.wait()
        return

    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover
        logger.warning("redis.asyncio unavailable; progress streaming disabled")
        await stop.wait()
        return

    client = None
    pubsub = None
    try:
        client = aioredis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)

        while not stop.is_set():
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=0.25,
            )
            if stop.is_set():
                break
            if message is None:
                await asyncio.sleep(0.05)
                continue
            if message.get("type") != "message":
                continue
            raw = message.get("data")
            if not raw:
                continue
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            try:
                await websocket.send_json(payload)
            except Exception:  # noqa: BLE001 — client gone
                break
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning("Redis progress subscribe failed (%s); streaming disabled", exc)
        await stop.wait()
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


@router.websocket("/api/v1/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> None:
    """Live chat: JWT via ``?token=``, then ask messages with progress streaming."""
    if not token:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Missing token")
        return

    user = resolve_user_from_access_token(token, db)
    if user is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Invalid token")
        return

    tenant_id = user.tenant_id
    user_id = user.id
    # Every LLM call for this socket (incl. asyncio.to_thread work) counts
    # toward this user's usage.
    set_usage_owner(tenant_id, user_id)
    set_usage_feature("chat")

    default_room = ConnectionManager.room_key(tenant_id, user_id=user_id)
    await manager.connect(websocket, default_room)
    current_room = default_room

    try:
        await websocket.send_json(
            {
                "type": "ready",
                "user_id": str(user_id),
                "tenant_id": str(tenant_id),
            }
        )

        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "error": "Invalid JSON message"}
                )
                continue

            if not isinstance(data, dict):
                await websocket.send_json(
                    {"type": "error", "error": "Message must be a JSON object"}
                )
                continue

            msg_type = str(data.get("type") or "ask").lower()
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if msg_type != "ask":
                await websocket.send_json(
                    {"type": "error", "error": f"Unsupported message type: {msg_type}"}
                )
                continue

            if not can_run_analytics(user.role):
                await websocket.send_json(
                    {"type": "error", "error": "Insufficient permissions"}
                )
                continue

            try:
                ask = ChatAskMessage.model_validate(data)
            except ValidationError as exc:
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": "Invalid ask payload",
                        "detail": exc.errors(),
                    }
                )
                continue

            # Departments decide which connections a member may ask about.
            if accessible_connection(db, connection_id=ask.connection_id, user=user) is None:
                await websocket.send_json(
                    {"type": "error", "error": "Connection not found"}
                )
                continue

            if ask.conversation_id is not None:
                if not authorize_conversation_for_ask(
                    db,
                    conversation_id=ask.conversation_id,
                    user=user,
                ):
                    await websocket.send_json(
                        {"type": "error", "error": "Conversation not found"}
                    )
                    continue
                conversation_id = ask.conversation_id
            else:
                conversation_id = uuid4()

            ask_room = ConnectionManager.room_key(
                tenant_id,
                conversation_id=conversation_id,
                user_id=user_id,
            )
            if ask_room != current_room:
                manager.disconnect(websocket, current_room)
                manager.attach(websocket, ask_room)
                current_room = ask_room

            channel = progress_channel(
                str(tenant_id),
                conversation_id=str(conversation_id),
                user_id=str(user_id),
            )
            stop = asyncio.Event()
            listener = asyncio.create_task(
                _forward_redis_progress(websocket, channel, stop)
            )
            # Let the subscriber attach before the workflow publishes.
            await asyncio.sleep(0.05)

            payload = AgentQuestion(
                tenant_id=tenant_id,
                user_id=user_id,
                question=ask.question.strip(),
                connection_id=ask.connection_id,
                conversation_id=conversation_id,
                document_ids=list(ask.document_ids or []),
                source_scope=ask.scope.model_dump(mode="json") if ask.scope else None,
            )

            result: AgentResult | None = None
            try:
                result = await asyncio.to_thread(run_question, payload)
            except Exception as exc:  # noqa: BLE001 — surface as chat error
                logger.exception("run_question failed over WebSocket")
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": f"Agent failed: {type(exc).__name__}",
                    }
                )
            finally:
                stop.set()
                try:
                    await asyncio.wait_for(listener, timeout=2.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    listener.cancel()

            if result is None:
                continue

            done = _result_to_done(result)
            done["conversation_id"] = str(conversation_id)
            await websocket.send_json(done)

    except WebSocketDisconnect:
        logger.debug(
            "WebSocket disconnected user=%s tenant=%s",
            user_id,
            tenant_id,
        )
    finally:
        manager.disconnect(websocket)
