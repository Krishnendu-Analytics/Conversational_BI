"""Chat memory beyond the last few turns.

Short-term: each conversation keeps a running summary, so turns that fall
out of ``AnalysisState.recent_turns`` are still known to the model.

Long-term (per user, never shared across users or tenants):
- facts: durable notes about the user ("focuses on the North region"),
- past chats: every conversation's summary is embedded, and a new question
  recalls the most similar earlier chats.

Both live in ``user_memories``. Updating needs an LLM call (on the cheaper
LLM_FAST_MODEL tier), so it runs in the background — and not after every
turn: a chat's turns are buffered and folded in with one call once the chat
has been quiet for MEMORY_UPDATE_DELAY_SECONDS, or as soon as enough turns
are buffered that the oldest would otherwise leave ``recent_turns`` before
being summarised. Buffered turns live in this process only; a restart before
the flush loses them from memory (never from the chat itself). Recall is a
couple of indexed queries plus one query embedding. Users can switch long-term memory off
(``User.memory_enabled``) and view/delete what is stored (routes/memories.py).
"""

from __future__ import annotations

import contextvars
import logging
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agent.analysis_state import MAX_RECENT_TURNS
from app.agent.document_dashboard import _parse_json as parse_llm_json
from app.agent.prompts import get_prompt
from app.config import get_settings
from app.database import SessionLocal
from app.llm.client import fast_llm, get_llm
from app.llm.usage import ContextThreadPoolExecutor, usage_feature
from app.models import Conversation, User, UserMemory
from app.services.embeddings import cosine_similarity, get_embedding_provider

logger = logging.getLogger(__name__)

KIND_FACT = "fact"
KIND_CONVERSATION = "conversation"

MAX_SUMMARY_LEN = 1200
MAX_FACT_LEN = 200
MAX_NEW_FACTS_PER_TURN = 3
_ANSWER_CHARS_FOR_UPDATE = 1500

# Turns with nothing worth remembering.
_SKIP_INTENTS = frozenset({"greeting", "history"})

_executor = ContextThreadPoolExecutor(max_workers=2, thread_name_prefix="chat-memory")
_sync_mode = False

# Flush before the oldest buffered turn drops out of the recent-turns window
# the model still sees verbatim.
_MAX_BUFFERED_TURNS = max(1, MAX_RECENT_TURNS - 1)


@dataclass
class _PendingTurns:
    tenant_id: UUID
    user_id: UUID
    turns: list[tuple[str, str]] = field(default_factory=list)
    timer: threading.Timer | None = None
    # The latest turn's context, so usage is attributed to its user.
    context: contextvars.Context | None = None


_pending: dict[UUID, _PendingTurns] = {}
_pending_lock = threading.Lock()


def set_sync_mode(enabled: bool) -> None:
    """Run memory updates inline (tests)."""
    global _sync_mode
    _sync_mode = enabled


# ----- recall (start of a turn) -----


def recall(
    db: Session,
    *,
    user: User,
    conversation_id: UUID | None,
    question: str,
) -> dict[str, Any]:
    """Memory for one turn, shaped as AnalysisState fields:
    ``conversation_summary``, ``user_facts``, ``related_chats``."""
    if not get_settings().memory_enabled:
        return {}
    out: dict[str, Any] = {}

    if conversation_id is not None:
        summary = db.scalar(
            select(UserMemory.content).where(
                UserMemory.user_id == user.id,
                UserMemory.kind == KIND_CONVERSATION,
                UserMemory.conversation_id == conversation_id,
            )
        )
        if summary:
            out["conversation_summary"] = summary

    if not user.memory_enabled:
        return out

    facts = db.scalars(
        select(UserMemory.content)
        .where(
            UserMemory.tenant_id == user.tenant_id,
            UserMemory.user_id == user.id,
            UserMemory.kind == KIND_FACT,
        )
        .order_by(UserMemory.updated_at.desc())
        .limit(get_settings().memory_max_facts)
    ).all()
    if facts:
        out["user_facts"] = list(reversed(facts))

    related = _related_chats(db, user=user, conversation_id=conversation_id, question=question)
    if related:
        out["related_chats"] = related
    return out


def _related_chats(
    db: Session,
    *,
    user: User,
    conversation_id: UUID | None,
    question: str,
) -> list[dict[str, str]]:
    settings = get_settings()
    stmt = (
        select(UserMemory.content, UserMemory.embedding, Conversation.title)
        .join(Conversation, Conversation.id == UserMemory.conversation_id)
        .where(
            UserMemory.tenant_id == user.tenant_id,
            UserMemory.user_id == user.id,
            UserMemory.kind == KIND_CONVERSATION,
            UserMemory.embedding.is_not(None),
            Conversation.deleted_at.is_(None),
        )
    )
    if conversation_id is not None:
        stmt = stmt.where(UserMemory.conversation_id != conversation_id)
    # Newest chats first; recall is over a user's own recent history.
    rows = db.execute(stmt.order_by(UserMemory.updated_at.desc()).limit(200)).all()
    if not rows or not question.strip():
        return []
    try:
        query_vec = get_embedding_provider().embed_query(question)
    except Exception as exc:  # noqa: BLE001 — recall is optional
        logger.warning("memory_recall_embed_failed: %s", type(exc).__name__)
        return []

    scored: list[tuple[float, str, str]] = []
    for content, embedding, title in rows:
        if embedding is None:
            continue
        score = cosine_similarity(query_vec, list(embedding))
        if score >= settings.memory_recall_min_similarity:
            scored.append((score, str(title or ""), str(content)))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"title": title, "summary": content}
        for _score, title, content in scored[: settings.memory_recall_chats]
    ]


# ----- update (after a turn) -----


def schedule_update(
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    question: str,
    answer: str,
    intent: str | None,
) -> None:
    """Queue one finished turn to be folded into memory, off the request path."""
    settings = get_settings()
    if not settings.memory_enabled:
        return
    if intent in _SKIP_INTENTS or not question.strip() or not answer.strip():
        return
    if _sync_mode:
        _update_safely(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turns=[(question, answer)],
        )
        return

    flush_now = False
    with _pending_lock:
        pending = _pending.get(conversation_id)
        if pending is None:
            pending = _pending[conversation_id] = _PendingTurns(tenant_id, user_id)
        pending.turns.append((question, answer))
        pending.context = contextvars.copy_context()
        if pending.timer is not None:
            pending.timer.cancel()
            pending.timer = None
        if len(pending.turns) >= _MAX_BUFFERED_TURNS:
            flush_now = True
        else:
            pending.timer = threading.Timer(
                settings.memory_update_delay_seconds, _flush, args=(conversation_id,)
            )
            pending.timer.daemon = True
            pending.timer.start()
    if flush_now:
        _flush(conversation_id)


def _flush(conversation_id: UUID) -> None:
    """Fold a chat's buffered turns into memory with one LLM call."""
    with _pending_lock:
        pending = _pending.pop(conversation_id, None)
    if pending is None or not pending.turns:
        return
    if pending.timer is not None:
        pending.timer.cancel()
    kwargs = {
        "tenant_id": pending.tenant_id,
        "user_id": pending.user_id,
        "conversation_id": conversation_id,
        "turns": pending.turns,
    }
    context = pending.context or contextvars.copy_context()
    try:
        # submit() copies the *current* context into the worker; run it inside
        # the turn's context so the tokens are charged to that user.
        context.run(_executor.submit, _update_safely, **kwargs)
    except RuntimeError:  # interpreter shutting down
        pass


def _update_safely(**kwargs: Any) -> None:
    db = SessionLocal()
    try:
        with usage_feature("chat_memory"):
            update_after_turn(db, **kwargs)
    except Exception:  # noqa: BLE001 — memory must never break chat
        logger.exception("memory_update_failed")
        db.rollback()
    finally:
        db.close()


def update_after_turn(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    question: str = "",
    answer: str = "",
    turns: list[tuple[str, str]] | None = None,
) -> None:
    turns = turns or [(question, answer)]
    user = db.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        return
    row = db.scalar(
        select(UserMemory).where(
            UserMemory.user_id == user.id,
            UserMemory.kind == KIND_CONVERSATION,
            UserMemory.conversation_id == conversation_id,
        )
    )
    facts = _fact_rows(db, user) if user.memory_enabled else []

    # Several buffered turns share the answer budget so the prompt stays small.
    per_answer = max(500, _ANSWER_CHARS_FOR_UPDATE // len(turns))
    exchanges = "\n\n".join(
        f"User: {q.strip()}\nAssistant: {a.strip()[:per_answer]}" for q, a in turns
    )
    prompt = get_prompt("memory_update").render(
        summary=(row.content if row else "") or "(none yet)",
        exchanges=exchanges,
        facts="\n".join(f"- {f.content}" for f in facts) or "(none)",
    )
    data = parse_llm_json(fast_llm(get_llm()).complete(prompt))
    if not data:
        return

    summary = " ".join(str(data.get("summary") or "").split())[:MAX_SUMMARY_LEN]
    if summary:
        title = db.scalar(select(Conversation.title).where(Conversation.id == conversation_id))
        embedding = _embed(f"{title or ''}\n{summary}")
        if row is None:
            db.add(
                UserMemory(
                    tenant_id=tenant_id,
                    user_id=user.id,
                    kind=KIND_CONVERSATION,
                    content=summary,
                    conversation_id=conversation_id,
                    embedding=embedding,
                )
            )
        else:
            row.content = summary
            row.embedding = embedding

    if user.memory_enabled:
        _add_facts(db, user, facts, data.get("new_facts"))
    db.commit()


def _fact_rows(db: Session, user: User) -> list[UserMemory]:
    return list(
        db.scalars(
            select(UserMemory)
            .where(
                UserMemory.tenant_id == user.tenant_id,
                UserMemory.user_id == user.id,
                UserMemory.kind == KIND_FACT,
            )
            .order_by(UserMemory.updated_at.asc())
        ).all()
    )


def _add_facts(db: Session, user: User, existing: list[UserMemory], raw: Any) -> None:
    if not isinstance(raw, list):
        return
    known = {" ".join(f.content.lower().split()) for f in existing}
    added: list[UserMemory] = []
    for item in raw[:MAX_NEW_FACTS_PER_TURN]:
        text = " ".join(str(item or "").split())[:MAX_FACT_LEN]
        key = text.lower()
        if not text or key in known:
            continue
        known.add(key)
        fact = UserMemory(
            tenant_id=user.tenant_id, user_id=user.id, kind=KIND_FACT, content=text
        )
        db.add(fact)
        added.append(fact)
    # Keep the newest facts only.
    overflow = len(existing) + len(added) - get_settings().memory_max_facts
    for old in existing[: max(overflow, 0)]:
        db.delete(old)


def _embed(text: str) -> list[float] | None:
    try:
        return get_embedding_provider().embed_query(text)
    except Exception as exc:  # noqa: BLE001 — summary is still useful unembedded
        logger.warning("memory_embed_failed: %s", type(exc).__name__)
        return None


# ----- management (settings page) -----


def list_memories(db: Session, *, user: User) -> list[dict[str, Any]]:
    rows = db.execute(
        select(UserMemory, Conversation.title)
        .outerjoin(Conversation, Conversation.id == UserMemory.conversation_id)
        .where(UserMemory.tenant_id == user.tenant_id, UserMemory.user_id == user.id)
        .order_by(UserMemory.kind.desc(), UserMemory.updated_at.desc())
    ).all()
    return [
        {
            "id": mem.id,
            "kind": mem.kind,
            "content": mem.content,
            "conversation_id": mem.conversation_id,
            "conversation_title": title,
            "updated_at": mem.updated_at,
        }
        for mem, title in rows
        if mem.kind == KIND_FACT or mem.conversation_id is not None
    ]


def delete_memory(db: Session, *, user: User, memory_id: UUID) -> bool:
    result = db.execute(
        delete(UserMemory).where(
            UserMemory.id == memory_id,
            UserMemory.tenant_id == user.tenant_id,
            UserMemory.user_id == user.id,
        )
    )
    db.commit()
    return bool(result.rowcount)


def clear_memories(db: Session, *, user: User) -> int:
    result = db.execute(
        delete(UserMemory).where(
            UserMemory.tenant_id == user.tenant_id, UserMemory.user_id == user.id
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def forget_conversation(db: Session, *, user: User, conversation_id: UUID) -> None:
    """A deleted chat must not be recalled later. Caller commits."""
    db.execute(
        delete(UserMemory).where(
            UserMemory.user_id == user.id,
            UserMemory.kind == KIND_CONVERSATION,
            UserMemory.conversation_id == conversation_id,
        )
    )
