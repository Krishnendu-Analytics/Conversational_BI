"""Single-domain query tool — Phase 2 of the multi-domain investigator plan.

Wraps the already-hardened single-question pipeline as a plain function a
planner can call multiple times, once per domain, without going through the
LangGraph chat DAG or its event emission/clarification plumbing. Reuses the
exact SQL-generation and execution building blocks ``workflow.py``'s
``SQLGenerator``/``QueryExecutor`` nodes use for the single-shot chat path
(see the public re-export block near the end of ``workflow.py``) — a fix to
SQL safety validation or fallback heuristics there applies here too.

Resolving a domain question to SQL is three tiers, same as the chat path:
1. Structured query plan -> deterministic compile (``parse_query_plan`` ->
   ``compile_query_plan``) — precise when it resolves, but only covers
   questions that map cleanly onto confirmed metrics/dimensions.
2. Schema-grounded LLM SQL (``generate_schema_grounded_sql``) when tier 1
   didn't resolve or didn't compile — the LLM writes SQL directly against
   the domain's real schema. This is what gives a domain call the same
   quality of answer the main chat path already gets for an equivalent
   question (e.g. "which production unit is underperforming" ->
   Planned/Actual/Shortfall/Yield, not just a row count); without it, an
   unresolved domain question collapsed straight to tier 3's bare
   COUNT(*)/SUM(one column) far more often than it needed to — an observed
   failure mode, not a hypothetical one.
3. ``ensure_valid_sql`` — validate/repair whatever tier 1 or 2 produced, or
   the deterministic schema-safe query as the absolute last resort.

An autonomous investigation step can't pause mid-loop to ask the user a
clarifying question the way a chat turn can, so anything that would produce
a ``QueryClarification`` in chat instead becomes an error-carrying
``DomainEvidence`` here — the investigator (Phase 3) treats that as "this
domain didn't pan out" and moves on, rather than blocking. A tier-1
rejection is not itself reported as the domain's error, though — tier 2
gets a real shot at the question first (see ``run_domain_query``).

Every query this module runs is scoped to exactly the given domain's own
table (see ``_scope_schema_to_table``) and its own confirmed metrics/
dimensions (see ``_scope_semantic_catalog``) — not the full discovered
schema/catalog. Without that scoping, an unresolved query plan falls back
to the query compiler's own generic "first table in the schema" default,
which is the *same* table for every domain call — every domain silently
collapsing onto one table's row count was an observed failure mode, not a
hypothetical one, so this scoping is load-bearing, not a nicety.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.agent.chat_insight_facts import ChatInsightFacts, extract_chat_insight_facts
from app.agent.domain_catalog import Domain
from app.agent.query_plan import QueryPlan, parse_query_plan
from app.agent.query_plan_validation import normalize_query_plan, validate_query_plan
from app.agent.sql_compiler import compile_query_plan
from app.agent.workflow import (
    clean_generated_sql,
    ensure_valid_sql,
    filter_confirmed_semantic_catalog,
    generate_schema_grounded_sql,
    postprocess_generated_sql,
    recover_sql_after_execution_error,
    resolve_schema_for_connection,
    resolve_semantic_for_connection,
)
from app.llm.client import LLMClient, get_llm
from app.query_execution import (
    QueryCategory,
    is_non_retryable_execution_error,
    user_facing_execution_error,
)
from app.redis_client import SchemaCache, SemanticCache
from app.tools.customer_db import run_readonly_query
from app.tools.sql_safety import SQLSafetyPipeline


class DomainEvidence(BaseModel):
    """What one domain query found, plus how confident it is.

    ``fallback_label`` mirrors the plain-English confidence signal already
    built for chat (``_build_fallback_choice`` in workflow.py): set only
    when the heuristic fallback ran instead of SQL the LLM grounded
    directly in the question, so a synthesis step can down-weight or flag
    it instead of treating it as equally solid evidence.
    """

    domain: str
    connection_id: str
    question: str
    sql: str | None = None
    facts: ChatInsightFacts | None = None
    fallback_label: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.facts is not None


def _scope_schema_to_table(
    schema: dict[str, Any], qualified_table: str
) -> dict[str, Any] | None:
    """A schema containing only the one table this domain covers.

    Every downstream step (planning, compilation, the deterministic
    fallback, safety validation) only ever sees this table, so a domain
    query can never silently drift onto a different table.
    """
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("table_name")
        if not name:
            continue
        schema_name = table.get("schema_name") or ""
        candidate = f"{schema_name}.{name}" if schema_name else str(name)
        if candidate == qualified_table or str(name) == qualified_table:
            return {"db_type": schema.get("db_type"), "tables": [table]}
    return None


def _scope_semantic_catalog(
    semantic_context: dict[str, Any] | None, domain: Domain
) -> dict[str, Any]:
    """Only this domain's own confirmed metrics/dimensions.

    Prevents ``validate_query_plan`` from resolving — or offering as a
    clarification choice — a field that actually belongs to a different
    domain's table.
    """
    metric_names = set(domain.metrics)
    dim_names = set(domain.dimensions)
    metrics = [
        m
        for m in ((semantic_context or {}).get("metrics") or [])
        if isinstance(m, dict) and m.get("name") in metric_names
    ]
    dimensions = [
        d
        for d in ((semantic_context or {}).get("dimensions") or [])
        if isinstance(d, dict) and d.get("name") in dim_names
    ]
    return {"metrics": metrics, "dimensions": dimensions, "relationships": []}


def run_domain_query(
    question: str,
    *,
    tenant_id: str,
    connection_id: str,
    domain: Domain,
    llm: LLMClient | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    schema_cache: SchemaCache | None = None,
    semantic_cache: SemanticCache | None = None,
) -> DomainEvidence:
    """Answer one domain-scoped question the same way a single chat turn
    would, minus the chat-specific clarification/event-emission plumbing.

    ``schema_cache``/``semantic_cache`` are injectable (default to real
    Redis-backed caches), mirroring the same DI convention already used by
    ``SQLGenerator``/``QueryExecutor`` in workflow.py, for testability.

    Note: unlike the chat path, this does not resolve file-backed (CSV/
    Excel) datasets via ``_apply_file_dataset_to_analytical_context`` —
    Phase 2 targets real DB connections, which is the investigator's first
    use case. File-backed multi-domain investigation is a follow-up.
    """
    llm = llm or get_llm()

    full_schema = resolve_schema_for_connection(
        tenant_id, connection_id, schema_cache or SchemaCache()
    )
    if full_schema is None:
        return DomainEvidence(
            domain=domain.name,
            connection_id=connection_id,
            question=question,
            error="No discovered schema for this connection.",
        )

    schema = _scope_schema_to_table(full_schema, domain.table)
    if schema is None:
        return DomainEvidence(
            domain=domain.name,
            connection_id=connection_id,
            question=question,
            error=f"Table {domain.table} is no longer present in the discovered schema.",
        )

    full_semantic_context = resolve_semantic_for_connection(
        tenant_id, connection_id, semantic_cache or SemanticCache()
    )
    full_semantic_context = filter_confirmed_semantic_catalog(full_semantic_context)
    semantic_context = _scope_semantic_catalog(full_semantic_context, domain)

    db_type = str(schema.get("db_type") or "")
    if not db_type:
        return DomainEvidence(
            domain=domain.name,
            connection_id=connection_id,
            question=question,
            error="Database type is missing from the discovered schema.",
        )

    # A domain hint, not a rewrite: the original `question` is what's shown
    # to the user (DomainEvidence.question, extract_chat_insight_facts
    # below); this scoped variant only steers plan parsing, repair, and the
    # deterministic fallback's column-relevance scoring toward this domain.
    scoped_question = f"For the {domain.name} domain: {question}"

    # Tier 1: structured query plan -> deterministic compile. Only kept as
    # `plan` (passed to extract_chat_insight_facts below) if it actually
    # produced SQL — a plan that failed to parse/validate/compile is
    # discarded, not reported as an error, so tier 2 gets a clean shot at
    # the question instead of this domain giving up early.
    plan: QueryPlan | None = None
    sql: str | None = None
    structured_plan = parse_query_plan(llm, scoped_question, semantic_context, None)
    if structured_plan is not None:
        structured_plan = normalize_query_plan(
            structured_plan, question=scoped_question, semantic_catalog=semantic_context
        )
        validation = validate_query_plan(
            structured_plan, question=scoped_question, semantic_catalog=semantic_context
        )
        if validation.ok:
            try:
                sql = compile_query_plan(structured_plan, semantic_context, schema, db_type)
            except Exception:  # noqa: BLE001
                sql = None
            else:
                plan = structured_plan

    sql = clean_generated_sql(sql) if sql else None
    if sql:
        sql = postprocess_generated_sql(sql, db_type)

    # Tier 2: ask the LLM to write SQL directly against this domain's own
    # (single-table) schema when the structured plan didn't resolve or
    # didn't compile. This is the tier that gives the main chat path its
    # richer answers ("which production unit is underperforming" ->
    # Planned/Actual/Shortfall/Yield, not just a row count) — without it, an
    # unresolved domain question collapsed straight to tier 3's bare
    # COUNT(*)/SUM(one column), which is a much thinner answer.
    if not sql:
        generated_sql, no_matching_data = generate_schema_grounded_sql(
            llm=llm,
            db_type=db_type,
            schema=schema,
            semantic_context=semantic_context,
            question=scoped_question,
        )
        if no_matching_data:
            return DomainEvidence(
                domain=domain.name,
                connection_id=connection_id,
                question=question,
                error="This domain's data doesn't appear to be relevant to the question.",
            )
        sql = generated_sql

    # Tier 3: validate/repair whatever tier 1 or 2 produced, or fall to the
    # deterministic schema-safe query as the absolute last resort.
    sql, gen_error, fallback_label = ensure_valid_sql(
        sql,
        schema=schema,
        db_type=db_type,
        semantic_context=semantic_context,
        question=scoped_question,
        llm=llm,
    )
    if gen_error or not sql:
        return DomainEvidence(
            domain=domain.name,
            connection_id=connection_id,
            question=question,
            error=gen_error or "Could not generate valid SQL for this domain.",
        )

    try:
        safe_sql = SQLSafetyPipeline(schema=schema, db_type=db_type).validate(sql)
        rows = run_readonly_query(
            connection_id,
            safe_sql,
            tenant_id=UUID(str(tenant_id)),
            category=QueryCategory.ANALYTICS,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        final_sql = safe_sql
    except Exception as exc:  # noqa: BLE001
        if is_non_retryable_execution_error(exc):
            return DomainEvidence(
                domain=domain.name,
                connection_id=connection_id,
                question=question,
                sql=sql,
                error=user_facing_execution_error(exc),
            )

        recovered_sql, recovered_label = recover_sql_after_execution_error(
            sql=sql,
            schema=schema,
            db_type=db_type,
            semantic_context=semantic_context,
            question=scoped_question,
            llm=llm,
            execution_error=f"{type(exc).__name__}: {exc}",
        )
        if not recovered_sql:
            return DomainEvidence(
                domain=domain.name,
                connection_id=connection_id,
                question=question,
                sql=sql,
                error=f"Query execution failed for this domain: {exc}",
            )
        try:
            rows = run_readonly_query(
                connection_id,
                recovered_sql,
                tenant_id=UUID(str(tenant_id)),
                category=QueryCategory.ANALYTICS,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            final_sql = recovered_sql
            fallback_label = recovered_label or fallback_label
        except Exception as retry_exc:  # noqa: BLE001
            return DomainEvidence(
                domain=domain.name,
                connection_id=connection_id,
                question=question,
                sql=recovered_sql,
                error=f"Query execution failed for this domain: {retry_exc}",
            )

    facts = extract_chat_insight_facts(
        rows,
        question=question,
        sql=final_sql,
        query_plan=plan,
        tenant_id=tenant_id,
        connection_id=connection_id,
        semantic_catalog=full_semantic_context,
    )

    return DomainEvidence(
        domain=domain.name,
        connection_id=connection_id,
        question=question,
        sql=final_sql,
        facts=facts,
        fallback_label=fallback_label,
    )
