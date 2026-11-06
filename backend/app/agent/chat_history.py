"""Questions about the user's own chat history ("which question did I ask
the most?", "what did we discuss yesterday?", "what do you remember about
me?").

These are about neither the database nor the files, so the SQL path answered
"the schema doesn't contain that". Here the source is the user's own saved
messages (every non-deleted chat of this user, never anyone else's) plus
what long-term memory holds; counts are computed in Python, and the LLM only
phrases the answer from those facts.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.prompts import get_prompt
from app.llm.client import LLMClient
from app.models import Conversation, Message, UserMemory

MAX_MESSAGES = 500
MAX_LISTED_QUESTIONS = 120
MAX_QUESTION_CHARS = 160
TOP_REPEATED = 10

def _normalize(question: str) -> str:
    text = re.sub(r"[^\w\s]", " ", question.lower())
    return " ".join(text.split())


def load_history_facts(db: Session, *, tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
    """What the user asked, across all their chats, reduced to facts."""
    rows = db.execute(
        select(Message.content, Message.created_at, Conversation.title, Conversation.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.tenant_id == tenant_id,
            Message.role == "user",
            Conversation.tenant_id == tenant_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
        .order_by(Message.created_at.desc())
        .limit(MAX_MESSAGES)
    ).all()

    questions = [(str(c or "").strip(), at, title, conv) for c, at, title, conv in rows if (c or "").strip()]
    counts: Counter[str] = Counter(_normalize(q) for q, *_ in questions)
    first_wording: dict[str, str] = {}
    for q, *_ in reversed(questions):  # oldest wording wins
        first_wording.setdefault(_normalize(q), q)
    repeated = [
        {"question": first_wording[key][:MAX_QUESTION_CHARS], "times": n}
        for key, n in counts.most_common(TOP_REPEATED)
        if n > 1
    ]
    dates = [at for _, at, *_ in questions if isinstance(at, datetime)]

    memories = db.execute(
        select(UserMemory.kind, UserMemory.content)
        .where(UserMemory.tenant_id == tenant_id, UserMemory.user_id == user_id)
        .order_by(UserMemory.updated_at.desc())
        .limit(40)
    ).all()

    return {
        "total_questions": len(questions),
        "total_chats": len({conv for *_, conv in questions}),
        "first_asked": min(dates).date().isoformat() if dates else None,
        "last_asked": max(dates).date().isoformat() if dates else None,
        "repeated": repeated,
        "recent": [
            {
                "question": q[:MAX_QUESTION_CHARS],
                "date": at.date().isoformat() if isinstance(at, datetime) else "",
                "chat": str(title or ""),
            }
            for q, at, title, _ in questions[:MAX_LISTED_QUESTIONS]
        ],
        "facts": [c for kind, c in memories if kind == "fact"],
        "chat_summaries": [c for kind, c in memories if kind == "conversation"][:10],
    }


def answer_history_question(question: str, facts: dict[str, Any], llm: LLMClient) -> str:
    if facts["total_questions"] == 0:
        return "You haven't asked me anything yet in your saved chats."

    repeated = "\n".join(f"- {r['times']}x: {r['question']}" for r in facts["repeated"]) or "(no exact repeats)"
    recent = "\n".join(
        f"- [{r['date']}] ({r['chat']}) {r['question']}" for r in facts["recent"]
    )
    prompt = get_prompt("chat_history_answer").render(
        question=question,
        total_questions=str(facts["total_questions"]),
        total_chats=str(facts["total_chats"]),
        first_asked=str(facts["first_asked"] or "unknown"),
        last_asked=str(facts["last_asked"] or "unknown"),
        repeated=repeated,
        recent=recent,
        facts="\n".join(f"- {f}" for f in facts["facts"]) or "(none)",
        summaries="\n".join(f"- {s}" for s in facts["chat_summaries"]) or "(none)",
    )
    try:
        answer = llm.complete(prompt).strip()
    except Exception:  # noqa: BLE001 — fall back to the plain facts
        answer = ""
    if answer and not answer.lower().startswith("[llm stub]"):
        return answer
    return _plain_answer(facts)


def _plain_answer(facts: dict[str, Any]) -> str:
    lines = [
        f"You've asked {facts['total_questions']} questions across {facts['total_chats']} chats."
    ]
    if facts["repeated"]:
        lines.append("Most repeated:")
        lines.extend(f"- {r['question']} ({r['times']} times)" for r in facts["repeated"][:5])
    else:
        lines.append("No question was asked more than once. Most recent:")
        lines.extend(f"- {r['question']}" for r in facts["recent"][:5])
    return "\n".join(lines)
