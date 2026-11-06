"""Supervisor agent: an LLM orchestrating specialist agents via tool calls."""

import json
from types import SimpleNamespace as NS

from app.agent.supervisor import DatabaseInfo, FileInfo, build_tools, run_supervisor


def _call(name: str, args: dict, call_id: str = "c1") -> NS:
    return NS(id=call_id, function=NS(name=name, arguments=json.dumps(args)))


class ScriptedLLM:
    """Replies with the scripted assistant messages, in order."""

    def __init__(self, *replies: NS) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.tools_offered: list[list[str]] = []

    def chat(self, messages, *, tools=None, max_output_tokens=None):
        self.seen.append(list(messages))
        self.tools_offered.append([t["function"]["name"] for t in tools or []])
        return self.replies.pop(0)


def _msg(content: str = "", *calls: NS) -> NS:
    return NS(content=content, tool_calls=list(calls) or None)


class Specialists:
    def __init__(self, answers: dict[str, dict]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, intent: str, question: str, overrides: dict) -> dict:
        self.calls.append((intent, question, overrides))
        return {"intent": intent, **self.answers[intent]}


FILES = [FileInfo(id="doc-1", filename="51 BS SGRL.pdf", snippet="Challan No. 51")]
PETRO = DatabaseInfo(id="conn-petro", name="petrochemical", db_type="postgres",
                     tables=["fact_sales", "fact_procurement"], selected=True)
SHOP = DatabaseInfo(id="conn-shop", name="webshop", db_type="mysql", tables=["customers", "orders"])


def _run(llm, specialists, files=FILES, source="auto", databases=(PETRO,), question="tell me about the challan"):
    return run_supervisor(
        question,
        llm=llm, run_specialist=specialists, databases=list(databases),
        files=files, context="None.", source=source, has_dashboard=False,
    )


def test_one_good_specialist_answer_is_returned_without_a_second_llm_call() -> None:
    llm = ScriptedLLM(_msg("", _call("search_documents", {"question": "the challan", "files": ["F1"]})))
    specialists = Specialists({"rag": {"answer": "Challan No. 51 for Waris Ali.", "rag_chunks": [{"x": 1}]}})
    result = _run(llm, specialists)
    # One agent: it gets the user's own words, not the supervisor's rewrite.
    assert specialists.calls == [("rag", "tell me about the challan", {"document_ids": ["doc-1"]})]
    assert result.answer is None and result.primary["answer"].startswith("Challan No. 51")
    assert len(llm.seen) == 1
    # The supervisor saw the tables and the file with its excerpt.
    system = llm.seen[0][0]["content"]
    assert "fact_procurement" in system and "[F1] 51 BS SGRL.pdf" in system and "Challan No. 51" in system


def test_empty_answer_makes_the_supervisor_try_another_agent() -> None:
    llm = ScriptedLLM(
        _msg("", _call("query_database", {"question": "the challan"})),
        _msg("", _call("search_documents", {"question": "the challan"}, "c2")),
        _msg("It is Challan No. 51 for Waris Ali Fish Company."),
    )
    specialists = Specialists({
        "sql": {"answer": "", "query_clarification": {"kind": "invalid_plan", "choices": []}},
        "rag": {"answer": "Challan No. 51.", "rag_chunks": [{"x": 1}]},
    })
    result = _run(llm, specialists)
    assert [c[0] for c in specialists.calls] == ["sql", "rag"]
    assert result.answer == "It is Challan No. 51 for Waris Ali Fish Company."
    assert [t["ok"] for t in result.trace] == [False, True]
    # The failed attempt was reported back to the model as a tool result.
    assert any(m.get("role") == "tool" for m in llm.seen[1])


def test_two_agents_in_one_step_are_combined_by_the_supervisor() -> None:
    llm = ScriptedLLM(
        _msg("", _call("search_documents", {"question": "challan total"}), _call("query_database", {"question": "procurement spend"}, "c2")),
        _msg("The challan totals 45,962 (file); procurement spend is 1.2M (database)."),
    )
    specialists = Specialists({
        "rag": {"answer": "Total 45,962.", "rag_chunks": [{"x": 1}]},
        "sql": {"answer": "1.2M", "rows": [{"spend": 1200000}]},
    })
    result = _run(llm, specialists)
    # Split across agents: each gets its own part of the question.
    assert [(c[0], c[1]) for c in specialists.calls] == [("rag", "challan total"), ("sql", "procurement spend")]
    assert result.answer.startswith("The challan totals")
    assert result.primary["intent"] == "sql"  # the run with rows feeds the chart panel


def test_greeting_is_answered_without_agents() -> None:
    llm = ScriptedLLM(_msg("Hi! Ask me about your data or your files."))
    specialists = Specialists({})
    result = _run(llm, specialists)
    assert result.runs == [] and result.answer.startswith("Hi!")


def test_a_crashing_agent_is_reported_not_fatal() -> None:
    llm = ScriptedLLM(
        _msg("", _call("query_database", {"question": "sales"})),
        _msg("Sorry, the database is unavailable right now."),
    )

    def boom(intent, question, overrides):
        raise RuntimeError("db down")

    result = _run(llm, boom)
    assert result.runs[0]["error"] == "RuntimeError"
    assert result.answer.startswith("Sorry")


def test_scope_limits_the_tools() -> None:
    names = lambda tools: {t["function"]["name"] for t in tools}  # noqa: E731
    auto = names(build_tools(source="auto", has_files=True, has_dashboard=True))
    assert {"query_database", "search_documents", "search_chat_history", "edit_dashboard"} <= auto
    docs_only = names(build_tools(source="documents", has_files=True, has_dashboard=True))
    assert "query_database" not in docs_only and "search_documents" in docs_only
    db_only = names(build_tools(source="database", has_files=True, has_dashboard=False))
    assert "search_documents" not in db_only and "edit_dashboard" not in db_only
    no_files = names(build_tools(source="auto", has_files=False, has_dashboard=False))
    assert "search_documents" not in no_files


def test_workflow_uses_the_supervisor_and_merges_sources(monkeypatch) -> None:
    from uuid import uuid4

    from app.agent import workflow
    from app.agent.supervisor import SupervisorResult

    monkeypatch.setattr(workflow, "_use_supervisor", lambda state: True)
    monkeypatch.setattr(workflow, "_ready_documents", lambda tenant, ids: {})
    chunk = {"score": 0.8, "metadata": {"document_name": "51 BS SGRL.pdf"}}
    outcome = SupervisorResult(
        answer="Combined answer.",
        runs=[
            {"intent": "rag", "rag_chunks": [chunk], "attached_document_ids": ["doc-1"], "steps": ["retrieve_docs"]},
            {"intent": "sql", "sql": "SELECT 1", "rows": [{"a": 1}], "steps": ["generate_sql"]},
        ],
        trace=[{"tool": "search_documents"}, {"tool": "query_database"}],
    )

    def fake_supervised(self, question, initial, *, source, available, attached_now):
        available["doc-1"] = "51 BS SGRL.pdf"
        return outcome

    monkeypatch.setattr(workflow.BIWorkflow, "_run_supervised", fake_supervised)
    wf = workflow.BIWorkflow()
    out = wf.run("compare the challan with procurement", tenant_id=str(uuid4()), connection_id="c1")
    assert out["answer"] == "Combined answer."
    assert out["rows"] == [{"a": 1}]
    assert out["answered_from"] == {
        "database": True, "documents": ["51 BS SGRL.pdf"], "history": False, "connection_ids": [],
    }
    assert out["attached_documents"] == [{"id": "doc-1", "filename": "51 BS SGRL.pdf"}]
    assert out["steps"][0] == "supervisor"


def test_pending_clarification_keeps_the_pipeline(monkeypatch) -> None:
    from app.agent import workflow

    monkeypatch.setenv("AGENT_MODE", "supervisor")
    workflow.get_settings.cache_clear()
    try:
        assert workflow._use_supervisor({})
        assert not workflow._use_supervisor({"pending_dataset_clarification": {"x": 1}})
        assert not workflow._use_supervisor({"dashboard": {"pending_operation": {"op": "add"}}})
    finally:
        workflow.get_settings.cache_clear()


def test_supervisor_picks_the_database_that_holds_the_data() -> None:
    llm = ScriptedLLM(_msg("", _call("query_database", {"question": "how many web orders", "database": "DB2"})))
    specialists = Specialists({"sql": {"answer": "1,204 orders.", "rows": [{"n": 1204}]}})
    _run(llm, specialists, databases=(PETRO, SHOP), question="how many web orders did we get")
    assert specialists.calls == [("sql", "how many web orders did we get", {"document_ids": [], "connection_id": "conn-shop"})]
    system = llm.seen[0][0]["content"]
    assert "[DB1] petrochemical (postgres, currently selected)" in system
    assert "[DB2] webshop (mysql)" in system and "customers, orders" in system


def test_no_database_label_means_the_selected_connection() -> None:
    llm = ScriptedLLM(_msg("", _call("query_database", {"question": "sales"})))
    specialists = Specialists({"sql": {"answer": "Sales 5M.", "rows": [{"s": 5}]}})
    _run(llm, specialists, databases=(PETRO, SHOP))
    assert specialists.calls == [("sql", "tell me about the challan", {"document_ids": []})]


def test_database_choice_is_only_offered_with_several_databases() -> None:
    def params(count):
        tools = build_tools(source="auto", has_files=False, has_dashboard=False, database_count=count)
        return next(t for t in tools if t["function"]["name"] == "query_database")["function"]["parameters"]["properties"]

    assert "database" not in params(1)
    assert params(2)["database"]["enum"] == ["DB1", "DB2"]
    no_db = build_tools(source="auto", has_files=True, has_dashboard=False, database_count=0)
    assert "query_database" not in {t["function"]["name"] for t in no_db}


def test_switching_database_drops_the_other_databases_follow_up_context(monkeypatch) -> None:
    from uuid import uuid4

    from app.agent import workflow
    from app.agent.supervisor import SupervisorResult

    seen: dict = {}

    class Graph:
        def invoke(self, state):
            seen.update(state)
            return {**state, "answer": "1,204 orders.", "sql": "SELECT 1", "rows": [{"n": 1}]}

    def fake_supervisor(question, *, run_specialist, **kwargs):
        final = run_specialist("sql", question, {"document_ids": [], "connection_id": "conn-shop"})
        return SupervisorResult(answer=None, runs=[final], trace=[])

    monkeypatch.setattr(workflow, "_use_supervisor", lambda state: True)
    monkeypatch.setattr(workflow, "_ready_documents", lambda tenant, ids: {})
    monkeypatch.setattr(workflow, "_tenant_databases", lambda tenant, selected: [PETRO, SHOP])
    monkeypatch.setattr(workflow, "run_supervisor", fake_supervisor)
    monkeypatch.setattr(workflow, "find_candidate_documents", lambda *a, **k: [])
    monkeypatch.setattr(workflow, "get_llm", lambda: type("L", (), {"client": object()})())
    wf = workflow.BIWorkflow()
    wf._graph = Graph()
    out = wf.run("how many web orders", tenant_id=str(uuid4()), connection_id="conn-petro")

    assert seen["connection_id"] == "conn-shop"
    assert "last_sql" not in seen["analysis_state"]
    assert out["connection_id"] == "conn-shop"
    assert out["answered_from"]["connection_ids"] == ["conn-shop"]


def test_dashboard_coverage_is_the_supervisors_choice() -> None:
    llm = ScriptedLLM(_msg("", _call("build_dashboard", {"request": "full overview", "coverage": "whole_database"})))
    specialists = Specialists({"dashboard": {"answer": "Built.", "dashboard": {"title": "Overview", "widgets": [1]}}})
    _run(llm, specialists, files=[], question="give me the complete picture of the business")
    assert specialists.calls == [
        ("dashboard", "give me the complete picture of the business", {"document_ids": [], "dashboard_scope": "whole_database"})
    ]

    llm = ScriptedLLM(_msg("", _call("build_dashboard", {"request": "sales"})))
    specialists = Specialists({"dashboard": {"answer": "Built.", "dashboard": {"title": "Sales", "widgets": [1]}}})
    _run(llm, specialists, files=[], question="sales dashboard")
    assert specialists.calls[0][2] == {"document_ids": []}  # focused by default


def test_prompt_lists_every_file_with_the_total_and_forbids_general_knowledge() -> None:
    files = [
        FileInfo(id="d1", filename="patients.csv", in_chat=True, uploaded_at="2026-09-27"),
        FileInfo(id="d2", filename="Small_Machine_Learning_Notes.pdf", uploaded_at="2026-09-20"),
    ]
    llm = ScriptedLLM(_msg("You have uploaded 2 files."))
    _run(llm, Specialists({}), files=files, question="how many files have I uploaded?")
    system = llm.seen[0][0]["content"]
    assert "2 file(s) uploaded in total:" in system
    assert "Small_Machine_Learning_Notes.pdf (uploaded 2026-09-20)" in system
    assert "general knowledge" in system
    # A file that didn't match the question is still searchable.
    assert "search_documents" in llm.tools_offered[0]


def test_total_file_count_beyond_the_listed_files() -> None:
    files = [FileInfo(id="d1", filename="a.pdf")]
    llm = ScriptedLLM(_msg("You have uploaded 70 files."))
    run_supervisor(
        "how many files?", llm=llm, run_specialist=Specialists({}), databases=[PETRO],
        files=files, total_files=70, context="None.", source="auto", has_dashboard=False,
    )
    assert "70 file(s) uploaded in total, newest 1 listed:" in llm.seen[0][0]["content"]
