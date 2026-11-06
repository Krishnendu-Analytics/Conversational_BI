"""Investigation intent detection and routing (Phase 3)."""

from __future__ import annotations

import app.agent.workflow as workflow_mod
from app.agent.workflow import Investigator, IntentClassifier


def _classify(monkeypatch, label: str, question: str, **state) -> str:
    class FakeLLM:
        def complete(self, prompt: str) -> str:
            return label

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    return IntentClassifier().run({"question": question, **state})["intent"]


def test_the_models_label_decides_not_keywords(monkeypatch) -> None:
    # The model understood a root-cause question (in any wording or language).
    assert _classify(monkeypatch, "investigate", "Why did our gross profit decline this month?") == "investigate"
    assert _classify(monkeypatch, "investigate", "मार्जिन क्यों गिरा?") == "investigate"
    # Keywords no longer overrule it: "why" alone is not an investigation.
    assert _classify(monkeypatch, "sql", "why is the total so high this month") == "sql"
    assert _classify(monkeypatch, "schema", "What tables are in this database?") == "schema"
    assert _classify(monkeypatch, "sql", "Show me top 10 customers") == "sql"


def test_an_unusable_reply_falls_back_to_a_data_question(monkeypatch) -> None:
    assert _classify(monkeypatch, "[llm stub] no key", "total sales") == "sql"
    assert _classify(monkeypatch, "banana", "total sales") == "sql"


def test_conversation_state_can_override_the_label(monkeypatch) -> None:
    from app.agent.analysis_state import AnalysisState

    pending = AnalysisState(pending_query_clarification={"kind": "pick", "choices": [1, 2]}).model_dump()
    # "the second one" answers the clarification, whatever the label says.
    assert _classify(monkeypatch, "greeting", "the second one", analysis_state=pending) == "sql"
    # Nothing to edit without a dashboard: build one instead.
    assert _classify(monkeypatch, "edit_dashboard", "add a trend chart") == "dashboard"


def test_route_after_intent_investigate() -> None:
    assert workflow_mod._route_after_intent({"intent": "investigate"}) == "investigate"


def test_investigate_intent_is_valid() -> None:
    assert "investigate" in workflow_mod._VALID_INTENTS


def test_investigator_node_requires_connection() -> None:
    result = Investigator().run({"question": "why did sales decline", "connection_id": ""})
    assert result["answer"].startswith("No database connection")
    assert result["error"] == "Missing connection_id for investigation."


def test_investigator_node_delegates_to_run_investigation(monkeypatch) -> None:
    import app.agent.investigator as investigator_mod

    captured = {}

    def _fake_run_investigation(question, **kwargs):
        captured["question"] = question
        captured["kwargs"] = kwargs
        return {"answer": "## Investigation: why did sales decline\n\n- Sales down 12%."}

    monkeypatch.setattr(investigator_mod, "run_investigation", _fake_run_investigation)

    result = Investigator().run(
        {
            "question": "why did sales decline",
            "connection_id": "c1",
            "tenant_id": "t1",
            "run_id": "r1",
            "conversation_id": "conv1",
            "user_id": "u1",
        }
    )

    assert captured["question"] == "why did sales decline"
    # tenant_id/conversation_id aren't valid UUIDs here, so the attached-
    # connections DB lookup fails and falls back to just the primary
    # connection — covered on its own in
    # test_investigator_node_uses_attached_connections below.
    assert captured["kwargs"]["connection_ids"] == ["c1"]
    assert captured["kwargs"]["tenant_id"] == "t1"
    assert "Sales down 12%" in result["answer"]


def test_investigator_node_uses_attached_connections(monkeypatch) -> None:
    """Phase 4: when the conversation has extra connections attached, the
    investigator checks all of them, not just the primary one."""
    import app.agent.investigator as investigator_mod
    import app.services.conversation_connections_service as conn_svc

    class _FakeDB:
        def close(self) -> None:
            pass

    monkeypatch.setattr(workflow_mod, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(
        conn_svc,
        "resolve_investigation_connection_ids",
        lambda db, **kwargs: [kwargs["primary_connection_id"], "c2"],
    )

    captured = {}

    def _fake_run_investigation(question, **kwargs):
        captured["kwargs"] = kwargs
        return {"answer": "ok"}

    monkeypatch.setattr(investigator_mod, "run_investigation", _fake_run_investigation)

    from uuid import uuid4

    tenant_id = str(uuid4())
    conversation_id = str(uuid4())

    result = Investigator().run(
        {
            "question": "why did sales decline",
            "connection_id": "c1",
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
        }
    )

    assert captured["kwargs"]["connection_ids"] == ["c1", "c2"]
    assert result["error"] is None


def test_insight_generator_passes_through_investigation_answer() -> None:
    from app.agent.workflow import InsightGenerator

    result = InsightGenerator().run(
        {
            "intent": "investigate",
            "question": "why did sales decline",
            "answer": "## Investigation: why did sales decline\n\n- Sales down 12%.",
        }
    )
    assert "Sales down 12%" in result["answer"]


def test_investigator_node_returns_evidence_and_domains_checked(monkeypatch) -> None:
    """Phase: evidence must survive past the Investigator node so it can be
    merged into AnalysisState — previously it was discarded after building
    the narrated answer string."""
    import app.agent.investigator as investigator_mod

    class _FakeDB:
        def add(self, row) -> None:
            pass

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(workflow_mod, "SessionLocal", lambda: _FakeDB())

    evidence = [
        {"domain": "Sales", "connection_id": "c1", "error": None, "facts": {"headline": "ok"}},
        {"domain": "Production", "connection_id": "c1", "error": "boom", "facts": None},
    ]
    monkeypatch.setattr(
        investigator_mod,
        "run_investigation",
        lambda question, **kwargs: {"answer": "ok", "evidence": evidence, "run_id": "r1"},
    )

    result = Investigator().run(
        {
            "question": "why did sales decline",
            "connection_id": "c1",
            "tenant_id": "t1",
            "conversation_id": "conv1",
            "user_id": "u1",
        }
    )

    assert result["investigation_evidence"] == evidence
    assert result["investigation_domains_checked"] == ["Production", "Sales"]


def test_investigator_node_writes_audit_log(monkeypatch) -> None:
    """Every completed investigation must leave an AuditLog trace — before
    this, nothing under app.agent ever called log_audit, so a multi-domain
    investigation's evidence vanished once the response was sent."""
    import app.agent.investigator as investigator_mod
    from uuid import uuid4

    recorded: list[object] = []

    class _FakeDB:
        def add(self, row) -> None:
            recorded.append(row)

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(workflow_mod, "SessionLocal", lambda: _FakeDB())

    evidence = [
        {
            "domain": "Sales",
            "connection_id": "c1",
            "error": None,
            "facts": {"ranked_highlights": ["Sales down 12%."]},
            "sql": "SELECT 1",
        }
    ]
    monkeypatch.setattr(
        investigator_mod,
        "run_investigation",
        lambda question, **kwargs: {"answer": "ok", "evidence": evidence, "run_id": "r1"},
    )

    tenant_id = str(uuid4())
    Investigator().run(
        {
            "question": "why did sales decline",
            "connection_id": "c1",
            "tenant_id": tenant_id,
            "conversation_id": "conv1",
            "user_id": "u1",
        }
    )

    assert len(recorded) == 1
    row = recorded[0]
    assert row.action == "agent.investigation"
    assert row.resource_type == "conversation"
    assert str(row.tenant_id) == tenant_id
    assert "Sales" in row.metadata_json


def test_investigator_node_audit_failure_never_breaks_the_answer(monkeypatch) -> None:
    """Audit logging is best-effort: a broken DB session must not turn a
    successful investigation into an error response."""
    import app.agent.investigator as investigator_mod

    def _broken_session_local():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(workflow_mod, "SessionLocal", _broken_session_local)
    monkeypatch.setattr(
        investigator_mod,
        "run_investigation",
        lambda question, **kwargs: {"answer": "Sales down 12%.", "evidence": []},
    )

    result = Investigator().run(
        {
            "question": "why did sales decline",
            "connection_id": "c1",
            "tenant_id": "t1",
        }
    )

    assert result["answer"] == "Sales down 12%."
    assert result["error"] is None


def test_full_graph_routes_root_cause_question_through_investigate(monkeypatch) -> None:
    """End-to-end wiring check: classify_intent -> investigate ->
    generate_insight -> END, through the actual compiled top-level graph —
    not just each piece unit-tested in isolation."""
    import app.agent.investigator as investigator_mod

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            return "investigate"  # the model recognises the root-cause question

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        investigator_mod,
        "run_investigation",
        lambda question, **kwargs: {
            "answer": "## Investigation: why did sales decline\n\n**Sales** — down 12%.",
            "evidence": [],
        },
    )

    graph = workflow_mod._build_graph()
    result = graph.invoke(
        {
            "tenant_id": "t1",
            "user_id": "u1",
            "connection_id": "c1",
            "question": "Why did sales decline this month?",
        }
    )

    assert result["intent"] == "investigate"
    assert "down 12%" in result["answer"]
