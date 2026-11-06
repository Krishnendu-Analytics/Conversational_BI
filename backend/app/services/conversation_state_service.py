"""Load/save persistent conversation analysis state (PG source of truth, Redis cache)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.analysis_state import (
    MAX_RECENT_TURNS,
    MAX_TURN_ANSWER_LEN,
    MAX_TURN_QUESTION_LEN,
    AnalysisState,
    ConversationTurn,
    SemanticFocus,
    SourceScope,
    analysis_state_from_json,
    analysis_state_to_json,
    analysis_state_to_memory_blob,
    merge_workflow_into_state,
)
from app.authorization import get_user_conversation
from app.models import Conversation, Dashboard, Message, User
from app.redis_client import ConversationMemory
from app.serialize import json_safe
from app.services import memory_service
from app.services.conversation_service import (
    append_message,
    auto_title_from_question,
    ensure_conversation,
)

logger = logging.getLogger(__name__)


MAX_SAVED_VIZ_ROWS = 200


def _viz_metadata(final: dict[str, Any]) -> dict[str, Any]:
    """What the chart panel needs to redraw an answer when the conversation
    is reopened — without it, a reloaded chat shows an empty panel."""
    rows = final.get("rows")
    if not isinstance(rows, list) or not rows:
        return {}
    grounding = final.get("chat_grounding")
    kinds = grounding.get("column_kinds") if isinstance(grounding, dict) else None
    return {
        "viz": {
            "rows": json_safe(rows[:MAX_SAVED_VIZ_ROWS]),
            "row_count": len(rows),
            "chart_suggestion": final.get("chart_suggestion"),
            "column_kinds": kinds if isinstance(kinds, dict) else {},
        }
    }


class ConversationStateService:
    """PostgreSQL-backed analysis state with optional Redis write-through cache."""

    def __init__(self, memory: ConversationMemory | None = None) -> None:
        self.memory = memory or ConversationMemory()

    def load(
        self,
        db: Session,
        *,
        conversation_id: UUID,
        user: User,
    ) -> AnalysisState:
        conv = get_user_conversation(db, conversation_id=conversation_id, user=user)
        if conv is None:
            return AnalysisState()

        state = analysis_state_from_json(conv.analysis_state_json)
        if not state.last_sql and state.turn_count == 0:
            repaired = self._repair_from_messages(db, conv)
            if repaired is not None:
                state = repaired
        if not state.recent_turns:
            state.recent_turns = self._recent_turns_from_messages(db, conv)

        self._hydrate_dashboard(db, conv, state)
        self._sync_connection(conv, state)
        self._write_cache(user.tenant_id, conversation_id, state)
        return state

    def save(
        self,
        db: Session,
        *,
        conversation_id: UUID,
        user: User,
        question: str,
        final: dict[str, Any],
        connection_id: UUID | None = None,
    ) -> AnalysisState:
        existing = get_user_conversation(
            db, conversation_id=conversation_id, user=user
        )
        prior = analysis_state_from_json(
            existing.analysis_state_json if existing else "{}"
        )
        conv = ensure_conversation(
            db,
            conversation_id=conversation_id,
            user=user,
            connection_id=connection_id,
            title=auto_title_from_question(question) if prior.turn_count == 0 else None,
        )
        prior = analysis_state_from_json(conv.analysis_state_json)
        effective_connection = connection_id or conv.connection_id
        state = merge_workflow_into_state(
            prior,
            question=question,
            final=final,
            connection_id=effective_connection,
        )
        self._hydrate_dashboard(db, conv, state, from_final=final)

        conv.analysis_state_json = analysis_state_to_json(state)
        conv.updated_at = datetime.now(timezone.utc)

        append_message(
            db,
            conversation=conv,
            role="user",
            content=question,
            metadata={"intent": final.get("intent")},
        )
        answer = str(final.get("answer") or "")
        if answer:
            append_message(
                db,
                conversation=conv,
                role="assistant",
                content=answer,
                metadata={
                    "sql": final.get("sql"),
                    "answered_from": final.get("answered_from"),
                    "dashboard_id": (
                        (final.get("dashboard") or {}).get("id")
                        if isinstance(final.get("dashboard"), dict)
                        else None
                    ),
                    **_viz_metadata(final),
                },
            )
        if isinstance(final.get("dashboard"), dict) and final["dashboard"].get("id"):
            conv.dashboard_id = UUID(str(final["dashboard"]["id"]))

        db.commit()
        self._write_cache(user.tenant_id, conversation_id, state)
        memory_service.schedule_update(
            tenant_id=user.tenant_id,
            user_id=user.id,
            conversation_id=conversation_id,
            question=question,
            answer=answer,
            intent=final.get("intent"),
        )
        return state

    def load_for_context(
        self,
        db: Session,
        *,
        conversation_id: UUID,
        user: User,
    ) -> tuple[AnalysisState, int]:
        """Load state for REST /context (PG-first, repair if needed)."""
        conv = get_user_conversation(db, conversation_id=conversation_id, user=user)
        if conv is None:
            raise ValueError("Conversation not found")

        state = analysis_state_from_json(conv.analysis_state_json)
        if not state.last_sql and state.turn_count == 0:
            repaired = self._repair_from_messages(db, conv)
            if repaired is not None:
                state = repaired
                conv.analysis_state_json = analysis_state_to_json(state)
                db.commit()

        self._hydrate_dashboard(db, conv, state)
        self._sync_connection(conv, state)

        from sqlalchemy import func

        total = db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conv.id)
        )
        return state, int(total or 0)

    def remove_document(
        self,
        db: Session,
        *,
        conversation_id: UUID,
        user: User,
        document_id: UUID,
    ) -> AnalysisState | None:
        """Stop a conversation using a file (the file itself is kept)."""
        conv = get_user_conversation(db, conversation_id=conversation_id, user=user)
        if conv is None:
            return None
        state = analysis_state_from_json(conv.analysis_state_json)
        state.documents = [d for d in state.documents if d.id != document_id]
        scope = state.source_scope
        if document_id in scope.document_ids:
            scope.document_ids = [d for d in scope.document_ids if d != document_id]
            if scope.source == "documents" and not scope.document_ids:
                # The file the chat was pointed at is gone: back to auto.
                state.source_scope = SourceScope()
        conv.analysis_state_json = analysis_state_to_json(state)
        db.commit()
        self._write_cache(user.tenant_id, conversation_id, state)
        return state

    def _write_cache(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        state: AnalysisState,
    ) -> None:
        try:
            blob = analysis_state_to_memory_blob(state)
            self.memory.set_state(tenant_id, conversation_id, blob)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Redis cache write skipped: %s", exc)

    def _sync_connection(self, conv: Conversation, state: AnalysisState) -> None:
        if not conv.connection_id:
            return
        if (
            state.connection_id
            and state.connection_id != conv.connection_id
            and (state.last_sql or state.query_plan)
        ):
            state.query_plan = None
            state.last_sql = None
            state.semantic_focus = SemanticFocus()
        state.connection_id = conv.connection_id

    def _hydrate_dashboard(
        self,
        db: Session,
        conv: Conversation,
        state: AnalysisState,
        *,
        from_final: dict[str, Any] | None = None,
    ) -> None:
        dash_id = conv.dashboard_id
        if from_final and isinstance(from_final.get("dashboard"), dict):
            raw_id = from_final["dashboard"].get("id")
            if raw_id:
                try:
                    dash_id = UUID(str(raw_id))
                except (ValueError, TypeError):
                    pass

        if not dash_id:
            return

        dashboard = db.get(Dashboard, dash_id)
        if dashboard is None or dashboard.deleted_at is not None:
            return
        if dashboard.tenant_id != conv.tenant_id:
            return

        state.dashboard.dashboard_id = dashboard.id
        state.dashboard.title = dashboard.name
        try:
            filters = json.loads(dashboard.filters_json or "[]")
            if isinstance(filters, list):
                state.dashboard.filters = filters
        except json.JSONDecodeError:
            pass

        if from_final and isinstance(from_final.get("dashboard"), dict):
            widgets = from_final["dashboard"].get("widgets") or []
            if widgets and isinstance(widgets[0], dict):
                wid = widgets[0].get("id")
                if wid:
                    state.dashboard.active_widget_id = str(wid)

    def _recent_turns_from_messages(
        self,
        db: Session,
        conv: Conversation,
    ) -> list[ConversationTurn]:
        """Backfill the recent thread for conversations saved before
        AnalysisState carried it; later turns keep it up to date."""
        rows = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == conv.id)
                # A turn's question and answer share one transaction
                # timestamp; within a tie the answer sorts first here so the
                # reversed list reads question -> answer.
                .order_by(Message.created_at.desc(), (Message.role == "user").asc())
                .limit(MAX_RECENT_TURNS * 2)
            ).all()
        )
        turns: list[ConversationTurn] = []
        for row in reversed(rows):
            content = (row.content or "").strip()
            if row.role == "user" and content:
                turns.append(ConversationTurn(question=content[:MAX_TURN_QUESTION_LEN]))
            elif row.role == "assistant" and turns and not turns[-1].answer:
                turns[-1].answer = content[:MAX_TURN_ANSWER_LEN]
        return turns[-MAX_RECENT_TURNS:]

    def _repair_from_messages(
        self,
        db: Session,
        conv: Conversation,
    ) -> AnalysisState | None:
        """One-time backfill from last assistant message metadata."""
        rows = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conv.id,
                Message.role == "assistant",
            )
            .order_by(Message.created_at.desc())
            .limit(5)
        ).all()

        last_sql: str | None = None
        last_answer: str | None = None
        for row in rows:
            if last_answer is None:
                last_answer = row.content
            try:
                meta = json.loads(row.metadata_json or "{}")
            except json.JSONDecodeError:
                meta = {}
            if isinstance(meta, dict) and meta.get("sql"):
                last_sql = str(meta["sql"])
                break

        if not last_sql:
            return None

        state = AnalysisState(
            connection_id=conv.connection_id,
            last_sql=last_sql[:8192],
            last_assistant_answer=last_answer,
            turn_count=1,
            updated_at=datetime.now(timezone.utc),
        )
        if conv.dashboard_id:
            state.dashboard.dashboard_id = conv.dashboard_id
        return state
