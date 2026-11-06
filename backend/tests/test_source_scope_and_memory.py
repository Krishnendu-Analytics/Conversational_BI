"""@-scoped sources (sticky per conversation) and chat memory: a rolling
summary per chat, plus per-user facts and past-chat recall across chats."""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.analysis_state import (
    AnalysisState,
    RecalledChat,
    format_analysis_state_context,
    merge_workflow_into_state,
)


# ----- source scope -----


def test_scope_overrides_the_router() -> None:
    from app.agent.workflow import _apply_source_scope

    # @file: data-looking questions are still answered from the file.
    assert _apply_source_scope("sql", "documents") == "rag"
    assert _apply_source_scope("hybrid", "documents") == "rag"
    assert _apply_source_scope("schema", "documents") == "rag"
    assert _apply_source_scope("dashboard", "documents") == "dashboard"
    assert _apply_source_scope("greeting", "documents") == "greeting"
    # @database: documents are never searched.
    assert _apply_source_scope("rag", "database") == "sql"
    assert _apply_source_scope("hybrid", "database") == "sql"
    assert _apply_source_scope("dashboard", "database") == "dashboard"


def test_scope_is_sticky_until_changed() -> None:
    from app.agent.workflow import _resolve_source_scope

    doc = uuid4()
    saved = {"source_scope": {"source": "documents", "document_ids": [str(doc)]}}
    scope, explicit = _resolve_source_scope(None, saved)
    assert (scope.source, scope.document_ids, explicit) == ("documents", [doc], False)

    scope, explicit = _resolve_source_scope({"source": "database"}, saved)
    assert (scope.source, explicit) == ("database", True)

    assert _resolve_source_scope(None, {})[0].source == "auto"
    assert _resolve_source_scope({"source": "nonsense"}, saved)[0].source == "auto"


def test_merge_persists_the_scope_only_when_sent() -> None:
    doc = uuid4()
    state = merge_workflow_into_state(
        AnalysisState(),
        question="q1",
        final={"answer": "a1", "source_scope": {"source": "documents", "document_ids": [str(doc)]}},
    )
    state = merge_workflow_into_state(state, question="q2", final={"answer": "a2"})
    assert state.source_scope.source == "documents"
    assert state.source_scope.document_ids == [doc]


def _run_capturing(monkeypatch, **kwargs) -> dict:
    """Run BIWorkflow with the graph stubbed out; return the initial state."""
    from app.agent import workflow

    seen: dict = {}

    class Graph:
        def invoke(self, initial):
            seen.update(initial)
            return {**initial, "answer": "ok"}

    monkeypatch.setattr(
        workflow, "_ready_documents", lambda tenant, ids: {i: f"{i}.pdf" for i in ids}
    )
    wf = workflow.BIWorkflow()
    wf._graph = Graph()
    result = wf.run("What is the total?", tenant_id=str(uuid4()), connection_id="c1", **kwargs)
    return {"initial": seen, "result": result}


def test_documents_scope_treats_chosen_files_as_attached(monkeypatch) -> None:
    doc = str(uuid4())
    out = _run_capturing(
        monkeypatch, document_ids=[doc], source_scope={"source": "documents", "document_ids": [doc]}
    )
    assert out["initial"]["source_scope"] == "documents"
    assert out["initial"]["attached_document_ids"] == [doc]
    assert out["initial"]["analysis_state"]["document_ids"] == [doc]
    assert out["result"]["source_scope"]["source"] == "documents"


def test_database_scope_hides_files_but_still_records_new_ones(monkeypatch) -> None:
    doc = str(uuid4())
    out = _run_capturing(monkeypatch, document_ids=[doc], source_scope={"source": "database"})
    assert out["initial"]["attached_document_ids"] == []
    assert "document_ids" not in out["initial"]["analysis_state"]
    assert out["result"]["attached_documents"] == [{"id": doc, "filename": f"{doc}.pdf"}]


def test_documents_scope_without_ready_files_falls_back_to_auto(monkeypatch) -> None:
    out = _run_capturing(monkeypatch, source_scope={"source": "documents", "document_ids": [str(uuid4())]})
    assert out["initial"]["source_scope"] == "auto"


def test_answered_from_names_the_sources_actually_used() -> None:
    from app.agent.workflow import _answered_from

    def used(final: dict) -> tuple[bool, list[str], bool]:
        out = _answered_from(final)
        return out["database"], out["documents"], out["history"]

    chunk = {"score": 0.8, "metadata": {"document_name": "policy.pdf"}}
    assert used({"intent": "rag", "rag_chunks": [chunk, chunk]}) == (False, ["policy.pdf"], False)
    assert used({"intent": "sql", "sql": "SELECT 1"}) == (True, [], False)
    # Earlier-turn file retrieved but not relevant: the database answered.
    weak = {**chunk, "score": 0.05}
    assert used(
        {"intent": "hybrid", "sql": "SELECT 1", "rag_chunks": [weak], "conversation_document_ids": ["d"]}
    ) == (True, [], False)
    doc_dash = {"build_mode": "document", "source_documents": [{"filename": "invoice.pdf"}], "widgets": [1]}
    assert used({"intent": "dashboard", "dashboard": doc_dash}) == (False, ["invoice.pdf"], False)
    assert used({"intent": "greeting"}) == (False, [], False)
    assert used({"intent": "history"}) == (False, [], True)


# ----- memory in the prompt context -----


def test_new_chat_context_carries_long_term_memory() -> None:
    state = AnalysisState(
        user_facts=["Focuses on the North region."],
        related_chats=[RecalledChat(title="Q2 dispatch", summary="Dispatch fell 8% in June.")],
    )
    ctx = format_analysis_state_context(state)
    assert "Focuses on the North region." in ctx
    assert "Q2 dispatch: Dispatch fell 8% in June." in ctx
    assert format_analysis_state_context(AnalysisState()) == "None."


def test_summary_covers_turns_older_than_the_recent_window() -> None:
    state = AnalysisState.model_validate(
        {
            "turn_count": 2,
            "conversation_summary": "Looked at sales.",
            "recent_turns": [{"question": "q1"}, {"question": "q2"}],
        }
    )
    assert "Looked at sales." not in format_analysis_state_context(state)
    state.turn_count = 9
    assert "Earlier in this conversation (summary): Looked at sales." in format_analysis_state_context(state)


# ----- memory service + API -----


def _user_and_chats(test_client, sent, email: str, titles: list[str]):
    from tests.test_semantic_api import _signup_and_headers

    headers = _signup_and_headers(test_client, sent, email)
    ids = [
        UUID(test_client.post("/api/v1/conversations", headers=headers, json={"title": t}).json()["id"])
        for t in titles
    ]
    return headers, ids


def test_memory_is_learned_and_recalled_in_another_chat(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    from app.models import User
    from app.services import memory_service
    from tests.test_semantic_model_import import _db

    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_RECALL_MIN_SIMILARITY", "0.1")
    memory_service.get_settings.cache_clear()

    class LLM:
        def complete(self, prompt: str, max_output_tokens=None) -> str:
            assert "Which plant had the most dispatch delays?" in prompt
            return (
                '{"summary": "User compared dispatch delays by plant; Pune plant had the most.",'
                ' "new_facts": ["Focuses on dispatch operations.", "Focuses on dispatch operations."]}'
            )

    monkeypatch.setattr(memory_service, "get_llm", lambda: LLM())
    test_client, sent = client
    headers, (first, second) = _user_and_chats(test_client, sent, "mem@acme.com", ["Dispatch delays", "New"])
    db, gen = _db()
    try:
        user = db.scalar(select(User).where(User.email == "mem@acme.com"))
        memory_service.update_after_turn(
            db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            conversation_id=first,
            question="Which plant had the most dispatch delays?",
            answer="Pune had 42 delayed dispatches.",
        )

        same_chat = memory_service.recall(db, user=user, conversation_id=first, question="and in May?")
        assert same_chat["conversation_summary"].startswith("User compared dispatch delays")
        assert same_chat["user_facts"] == ["Focuses on dispatch operations."]  # deduplicated
        assert "related_chats" not in same_chat  # never recalls itself

        other = memory_service.recall(
            db, user=user, conversation_id=second, question="dispatch delays by plant this month"
        )
        assert "conversation_summary" not in other
        assert other["related_chats"][0]["title"] == "Dispatch delays"

        # Opting out stops recall of facts and other chats.
        listing = test_client.put("/api/v1/memories/settings", headers=headers, json={"enabled": False})
        assert listing.json()["enabled"] is False
        db.expire_all()
        user = db.scalar(select(User).where(User.email == "mem@acme.com"))
        assert set(memory_service.recall(db, user=user, conversation_id=second, question="dispatch")) == set()
    finally:
        next(gen, None)
        memory_service.get_settings.cache_clear()


def test_memories_api_lists_deletes_and_forgets_deleted_chats(
    client: tuple[TestClient, dict[str, str]],
) -> None:
    from app.models import User, UserMemory
    from tests.test_semantic_model_import import _db

    test_client, sent = client
    headers, (chat,) = _user_and_chats(test_client, sent, "memapi@acme.com", ["Freight"])
    db, gen = _db()
    try:
        user = db.scalar(select(User).where(User.email == "memapi@acme.com"))
        fact = UserMemory(tenant_id=user.tenant_id, user_id=user.id, kind="fact", content="Prefers monthly view.")
        summary = UserMemory(
            tenant_id=user.tenant_id, user_id=user.id, kind="conversation",
            content="Freight cost per tonne by route.", conversation_id=chat,
        )
        db.add_all([fact, summary])
        db.commit()
        fact_id = fact.id
    finally:
        next(gen, None)

    body = test_client.get("/api/v1/memories", headers=headers).json()
    assert body["enabled"] is True
    assert {(m["kind"], m["content"]) for m in body["items"]} == {
        ("fact", "Prefers monthly view."),
        ("conversation", "Freight cost per tonne by route."),
    }
    assert next(m for m in body["items"] if m["kind"] == "conversation")["conversation_title"] == "Freight"

    other_headers, _ = _user_and_chats(test_client, sent, "intruder@globex.com", [])
    assert test_client.delete(f"/api/v1/memories/{fact_id}", headers=other_headers).status_code == 404
    assert test_client.delete(f"/api/v1/memories/{fact_id}", headers=headers).status_code == 204

    assert test_client.delete(f"/api/v1/conversations/{chat}", headers=headers).status_code == 204
    assert test_client.get("/api/v1/memories", headers=headers).json()["items"] == []


def test_conversation_detail_returns_the_scope(client: tuple[TestClient, dict[str, str]]) -> None:
    from app.agent.analysis_state import SourceScope, analysis_state_to_json
    from app.models import Conversation
    from tests.test_semantic_model_import import _db

    test_client, sent = client
    headers, (chat,) = _user_and_chats(test_client, sent, "scope@acme.com", ["t"])
    doc = uuid4()
    db, gen = _db()
    try:
        row = db.scalar(select(Conversation).where(Conversation.id == chat))
        row.analysis_state_json = analysis_state_to_json(
            AnalysisState(
                documents=[{"id": doc, "filename": "policy.pdf"}],
                source_scope=SourceScope(source="documents", document_ids=[doc]),
            )
        )
        db.commit()
    finally:
        next(gen, None)

    detail = test_client.get(f"/api/v1/conversations/{chat}", headers=headers).json()
    assert detail["source_scope"] == {"source": "documents", "document_ids": [str(doc)]}

    # Removing the scoped file puts the chat back on auto.
    test_client.delete(f"/api/v1/conversations/{chat}/documents/{doc}", headers=headers)
    detail = test_client.get(f"/api/v1/conversations/{chat}", headers=headers).json()
    assert detail["source_scope"]["source"] == "auto"


# ----- auto source router -----


def _decide(reply: str, n_docs: int = 2):
    from app.agent.source_router import CandidateDocument, decide_source

    class LLM:
        def __init__(self) -> None:
            self.prompt = ""

        def complete(self, prompt: str, max_output_tokens=None) -> str:
            self.prompt = prompt
            return reply

    llm = LLM()
    docs = [
        CandidateDocument(id=f"doc{i}", filename=f"file{i}.pdf", score=0.4, snippet=f"Challan No. {i}")
        for i in range(1, n_docs + 1)
    ]
    decision = decide_source(
        "tell me about the challan", candidates=docs, table_names=["fact_sales"],
        conversation_context="None.", llm=llm,  # type: ignore[arg-type]
    )
    return decision, llm.prompt


def test_router_reads_the_llm_choice() -> None:
    decision, prompt = _decide('{"source": "documents", "documents": ["D2"], "reason": "a challan"}')
    assert (decision.source, decision.document_ids) == ("documents", ["doc2"])
    assert "fact_sales" in prompt and "[D1] file1.pdf" in prompt and "Challan No. 1" in prompt

    decision, _ = _decide('{"source": "database", "documents": ["D1"]}')
    assert (decision.source, decision.document_ids) == ("database", [])

    # "documents" without naming one: the best match.
    decision, _ = _decide('```json\n{"source": "both"}\n```')
    assert (decision.source, decision.document_ids) == ("both", ["doc1"])

    assert _decide("[llm stub] no key")[0] is None
    assert _decide('{"source": "somewhere"}')[0] is None
    assert _decide('{"source": "documents", "documents": ["D9", "x"]}')[0].document_ids == ["doc1"]


def test_auto_mode_answers_from_a_file_the_router_picks(monkeypatch) -> None:
    from app.agent import workflow
    from app.agent.source_router import SourceDecision

    doc = str(uuid4())
    route = workflow._AutoRoute(
        decision=SourceDecision(source="documents", document_ids=[doc], reason="challan"),
        filenames={doc: "51 BS SGRL.pdf"},
        in_chat=set(),
    )
    monkeypatch.setattr(workflow, "_route_auto_source", lambda *a, **k: route)
    out = _run_capturing(monkeypatch)
    assert out["initial"]["source_scope"] == "documents"
    assert out["initial"]["attached_document_ids"] == [doc]
    # The file joins the chat, and the choice is recorded (scope stays auto).
    assert out["result"]["attached_documents"] == [{"id": doc, "filename": "51 BS SGRL.pdf"}]
    assert out["result"]["source_route"]["source"] == "documents"
    assert "source_scope" not in out["result"]


def test_router_is_skipped_when_the_user_chose_a_source(monkeypatch) -> None:
    from app.agent import workflow

    def boom(*a, **k):
        raise AssertionError("router must not run")

    monkeypatch.setattr(workflow, "_route_auto_source", boom)
    _run_capturing(monkeypatch, source_scope={"source": "database"})
    _run_capturing(monkeypatch, document_ids=[str(uuid4())])  # files attached now


def test_router_database_choice_drops_chat_files(monkeypatch) -> None:
    from app.agent import workflow
    from app.agent.source_router import SourceDecision

    route = workflow._AutoRoute(SourceDecision(source="database"), {}, set())
    monkeypatch.setattr(workflow, "_route_auto_source", lambda *a, **k: route)
    out = _run_capturing(monkeypatch)
    assert out["initial"]["source_scope"] == "auto"
    assert out["initial"]["attached_document_ids"] == []
    assert out["result"]["attached_documents"] == []


# ----- questions about the user's own chat history -----


def test_history_answer_counts_the_users_own_questions(client: tuple[TestClient, dict[str, str]]) -> None:
    from datetime import datetime, timedelta, timezone

    from app.agent.chat_history import answer_history_question, load_history_facts
    from app.models import Conversation, Message, User
    from tests.test_semantic_model_import import _db

    test_client, sent = client
    _, (chat_a, chat_b, gone) = _user_and_chats(test_client, sent, "hist@acme.com", ["Challan", "Sales", "Deleted"])
    _, (other_chat,) = _user_and_chats(test_client, sent, "other@globex.com", ["Theirs"])
    db, gen = _db()
    try:
        user = db.scalar(select(User).where(User.email == "hist@acme.com"))
        other = db.scalar(select(User).where(User.email == "other@globex.com"))
        now = datetime.now(timezone.utc)
        asked = [
            (chat_a, user, "Tell me about the challan"),
            (chat_a, user, "tell me about the challan?"),
            (chat_b, user, "Tell me about the challan"),
            (chat_b, user, "top 5 products by sales"),
            (gone, user, "secret deleted question"),
            (other_chat, other, "Tell me about the challan"),
        ]
        for i, (conv, owner, text) in enumerate(asked):
            db.add(Message(conversation_id=conv, tenant_id=owner.tenant_id, role="user",
                           content=text, created_at=now - timedelta(minutes=10 - i)))
            db.add(Message(conversation_id=conv, tenant_id=owner.tenant_id, role="assistant", content="answer"))
        db.scalar(select(Conversation).where(Conversation.id == gone)).deleted_at = now
        db.commit()

        facts = load_history_facts(db, tenant_id=user.tenant_id, user_id=user.id)
    finally:
        next(gen, None)

    assert facts["total_questions"] == 4  # deleted chat and other users excluded
    assert facts["total_chats"] == 2
    assert facts["repeated"] == [{"question": "Tell me about the challan", "times": 3}]
    assert all("secret" not in r["question"] for r in facts["recent"])

    class LLM:
        prompt = ""

        def complete(self, prompt: str, max_output_tokens=None) -> str:
            LLM.prompt = prompt
            return "You asked about the challan most (3 times)."

    assert answer_history_question("which question i asked the most", facts, LLM()) == (
        "You asked about the challan most (3 times)."
    )
    assert "- 3x: Tell me about the challan" in LLM.prompt

    class NoLLM:
        def complete(self, prompt: str, max_output_tokens=None) -> str:
            return "[llm stub] missing key"

    plain = answer_history_question("which question i asked the most", facts, NoLLM())
    assert "Tell me about the challan (3 times)" in plain
