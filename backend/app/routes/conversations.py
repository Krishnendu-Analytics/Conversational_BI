"""Conversation CRUD and Redis-backed analyst memory."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import accessible_connection
from app.agent.analysis_state import analysis_state_from_json
from app.database import get_db
from app.models import Conversation, Message, User
from app.routes.auth import get_current_user, require_role
from app.schemas import (
    AnalysisStateResponse,
    ConversationConnectionCreate,
    ConversationConnectionResponse,
    ConversationCreate,
    ConversationDocumentResponse,
    ConversationListItem,
    ConversationMessageResponse,
    ConversationResponse,
    ConversationUpdate,
)
from app.services.conversation_connections_service import (
    attach_connection,
    detach_connection,
    list_attached_connections,
)
from app.services import memory_service
from app.services.conversation_state_service import ConversationStateService
from app.serialize import json_safe

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _message_response(row: Message) -> ConversationMessageResponse:
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return ConversationMessageResponse(
        id=row.id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        created_at=row.created_at,
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _conversation_response(row: Conversation, messages: list[Message] | None = None) -> ConversationResponse:
    state = analysis_state_from_json(row.analysis_state_json)
    return ConversationResponse(
        id=row.id,
        title=row.title,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        connection_id=row.connection_id,
        dashboard_id=row.dashboard_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
        messages=[_message_response(message) for message in messages or []],
        documents=[
            ConversationDocumentResponse(id=d.id, filename=d.filename) for d in state.documents
        ],
        source_scope=state.source_scope.model_dump(mode="json"),
    )


def _get_conversation(
    db: Session,
    *,
    conversation_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
) -> Conversation:
    row = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return row


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def create_conversation(
    request: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    if request.connection_id is not None:
        conn = accessible_connection(db, connection_id=request.connection_id, user=current_user)
        if conn is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")

    row = Conversation(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        title=request.title.strip(),
        connection_id=request.connection_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _conversation_response(row)


@router.get("", response_model=list[ConversationListItem])
def list_conversations(
    archived: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List conversations. ``archived=false`` (default) is the normal chat
    list; ``archived=true`` returns only archived ones, for an "Archived"
    view. Either way, soft-deleted conversations are always excluded."""
    archived_filter = (
        Conversation.archived_at.is_not(None)
        if archived
        else Conversation.archived_at.is_(None)
    )
    rows = db.scalars(
        select(Conversation)
        .where(
            Conversation.tenant_id == current_user.tenant_id,
            Conversation.user_id == current_user.id,
            Conversation.deleted_at.is_(None),
            archived_filter,
        )
        .order_by(Conversation.updated_at.desc())
    ).all()
    return [
        ConversationListItem(
            id=row.id,
            title=row.title,
            tenant_id=row.tenant_id,
            connection_id=row.connection_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            archived_at=row.archived_at,
        )
        for row in rows
    ]


@router.get("/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    messages = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == row.id)
            .order_by(Message.created_at)
        ).all()
    )
    return _conversation_response(row, messages)


@router.delete(
    "/{conversation_id}/documents/{document_id}",
    response_model=list[ConversationDocumentResponse],
)
def remove_conversation_document(
    conversation_id: UUID,
    document_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    """Stop this conversation using a file. The uploaded file is not deleted."""
    _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    state = ConversationStateService().remove_document(
        db, conversation_id=conversation_id, user=current_user, document_id=document_id
    )
    documents = state.documents if state else []
    return [ConversationDocumentResponse(id=d.id, filename=d.filename) for d in documents]


@router.patch("/{conversation_id}", response_model=ConversationResponse)
def update_conversation(
    conversation_id: UUID,
    request: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    row = _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    row.title = request.title.strip()
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _conversation_response(row)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    row = _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    row.deleted_at = datetime.now(timezone.utc)
    memory_service.forget_conversation(db, user=current_user, conversation_id=row.id)
    db.commit()


@router.post("/{conversation_id}/archive", response_model=ConversationResponse)
def archive_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    """Hide from the main chat list without deleting; reversible via unarchive."""
    row = _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    if row.archived_at is None:
        row.archived_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
    return _conversation_response(row)


@router.post("/{conversation_id}/unarchive", response_model=ConversationResponse)
def unarchive_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    row = _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    if row.archived_at is not None:
        row.archived_at = None
        db.commit()
        db.refresh(row)
    return _conversation_response(row)


@router.get("/{conversation_id}/context", response_model=AnalysisStateResponse)
def get_conversation_context(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return persisted analyst analysis state from PostgreSQL."""
    row = _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    svc = ConversationStateService()
    try:
        state, message_count = svc.load_for_context(
            db,
            conversation_id=conversation_id,
            user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return json_safe(
        {
            "conversation_id": str(conversation_id),
            "title": row.title,
            "message_count": message_count,
            "analysis_state": state.model_dump(mode="json"),
            "last_sql": state.last_sql,
            "turn_count": state.turn_count,
        }
    )


def _conversation_connection_response(row) -> ConversationConnectionResponse:
    return ConversationConnectionResponse(
        id=row.id,
        conversation_id=row.conversation_id,
        connection_id=row.connection_id,
        created_at=row.created_at,
    )


@router.get(
    "/{conversation_id}/connections",
    response_model=list[ConversationConnectionResponse],
)
def list_conversation_connections(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Extra connections this conversation can investigate across, beyond
    its primary connection_id — see app.agent.investigator (Phase 4)."""
    _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    rows = list_attached_connections(
        db, tenant_id=current_user.tenant_id, conversation_id=conversation_id
    )
    return [_conversation_connection_response(row) for row in rows]


@router.post(
    "/{conversation_id}/connections",
    response_model=ConversationConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_conversation_connection(
    conversation_id: UUID,
    request: ConversationConnectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    """Attach another connection so investigations on this conversation can
    check it too. The conversation's primary connection_id is unaffected —
    this is additive only. Idempotent: attaching an already-attached
    connection just returns the existing row."""
    _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    if accessible_connection(db, connection_id=request.connection_id, user=current_user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    try:
        row = attach_connection(
            db,
            tenant_id=current_user.tenant_id,
            conversation_id=conversation_id,
            connection_id=request.connection_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _conversation_connection_response(row)


@router.delete(
    "/{conversation_id}/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_conversation_connection(
    conversation_id: UUID,
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    _get_conversation(
        db,
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    removed = detach_connection(
        db,
        tenant_id=current_user.tenant_id,
        conversation_id=conversation_id,
        connection_id=connection_id,
    )
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not attached")
