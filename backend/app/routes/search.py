"""Sidebar search across the current user's chats and dashboards."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routes.auth import get_current_user
from app.schemas import SearchChatHit, SearchDashboardHit, SearchResponse
from app.services import search_service

router = APIRouter(prefix="/api/v1/search", tags=["search"])


@router.get("", response_model=SearchResponse)
def search(
    q: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Chats whose title or messages contain ``q``, and dashboards whose
    name or widget titles do. Queries shorter than 2 characters return
    nothing."""
    query = search_service.normalize_query(q)
    if len(query) < search_service.MIN_QUERY_LEN:
        return SearchResponse(query=query, chats=[], dashboards=[])
    scope = {"tenant_id": current_user.tenant_id, "user_id": current_user.id, "query": query}
    chats = search_service.search_chats(db, **scope)
    dashboards = search_service.search_dashboards(db, **scope)
    return SearchResponse(
        query=query,
        chats=[
            SearchChatHit(
                id=c.id, title=c.title, archived=c.archived, updated_at=c.updated_at, snippet=c.snippet
            )
            for c in chats
        ],
        dashboards=[
            SearchDashboardHit(id=d.id, name=d.name, updated_at=d.updated_at, snippet=d.snippet)
            for d in dashboards
        ],
    )
