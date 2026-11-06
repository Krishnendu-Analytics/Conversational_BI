"""Files attached in a conversation stay usable on later turns (like
ChatGPT/Claude): remembered in the conversation state, searched on follow-ups,
used only when relevant, and removable."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent.analysis_state import AnalysisState, merge_workflow_into_state


def test_attached_files_join_the_conversation_once() -> None:
    doc_a, doc_b = uuid4(), uuid4()
    state = merge_workflow_into_state(
        AnalysisState(), question="q1", final={"answer": "a1",
        "attached_documents": [{"id": str(doc_a), "filename": "policy.pdf"}]},
    )
    state = merge_workflow_into_state(
        state, question="q2", final={"answer": "a2",
        "attached_documents": [{"id": str(doc_a), "filename": "policy.pdf"},
                               {"id": str(doc_b), "filename": "freight.xlsx"}]},
    )
    state = merge_workflow_into_state(state, question="q3", final={"answer": "a3"})
    assert [(d.id, d.filename) for d in state.documents] == [(doc_a, "policy.pdf"), (doc_b, "freight.xlsx")]


def test_earlier_files_route_data_questions_through_documents() -> None:
    from app.agent.workflow import _route_with_attachments

    assert _route_with_attachments("sql", [], ["d1"]) == "hybrid"
    assert _route_with_attachments("greeting", [], ["d1"]) == "greeting"  # "thanks" stays a greeting
    assert _route_with_attachments("dashboard", [], ["d1"]) == "dashboard"
    assert _route_with_attachments("sql", [], []) == "sql"


def test_short_follow_ups_are_searched_with_the_previous_question() -> None:
    from app.agent.workflow import _retrieval_query

    analysis = {"last_user_question": "How many days of credit do distributors get?"}
    assert _retrieval_query("What about export customers?", analysis) == (
        "How many days of credit do distributors get?\nWhat about export customers?"
    )
    long_question = "Give me the full breakdown of freight cost per tonne by route, mode and month for last year"
    assert _retrieval_query(long_question, analysis) == long_question
    assert _retrieval_query("What about export customers?", {}) == "What about export customers?"


def test_earlier_files_are_used_only_when_relevant(monkeypatch) -> None:
    from app.agent import workflow

    monkeypatch.setenv("RAG_FOLLOWUP_MIN_SIMILARITY", "0.30")
    workflow.get_settings.cache_clear()
    try:
        weak = {"conversation_document_ids": ["d1"], "rag_chunks": [{"score": 0.19}]}
        strong = {"conversation_document_ids": ["d1"], "rag_chunks": [{"score": 0.62}]}
        attached = {"attached_document_ids": ["d1"], "rag_chunks": [{"score": 0.05}]}
        assert not workflow._documents_relevant(weak)
        assert workflow._documents_relevant(strong)
        assert workflow._documents_relevant(attached)  # this turn's files always count
    finally:
        workflow.get_settings.cache_clear()


def test_follow_up_answer_gets_the_chat_history(monkeypatch) -> None:
    from app.agent import workflow

    prompts: list[str] = []

    class LLM:
        def complete(self, prompt: str, max_output_tokens=None) -> str:
            prompts.append(prompt)
            return "Export customers pay 50% in advance. [Source 1]"

    monkeypatch.setattr(workflow, "get_llm", lambda: LLM())
    state = {
        "intent": "hybrid",
        "question": "What about export customers?",
        "attached_document_ids": [],
        "conversation_document_ids": ["d1"],
        "rag_chunks": [{"score": 0.7, "content": "Export customers must pay 50% in advance."}],
        "rag_context": "[Source 1] Export customers must pay 50% in advance.",
        "rag_insufficient": False,
        "analysis_state": {
            "turn_count": 1,
            "recent_turns": [{"question": "How many days of credit do distributors get?", "answer": "45 days."}],
            "last_user_question": "How many days of credit do distributors get?",
        },
        "answer": "The query returned no rows.",
        "rows": [],
        "steps": [],
    }
    update = workflow.InsightGenerator().run(state)
    assert update["answer"].startswith("Export customers pay 50% in advance")
    assert "How many days of credit do distributors get?" in prompts[0]


def test_conversation_lists_and_removes_its_files(client: tuple[TestClient, dict[str, str]], monkeypatch) -> None:
    from sqlalchemy import select

    from app.agent.analysis_state import analysis_state_to_json
    from app.models import Conversation
    from tests.test_semantic_api import _signup_and_headers
    from tests.test_semantic_model_import import _db

    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "convdocs@acme.com")
    conv = test_client.post("/api/v1/conversations", headers=headers, json={"title": "t"}).json()
    doc_a, doc_b = uuid4(), uuid4()
    db, gen = _db()
    try:
        from uuid import UUID

        row = db.scalar(select(Conversation).where(Conversation.id == UUID(conv["id"])))
        state = AnalysisState(documents=[{"id": doc_a, "filename": "policy.pdf"}, {"id": doc_b, "filename": "freight.xlsx"}])
        row.analysis_state_json = analysis_state_to_json(state)
        db.commit()
    finally:
        next(gen, None)

    detail = test_client.get(f"/api/v1/conversations/{conv['id']}", headers=headers).json()
    assert [d["filename"] for d in detail["documents"]] == ["policy.pdf", "freight.xlsx"]

    remaining = test_client.delete(f"/api/v1/conversations/{conv['id']}/documents/{doc_a}", headers=headers)
    assert remaining.status_code == 200
    assert [d["filename"] for d in remaining.json()] == ["freight.xlsx"]
    detail = test_client.get(f"/api/v1/conversations/{conv['id']}", headers=headers).json()
    assert [d["filename"] for d in detail["documents"]] == ["freight.xlsx"]

    other = _signup_and_headers(test_client, sent, "someone@globex.com")
    denied = test_client.delete(f"/api/v1/conversations/{conv['id']}/documents/{doc_b}", headers=other)
    assert denied.status_code == 404
