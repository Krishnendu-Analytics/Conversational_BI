"""Auto mode: decide whether a question is about the database, the user's
uploaded files, or both.

The keyword/intent router only knew the database plus files already attached
to the chat, so "tell me about the challan" (a PDF uploaded earlier) was sent
to SQL and answered "the schema doesn't contain that". Vector similarity
alone can't decide either: a data question like "total sales last quarter"
scores almost as close to an invoice PDF as a real question about it. So:

1. search every ready file of the tenant for the question (cheap, pgvector),
2. if any file is a plausible match, ask the LLM — with the database's table
   names and a snippet of each candidate file — which source answers it.

No candidate file means no LLM call: the database answers as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent.document_dashboard import _parse_json as parse_llm_json
from app.agent.prompts import get_prompt
from app.config import get_settings
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

MAX_CANDIDATE_DOCS = 5
MAX_SNIPPET_CHARS = 350
MAX_TABLES_LISTED = 40

Source = Literal["database", "documents", "both"]


@dataclass
class CandidateDocument:
    id: str
    filename: str
    score: float = 0.0
    snippet: str = ""
    in_chat: bool = False


@dataclass
class SourceDecision:
    source: Source
    document_ids: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "document_ids": self.document_ids, "reason": self.reason}


def find_candidate_documents(
    db: Any,
    *,
    tenant_id: str,
    question: str,
    chat_documents: dict[str, str],
) -> list[CandidateDocument]:
    """Files that might answer ``question``: the best-matching ready files of
    the tenant, plus every file already in this chat (the user brought them
    in, so the router should always weigh them)."""
    from app.tools.rag import RAGRetrieveRequest, VectorDocumentRetriever

    settings = get_settings()
    response = VectorDocumentRetriever(
        db, min_similarity=settings.source_router_min_similarity
    ).retrieve(RAGRetrieveRequest(tenant_id=tenant_id, question=question, top_k=12))

    found: dict[str, CandidateDocument] = {}
    for chunk in response.chunks:  # best first
        doc = found.get(chunk.document_id)
        if doc is None:
            found[chunk.document_id] = CandidateDocument(
                id=chunk.document_id,
                filename=str(chunk.metadata.get("document_name") or "document"),
                score=chunk.score,
                snippet=" ".join(chunk.content.split())[:MAX_SNIPPET_CHARS],
            )
    candidates = sorted(found.values(), key=lambda d: d.score, reverse=True)[:MAX_CANDIDATE_DOCS]
    for doc in candidates:
        doc.in_chat = doc.id in chat_documents
    for doc_id, filename in chat_documents.items():
        if doc_id not in found:
            candidates.append(CandidateDocument(id=doc_id, filename=filename, in_chat=True))
    return candidates


def decide_source(
    question: str,
    *,
    candidates: list[CandidateDocument],
    table_names: list[str],
    conversation_context: str,
    llm: LLMClient,
) -> SourceDecision | None:
    """The LLM's pick, or None when it gave no usable answer (the caller then
    keeps the old behaviour)."""
    if not candidates:
        return None
    docs_block = "\n".join(
        f"[D{i}] {doc.filename}"
        + (" (already in this chat)" if doc.in_chat else "")
        + (f"\n     excerpt: {doc.snippet}" if doc.snippet else "")
        for i, doc in enumerate(candidates, start=1)
    )
    tables = ", ".join(table_names[:MAX_TABLES_LISTED]) or "(no tables discovered)"
    if len(table_names) > MAX_TABLES_LISTED:
        tables += f" and {len(table_names) - MAX_TABLES_LISTED} more"

    prompt = get_prompt("source_router").render(
        question=question,
        conversation_context=conversation_context or "None.",
        tables=tables,
        documents=docs_block,
    )
    try:
        data = parse_llm_json(llm.complete(prompt))
    except Exception as exc:  # noqa: BLE001 — routing falls back, never fails a turn
        logger.warning("source_router_llm_failed: %s", type(exc).__name__)
        return None
    if not data:
        return None

    source = str(data.get("source") or "").strip().lower()
    if source not in {"database", "documents", "both"}:
        return None
    ids: list[str] = []
    for ref in data.get("documents") or []:
        index = _doc_index(ref)
        if index is not None and 1 <= index <= len(candidates):
            doc_id = candidates[index - 1].id
            if doc_id not in ids:
                ids.append(doc_id)
    if source != "database" and not ids:
        # "Documents" without naming any: the best match is the one meant.
        ids = [candidates[0].id]
    if source == "database":
        ids = []
    return SourceDecision(source=source, document_ids=ids, reason=str(data.get("reason") or "")[:200])  # type: ignore[arg-type]


def _doc_index(ref: Any) -> int | None:
    text = str(ref).strip().upper().lstrip("[").rstrip("]")
    if text.startswith("D"):
        text = text[1:]
    try:
        return int(text)
    except ValueError:
        return None
