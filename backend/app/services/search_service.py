"""Search a user's chats and dashboards.

Matches chat titles and message text, dashboard names and widget titles,
case-insensitively ("contains", not whole words, so "insig" finds
"insight"). On Postgres the ILIKE lookups are served by pg_trgm GIN indexes
(migration 0024), so they stay fast as history grows. Only the searching
user's own, non-deleted items are returned, matching the sidebar lists.
Message metadata (which can hold generated SQL) is never searched or
returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Conversation, Dashboard, DashboardWidget, Message

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 200
_SNIPPET_CHARS = 140
_SNIPPET_SCAN_ROWS = 400


@dataclass
class ChatHit:
    id: UUID
    title: str
    archived: bool
    updated_at: datetime | None
    snippet: str | None  # matching message text, None when the title matched


@dataclass
class DashboardHit:
    id: UUID
    name: str
    updated_at: datetime | None
    snippet: str | None  # matching widget title, None when the name matched


def _like(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_MARKDOWN = re.compile(r"\*\*|__|`+|^\s{0,3}#{1,6}\s+|^\s*[-*+]\s+|\|", re.MULTILINE)


def snippet(text: str, query: str, width: int = _SNIPPET_CHARS) -> str:
    """A window of ``text`` around the first match, on one line, without
    markdown markup (answers are stored as markdown)."""
    flat = " ".join(_MARKDOWN.sub(" ", text or "").split())
    at = flat.lower().find(query.lower())
    if at < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, at - width // 3)
    end = min(len(flat), start + width)
    start = max(0, end - width)
    piece = flat[start:end]
    return ("…" if start else "") + piece + ("…" if end < len(flat) else "")


def normalize_query(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()[:MAX_QUERY_LEN]


def search_chats(
    db: Session, *, tenant_id: UUID, user_id: UUID, query: str, limit: int = 20
) -> list[ChatHit]:
    pattern = _like(query)
    message_match = (
        select(Message.conversation_id)
        .where(Message.tenant_id == tenant_id, Message.content.ilike(pattern, escape="\\"))
    )
    rows = db.scalars(
        select(Conversation)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
            or_(
                Conversation.title.ilike(pattern, escape="\\"),
                Conversation.id.in_(message_match),
            ),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    ).all()
    if not rows:
        return []

    title_hits = {r.id for r in rows if query.lower() in (r.title or "").lower()}
    need_snippet = [r.id for r in rows if r.id not in title_hits]
    snippets: dict[UUID, str] = {}
    if need_snippet:
        messages = db.execute(
            select(Message.conversation_id, Message.content)
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id.in_(need_snippet),
                Message.content.ilike(pattern, escape="\\"),
            )
            .order_by(Message.created_at.desc())
            .limit(_SNIPPET_SCAN_ROWS)
        ).all()
        for conversation_id, content in messages:
            snippets.setdefault(conversation_id, snippet(content, query))

    return [
        ChatHit(
            id=r.id,
            title=r.title,
            archived=r.archived_at is not None,
            updated_at=r.updated_at,
            snippet=None if r.id in title_hits else snippets.get(r.id),
        )
        for r in rows
    ]


def search_dashboards(
    db: Session, *, tenant_id: UUID, user_id: UUID, query: str, limit: int = 10
) -> list[DashboardHit]:
    pattern = _like(query)
    widget_match = (
        select(DashboardWidget.dashboard_id)
        .where(
            DashboardWidget.tenant_id == tenant_id,
            DashboardWidget.title.ilike(pattern, escape="\\"),
        )
    )
    rows = db.scalars(
        select(Dashboard)
        .where(
            Dashboard.tenant_id == tenant_id,
            Dashboard.user_id == user_id,
            Dashboard.deleted_at.is_(None),
            or_(Dashboard.name.ilike(pattern, escape="\\"), Dashboard.id.in_(widget_match)),
        )
        .order_by(Dashboard.updated_at.desc())
        .limit(limit)
    ).all()
    if not rows:
        return []

    name_hits = {r.id for r in rows if query.lower() in (r.name or "").lower()}
    widget_titles: dict[UUID, str] = {}
    need = [r.id for r in rows if r.id not in name_hits]
    if need:
        for dashboard_id, title in db.execute(
            select(DashboardWidget.dashboard_id, DashboardWidget.title).where(
                DashboardWidget.dashboard_id.in_(need),
                DashboardWidget.title.ilike(pattern, escape="\\"),
            )
        ).all():
            widget_titles.setdefault(dashboard_id, title)

    return [
        DashboardHit(
            id=r.id,
            name=r.name,
            updated_at=r.updated_at,
            snippet=None if r.id in name_hits else widget_titles.get(r.id),
        )
        for r in rows
    ]
