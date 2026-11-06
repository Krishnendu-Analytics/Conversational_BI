"""What the assistant remembers about the signed-in user: view, delete, opt out."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routes.auth import get_current_user
from app.services import memory_service

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


class MemoryItem(BaseModel):
    id: UUID
    kind: str  # "fact" | "conversation"
    content: str
    conversation_id: UUID | None = None
    conversation_title: str | None = None
    updated_at: datetime | None = None


class MemoryOverview(BaseModel):
    enabled: bool
    items: list[MemoryItem]


class MemorySettingsUpdate(BaseModel):
    enabled: bool


@router.get("", response_model=MemoryOverview)
def list_memories(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return MemoryOverview(
        enabled=current_user.memory_enabled,
        items=[MemoryItem(**m) for m in memory_service.list_memories(db, user=current_user)],
    )


@router.put("/settings", response_model=MemoryOverview)
def update_memory_settings(
    body: MemorySettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.memory_enabled = body.enabled
    db.commit()
    return list_memories(db=db, current_user=current_user)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(
    memory_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not memory_service.delete_memory(db, user=current_user, memory_id=memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_memories(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memory_service.clear_memories(db, user=current_user)
