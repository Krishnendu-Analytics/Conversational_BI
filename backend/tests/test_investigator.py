"""Tests for the multi-domain investigation loop (app.agent.investigator).

Collaborators (schema/semantic resolution, the domain catalog, and the
Phase 2 domain tool) are monkeypatched at the module level so these tests
exercise the loop's own control flow — planning, budget-bounded looping,
and grounded synthesis — without needing a real DB, LLM, or Redis.
"""

from __future__ import annotations

from app.agent import investigator
from app.agent.chat_insight_facts import extract_chat_insight_facts
from app.agent.domain_catalog import Domain
from app.agent.domain_tool import DomainEvidence


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[object] = []

    def publish_event(self, event: object) -> bool:
        self.events.append(event)
        return True


def _grounded_evidence(domain: str, *, value: float = 100.0) -> DomainEvidence:
    facts = extract_chat_insight_facts(
        [{"total": value}],
        question=f"total for {domain}",
        sql=f"SELECT SUM(x) AS total FROM {domain.lower()}",
        query_plan=None,
        tenant_id="t1",
        connection_id="c1",
    )
    return DomainEvidence(
        domain=domain,
        connection_id="c1",
        question=f"total for {domain}",
        sql=f"SELECT SUM(x) AS total FROM {domain.lower()}",
        facts=facts,
    )


def _failed_evidence(domain: str, error: str = "boom") -> DomainEvidence:
    return DomainEvidence(
        domain=domain, connection_id="c1", question="q", error=error
    )


def test_relevance_ranks_matching_domain_higher() -> None:
    sales = Domain(name="Sales", table="public.fact_sales", metrics=["Total Sales"])
    production = Domain(name="Production", table="public.fact_production")

    assert investigator._relevance("why did sales decline", sales) > investigator._relevance(
        "why did sales decline", production
    )


def test_relevance_zero_for_empty_question() -> None:
    sales = Domain(name="Sales", table="public.fact_sales")
    assert investigator._relevance("", sales) == 0


def test_should_continue_stops_when_no_domains_left() -> None:
    state = {"domains": [], "round_index": 0, "max_rounds": 2, "evidence": []}
    assert investigator._should_continue(state) == "synthesize"


def test_should_continue_stops_at_round_budget() -> None:
    state = {
        "domains": [{"name": "Inventory", "table": "public.fact_inventory"}],
        "round_index": 2,
        "max_rounds": 2,
        "evidence": [],
    }
    assert investigator._should_continue(state) == "synthesize"


def test_should_continue_stops_when_everything_failed() -> None:
    state = {
        "domains": [{"name": "Inventory", "table": "public.fact_inventory"}],
        "round_index": 0,
        "max_rounds": 2,
        "evidence": [_failed_evidence("Sales").model_dump(mode="json")],
    }
    assert investigator._should_continue(state) == "synthesize"


def test_should_continue_keeps_going_with_domains_and_budget_left() -> None:
    state = {
        "domains": [{"name": "Inventory", "table": "public.fact_inventory"}],
        "round_index": 0,
        "max_rounds": 2,
        "evidence": [_grounded_evidence("Sales").model_dump(mode="json")],
    }
    assert investigator._should_continue(state) == "plan"


def test_planner_resolves_and_ranks_domain_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        investigator, "resolve_schema_for_connection", lambda *_a, **_k: {"db_type": "postgres"}
    )
    monkeypatch.setattr(investigator, "resolve_semantic_for_connection", lambda *_a, **_k: {})
    monkeypatch.setattr(investigator, "filter_confirmed_semantic_catalog", lambda c: c or {})
    monkeypatch.setattr(
        investigator,
        "resolve_domains",
        lambda *_a, **_k: [
            Domain(name="Production", table="public.fact_production"),
            Domain(name="Sales", table="public.fact_sales"),
        ],
    )

    update = investigator.Planner().run(
        {"tenant_id": "t1", "connection_ids": ["c1"], "question": "why did sales decline"}
    )

    names = [d["name"] for d in update["domains"]]
    assert names[0] == "Sales"  # ranked ahead of Production by relevance
    assert all(d["connection_id"] == "c1" for d in update["domains"])


def test_planner_merges_domains_across_multiple_connections(monkeypatch) -> None:
    """Phase 4: domains from a second attached connection are tagged with
    that connection's own id, not the first connection's — this is what
    lets Dispatcher later query each domain against the right database."""
    schemas = {"c1": {"db_type": "postgres"}, "c2": {"db_type": "postgres"}}
    monkeypatch.setattr(
        investigator,
        "resolve_schema_for_connection",
        lambda tenant_id, connection_id, cache: schemas[connection_id],
    )
    monkeypatch.setattr(investigator, "resolve_semantic_for_connection", lambda *_a, **_k: {})
    monkeypatch.setattr(investigator, "filter_confirmed_semantic_catalog", lambda c: c or {})

    domains_by_connection = {
        "c1": [Domain(name="Sales", table="public.fact_sales")],
        "c2": [Domain(name="Weather", table="public.fact_weather")],
    }
    monkeypatch.setattr(
        investigator,
        "resolve_domains",
        lambda schema, semantic, max_domains: (
            domains_by_connection["c1"] if schema is schemas["c1"] else domains_by_connection["c2"]
        ),
    )

    update = investigator.Planner().run(
        {
            "tenant_id": "t1",
            "connection_ids": ["c1", "c2"],
            "question": "why did sales decline",
        }
    )

    by_name = {d["name"]: d["connection_id"] for d in update["domains"]}
    assert by_name == {"Sales": "c1", "Weather": "c2"}
    assert update["round_index"] == 0


def test_planner_missing_schema_yields_empty_catalog(monkeypatch) -> None:
    monkeypatch.setattr(investigator, "resolve_schema_for_connection", lambda *_a, **_k: None)

    update = investigator.Planner().run(
        {"tenant_id": "t1", "connection_ids": ["c1"], "question": "why did sales decline"}
    )

    assert update["domains"] == []


def test_planner_replan_entry_advances_round(monkeypatch) -> None:
    update = investigator.Planner().run(
        {"domains": [{"name": "Sales", "table": "public.fact_sales"}], "round_index": 0}
    )
    assert update["round_index"] == 1
    assert "domains" not in update  # queue untouched on a replan entry


def test_dispatcher_queries_batch_and_trims_queue(monkeypatch) -> None:
    monkeypatch.setattr(investigator, "progress_publisher", _RecordingPublisher())
    monkeypatch.setattr(investigator, "get_llm", lambda: object())

    calls: list[str] = []

    def _fake_run_domain_query(question, **kwargs):
        domain_arg = kwargs["domain"]
        assert isinstance(domain_arg, investigator.Domain)
        calls.append(domain_arg.name)
        return _grounded_evidence(domain_arg.name)

    monkeypatch.setattr(investigator, "run_domain_query", _fake_run_domain_query)

    domains = [
        {"name": "Sales", "table": "public.fact_sales"},
        {"name": "Production", "table": "public.fact_production"},
        {"name": "Inventory", "table": "public.fact_inventory"},
    ]
    state = {
        "domains": domains,
        "batch_size": 2,
        "tenant_id": "t1",
        "question": "why did sales decline",
        "evidence": [],
    }

    update = investigator.Dispatcher().run(state)

    assert calls == ["Sales", "Production"]
    assert [d["name"] for d in update["domains"]] == ["Inventory"]
    assert len(update["evidence"]) == 2


def test_synthesize_report_includes_grounded_highlights() -> None:
    evidence = [
        _grounded_evidence("Sales", value=250.0).model_dump(mode="json"),
        _failed_evidence("Procurement", "No discovered schema").model_dump(mode="json"),
    ]
    state = {
        "question": "why did profit decline",
        "evidence": evidence,
    }

    update = investigator.Synthesizer().run(state)
    answer = update["answer"]

    assert "Sales" in answer
    assert "Procurement" in answer
    assert "No discovered schema" in answer


def test_synthesize_all_failed_says_so_honestly() -> None:
    evidence = [_failed_evidence("Sales", "boom").model_dump(mode="json")]
    update = investigator.Synthesizer().run(
        {"question": "why did profit decline", "evidence": evidence}
    )
    assert "couldn't find grounded findings" in update["answer"]


def test_run_investigation_end_to_end_respects_budget(monkeypatch) -> None:
    monkeypatch.setattr(investigator, "progress_publisher", _RecordingPublisher())
    monkeypatch.setattr(investigator, "get_llm", lambda: object())
    monkeypatch.setattr(
        investigator, "resolve_schema_for_connection", lambda *_a, **_k: {"db_type": "postgres"}
    )
    monkeypatch.setattr(investigator, "resolve_semantic_for_connection", lambda *_a, **_k: {})
    monkeypatch.setattr(investigator, "filter_confirmed_semantic_catalog", lambda c: c or {})

    # More domains than the budget allows, so the round cutoff is the
    # actual reason the loop stops — not just running out of domains.
    catalog = [
        Domain(name=f"Domain{i}", table=f"public.fact_{i}") for i in range(10)
    ]
    monkeypatch.setattr(investigator, "resolve_domains", lambda *_a, **_k: catalog)

    queried: list[str] = []

    def _fake_run_domain_query(question, **kwargs):
        domain_arg = kwargs["domain"]
        queried.append(domain_arg.name)
        return _grounded_evidence(domain_arg.name)

    monkeypatch.setattr(investigator, "run_domain_query", _fake_run_domain_query)

    result = investigator.run_investigation(
        "why did profit decline",
        tenant_id="t1",
        connection_ids=["c1"],
        max_domains=10,
        batch_size=2,
        max_rounds=2,
    )

    # batch_size=2 per round; rounds fire at round_index 0, 1, 2 (3 dispatch
    # passes = 6 domains) before evaluate sees round_index(2) >= max_rounds
    # and stops — 4 of the 10 catalog domains are deliberately never
    # queried, proving the budget (not an empty queue) ended the loop.
    assert len(queried) == 6
    assert len(catalog) > len(queried)
    assert result["answer"]
    assert "Domain0" in result["answer"]
