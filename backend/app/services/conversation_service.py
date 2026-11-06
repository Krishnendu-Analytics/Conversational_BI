"""Persist conversations and messages to the platform database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, Message, User


def ensure_conversation(
    db: Session,
    *,
    conversation_id: UUID,
    user: User,
    connection_id: UUID | None = None,
    title: str | None = None,
) -> Conversation:
    row = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == user.tenant_id,
            Conversation.user_id == user.id,
        )
    )
    if row is None:
        row = Conversation(
            id=conversation_id,
            tenant_id=user.tenant_id,
            user_id=user.id,
            title=(title or "New chat")[:200],
            connection_id=connection_id,
        )
        db.add(row)
    else:
        if row.deleted_at is not None:
            row.deleted_at = None
        if connection_id is not None:
            row.connection_id = connection_id
        if title:
            row.title = title[:200]
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    return row


def append_message(
    db: Session,
    *,
    conversation: Conversation,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        tenant_id=conversation.tenant_id,
        role=role,
        content=content,
        metadata_json=json.dumps(metadata or {}, default=str),
    )
    db.add(message)
    conversation.updated_at = datetime.now(timezone.utc)
    db.flush()
    return message


def auto_title_from_question(question: str) -> str:
    text = (question or "").strip()
    if not text:
        return "New chat"
    return text[:80] + ("…" if len(text) > 80 else "")
