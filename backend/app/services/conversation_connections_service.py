"""Extra connections attached to a conversation, for the multi-domain
investigator to check alongside its primary connection (Phase 4).

Every function here is scoped to `ConversationConnection` only — the
primary `Conversation.connection_id` and everything that reads it (REST
conversation/dashboard endpoints, the single-shot chat path) is untouched.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Connection, ConversationConnection


def resolve_investigation_connection_ids(
    db: Session,
    *,
    tenant_id: UUID,
    conversation_id: UUID | None,
    primary_connection_id: str,
) -> list[str]:
    """Every connection an investigation should check for this conversation:
    the primary connection plus any extra ones explicitly attached.

    Falls back to just the primary when there's no conversation (e.g. a
    one-off question with no saved history) or nothing extra is attached —
    multi-connection investigation is opt-in, never assumed, so a
    conversation with no attached rows behaves exactly as it did before
    this table existed.
    """
    ids = [str(primary_connection_id)] if primary_connection_id else []
    if conversation_id is None:
        return ids

    extra = db.scalars(
        select(ConversationConnection.connection_id).where(
            ConversationConnection.tenant_id == tenant_id,
            ConversationConnection.conversation_id == conversation_id,
        )
    ).all()
    for connection_id in extra:
        connection_str = str(connection_id)
        if connection_str not in ids:
            ids.append(connection_str)
    return ids


def list_attached_connections(
    db: Session, *, tenant_id: UUID, conversation_id: UUID
) -> list[ConversationConnection]:
    return list(
        db.scalars(
            select(ConversationConnection).where(
                ConversationConnection.tenant_id == tenant_id,
                ConversationConnection.conversation_id == conversation_id,
            )
        ).all()
    )


def attach_connection(
    db: Session, *, tenant_id: UUID, conversation_id: UUID, connection_id: UUID
) -> ConversationConnection:
    """Idempotent: attaching an already-attached connection just returns
    the existing row rather than raising a uniqueness error."""
    conn = db.scalar(
        select(Connection).where(
            Connection.id == connection_id, Connection.tenant_id == tenant_id
        )
    )
    if conn is None:
        raise ValueError("Connection not found")

    existing = db.scalar(
        select(ConversationConnection).where(
            ConversationConnection.tenant_id == tenant_id,
            ConversationConnection.conversation_id == conversation_id,
            ConversationConnection.connection_id == connection_id,
        )
    )
    if existing is not None:
        return existing

    row = ConversationConnection(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        connection_id=connection_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def detach_connection(
    db: Session, *, tenant_id: UUID, conversation_id: UUID, connection_id: UUID
) -> bool:
    """Returns True if a row was removed, False if nothing matched."""
    row = db.scalar(
        select(ConversationConnection).where(
            ConversationConnection.tenant_id == tenant_id,
            ConversationConnection.conversation_id == conversation_id,
            ConversationConnection.connection_id == connection_id,
        )
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
