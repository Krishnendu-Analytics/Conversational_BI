"""Multi-domain root-cause investigation loop — Phase 3.

A real plan -> act -> observe -> replan loop, as a separate LangGraph graph
from the single-shot chat DAG in workflow.py — entered only for the
``"investigate"`` intent (root-cause / "why" questions escalate here;
ordinary single-fact questions stay on the fast existing path and never
pay this loop's cost).

Stopping policy is deterministic, not an LLM judgment call, matching this
codebase's general preference for deterministic guardrails over LLM
judgment wherever one is workable (see e.g. ``_ensure_valid_sql`` in
workflow.py). v1 sweeps every catalog domain, highest-relevance-to-the-
question first, bounded by ``max_domains``/``max_rounds`` — it does not yet
try to judge "is this enough evidence for this specific question" and stop
early on a hit; that adaptive judgment is a natural v2, not attempted here,
since getting it wrong either way (stopping too early or investigating
pointlessly long) is worse than a bounded full sweep.

Synthesis is deterministic too: it reuses each domain's
``DashboardInsightFacts.ranked_highlights`` — the same pre-computed,
grounded one-liners that already power the "AI Insights" widget on
dashboards — rather than asking an LLM to write prose across N domains'
worth of facts. That keeps this loop's output impossible to hallucinate
even though it spans multiple queries; an LLM-narrated executive summary
on top of these grounded bullets is a natural v2, not attempted here.
"""

from __future__ import annotations

import operator
import re
from typing import Annotated, Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.agent.domain_catalog import Domain, resolve_domains
from app.agent.domain_tool import DomainEvidence, run_domain_query
from app.agent.events import InvestigationCompleteEvent, InvestigationStepEvent
from app.agent.workflow import (
    filter_confirmed_semantic_catalog,
    resolve_schema_for_connection,
    resolve_semantic_for_connection,
)
from app.llm.client import get_llm
from app.redis_client import SchemaCache, SemanticCache, progress_publisher

_DEFAULT_MAX_DOMAINS = 6
_DEFAULT_BATCH_SIZE = 2
_DEFAULT_MAX_ROUNDS = 2

_QUESTION_TOKEN_RE = re.compile(r"[a-z0-9]+")


class InvestigationState(TypedDict, total=False):
    tenant_id: str
    user_id: str | None
    connection_ids: list[str]
    conversation_id: str | None
    run_id: str
    question: str
    domains: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    round_index: int
    max_domains: int
    batch_size: int
    max_rounds: int
    answer: str | None
    steps: Annotated[list[str], operator.add]


def _emit(state: InvestigationState, event: Any) -> None:
    """Best-effort progress publish; never fail the loop. Mirrors
    workflow.py's ``_emit`` but adapted to ``InvestigationState``."""
    try:
        progress_publisher.publish_event(event)
    except Exception:  # noqa: BLE001
        pass


def _base_kwargs(state: InvestigationState) -> dict[str, Any]:
    connection_ids = state.get("connection_ids") or []
    return {
        "run_id": state.get("run_id") or "",
        "tenant_id": state.get("tenant_id") or "",
        "user_id": state.get("user_id") or "",
        "conversation_id": state.get("conversation_id"),
        # Events carry one connection_id as coarse metadata (which
        # connection an investigation touched at all matters more here than
        # per-event precision) — the primary connection when there are
        # several; per-domain attribution lives in DomainEvidence.
        "connection_id": connection_ids[0] if connection_ids else None,
    }


def _relevance(question: str, domain: Domain) -> int:
    """Keyword overlap between the question and this domain's name/fields —
    same token-overlap idea as workflow.py's ``_table_relevance_score``,
    used here to rank which domains get investigated first within budget."""
    tokens = {t for t in _QUESTION_TOKEN_RE.findall(question.lower()) if len(t) > 2}
    if not tokens:
        return 0
    name_tokens = set(_QUESTION_TOKEN_RE.findall(domain.name.lower()))
    field_tokens: set[str] = set()
    for field in (*domain.metrics, *domain.dimensions):
        field_tokens.update(_QUESTION_TOKEN_RE.findall(field.lower()))
    return len(tokens & name_tokens) * 3 + len(tokens & field_tokens)


class Planner:
    """Resolve the domain catalog once (first entry) across every attached
    connection and rank it by relevance to the question; on a replan entry,
    just advance the round counter — the domain queue itself was already
    trimmed by Dispatcher."""

    def run(self, state: InvestigationState) -> dict[str, Any]:
        if state.get("domains") is None:
            tenant_id = state.get("tenant_id") or ""
            connection_ids = state.get("connection_ids") or []
            max_domains = state.get("max_domains", _DEFAULT_MAX_DOMAINS)
            question = state.get("question") or ""

            all_domains: list[Domain] = []
            for connection_id in connection_ids:
                schema = resolve_schema_for_connection(
                    tenant_id, connection_id, SchemaCache()
                )
                if schema is None:
                    continue

                semantic_context = filter_confirmed_semantic_catalog(
                    resolve_semantic_for_connection(
                        tenant_id, connection_id, SemanticCache()
                    )
                )
                # Cap per connection first so one connection with a huge
                # catalog can't crowd out every other attached connection
                # before the global rank/trim below even runs.
                for domain in resolve_domains(
                    schema, semantic_context, max_domains=max_domains
                ):
                    all_domains.append(
                        domain.model_copy(update={"connection_id": connection_id})
                    )

            ranked = sorted(
                all_domains, key=lambda d: _relevance(question, d), reverse=True
            )[:max_domains]
            return {
                "domains": [d.model_dump() for d in ranked],
                "round_index": 0,
                "steps": ["plan_investigation"],
            }

        return {
            "round_index": state.get("round_index", 0) + 1,
            "steps": ["plan_investigation"],
        }


class Dispatcher:
    """Query this round's batch of domains via the Phase 2 domain tool."""

    def run(self, state: InvestigationState) -> dict[str, Any]:
        remaining = list(state.get("domains") or [])
        batch_size = state.get("batch_size", _DEFAULT_BATCH_SIZE)
        batch, rest = remaining[:batch_size], remaining[batch_size:]

        tenant_id = state.get("tenant_id") or ""
        question = state.get("question") or ""
        llm = get_llm()
        evidence = list(state.get("evidence") or [])

        for raw_domain in batch:
            domain = Domain.model_validate(raw_domain)
            _emit(
                state,
                InvestigationStepEvent(
                    **_base_kwargs(state),
                    domain=domain.name,
                    message=f"Checking {domain.name}...",
                ),
            )

            result = run_domain_query(
                question,
                tenant_id=tenant_id,
                # Each domain carries its own connection_id (tagged by
                # Planner) — not a single shared one — so a domain from a
                # second attached connection is queried against that
                # connection, never the primary one by accident.
                connection_id=domain.connection_id,
                domain=domain,
                llm=llm,
                user_id=state.get("user_id"),
                conversation_id=state.get("conversation_id"),
            )
            evidence.append(result.model_dump(mode="json"))

            _emit(
                state,
                InvestigationStepEvent(
                    **_base_kwargs(state),
                    domain=domain.name,
                    message=(
                        f"{domain.name}: found evidence"
                        if result.ok
                        else f"{domain.name}: {result.error}"
                    ),
                ),
            )

        return {"domains": rest, "evidence": evidence, "steps": ["dispatch_domain"]}


class Evaluator:
    """Marks the evaluate step in the audit trail; the actual continue/stop
    decision is the conditional edge below (``_should_continue``) so it's
    visible in the graph shape, not buried in a node body."""

    def run(self, state: InvestigationState) -> dict[str, Any]:
        return {"steps": ["evaluate_evidence"]}


def _should_continue(state: InvestigationState) -> str:
    domains_left = bool(state.get("domains"))
    round_index = state.get("round_index", 0)
    max_rounds = state.get("max_rounds", _DEFAULT_MAX_ROUNDS)
    evidence = state.get("evidence") or []
    all_failed = bool(evidence) and all(not e.get("facts") for e in evidence)

    if not domains_left:
        return "synthesize"
    if round_index >= max_rounds:
        return "synthesize"
    if all_failed:
        # Every domain tried so far has errored — the connection/schema
        # looks unusable for this question. More rounds won't help.
        return "synthesize"
    return "plan"


class Synthesizer:
    def run(self, state: InvestigationState) -> dict[str, Any]:
        question = state.get("question") or ""
        evidence_raw = state.get("evidence") or []

        lines = [f"## Investigation: {question}", ""]
        found_any = False
        skipped: list[str] = []
        domains_checked: set[str] = set()

        for raw in evidence_raw:
            evidence = DomainEvidence.model_validate(raw)
            domains_checked.add(evidence.domain)

            if not evidence.ok or evidence.facts is None:
                skipped.append(f"{evidence.domain} ({evidence.error or 'no data'})")
                continue

            highlights = evidence.facts.ranked_highlights[:3]
            if not highlights:
                skipped.append(f"{evidence.domain} (no notable findings)")
                continue

            found_any = True
            heading = f"**{evidence.domain}**"
            if evidence.fallback_label:
                heading += f" — _best interpretation: {evidence.fallback_label}_"
            lines.append(heading)
            for highlight in highlights:
                lines.append(f"- {highlight}")
            lines.append("")

        if not found_any:
            lines.append(
                "I checked the available domains but couldn't find grounded "
                "findings for this question."
            )
        if skipped:
            lines.append(f"_Not investigated further: {', '.join(skipped)}._")

        answer = "\n".join(lines).strip()

        _emit(
            state,
            InvestigationCompleteEvent(
                **_base_kwargs(state),
                domains_checked=len(domains_checked),
                message="Investigation complete",
            ),
        )

        return {"answer": answer, "steps": ["synthesize_root_cause"]}


def _build_graph():
    planner = Planner()
    dispatcher = Dispatcher()
    evaluator = Evaluator()
    synthesizer = Synthesizer()

    graph = StateGraph(InvestigationState)
    graph.add_node("plan", planner.run)
    graph.add_node("dispatch", dispatcher.run)
    graph.add_node("evaluate", evaluator.run)
    graph.add_node("synthesize", synthesizer.run)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "dispatch")
    graph.add_edge("dispatch", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        _should_continue,
        {"plan": "plan", "synthesize": "synthesize"},
    )
    graph.add_edge("synthesize", END)

    return graph.compile()


_GRAPH: Any = None


def _get_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


def run_investigation(
    question: str,
    *,
    tenant_id: str,
    connection_ids: list[str],
    run_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    max_domains: int = _DEFAULT_MAX_DOMAINS,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
) -> dict[str, Any]:
    """Run the multi-domain investigation loop to completion, across every
    connection in ``connection_ids`` (just the primary connection for a
    conversation with nothing extra attached — see
    app.services.conversation_connections_service).

    Returns the final ``InvestigationState`` dict — ``answer`` is the
    synthesized report, ``evidence`` the raw per-domain results.
    """
    graph = _get_graph()
    initial: InvestigationState = {
        "tenant_id": tenant_id,
        "connection_ids": list(connection_ids),
        "conversation_id": conversation_id,
        "user_id": user_id,
        "run_id": run_id or str(uuid4()),
        "question": question,
        "max_domains": max_domains,
        "batch_size": batch_size,
        "max_rounds": max_rounds,
        "evidence": [],
    }
    return graph.invoke(initial)
