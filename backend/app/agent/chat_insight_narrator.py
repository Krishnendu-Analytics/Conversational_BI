"""Grounded LLM narrator for conversational chat insights."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.chat_insight_facts import ChatInsightFacts, grounding_context_from_chat_facts
from app.agent.dashboard_insight_facts import InsightFactStatus
from app.agent.insight_validation import (
    GroundingOutcome,
    render_deterministic_grounded_answer,
    validate_grounded_narrative,
)
from app.agent.prompts import get_prompt

logger = logging.getLogger(__name__)

_MAX_FACTS_JSON = 8192
_NOT_FOR_NARRATOR = {
    "result_rows",  # passed separately, one row per line
    "sql",
    "tenant_id",
    "connection_id",
    "scope_key",
    "source_widget_ids",
}
_MAX_REGENERATIONS = 1


def _strip_markdown_fence(raw: str) -> str:
    content = (raw or "").strip()
    if content.startswith("```"):
        lines = content.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        content = "\n".join(body).strip()
    return content


def narrate_chat_insights(
    facts: ChatInsightFacts,
    llm: Any | None,
    *,
    question: str = "",
) -> tuple[str, str, GroundingOutcome]:
    """Return (answer, narrator mode, grounding outcome)."""
    if facts.is_listing:
        # A plain listing query has no aggregate facts to narrate by
        # design — describing that honestly beats asking the LLM to
        # narrate an intentionally-empty facts payload (which tends to
        # produce a misleading "not enough data" for a query that
        # actually worked fine).
        noun = "row" if facts.row_count == 1 else "rows"
        answer = f"Found {facts.row_count} matching {noun} — see the results table for the full list."
        return answer, "deterministic_listing", GroundingOutcome.VALID

    ctx = grounding_context_from_chat_facts(facts)

    if facts.status == InsightFactStatus.INSUFFICIENT_DATA:
        answer = render_deterministic_grounded_answer(ctx)
        logger.info("chat_grounding_insufficient_data scope=%s", facts.scope_key[:12])
        return answer, "deterministic_fallback", GroundingOutcome.INSUFFICIENT_DATA

    fallback = render_deterministic_grounded_answer(ctx)
    if llm is None:
        logger.info("chat_grounding_fallback reason=llm_unavailable")
        return fallback, "deterministic_fallback", GroundingOutcome.FALLBACK

    # The narrator sees business facts only: with the SQL (or ids) in view it
    # quoted "Source table: data_mart.fact_procurement" and "the provided SQL
    # uses calendar_year = 2025" back to the user. Period and filters are
    # already in time_context / active_filters.
    facts_json = json.dumps(
        facts.model_dump(mode="json", exclude=_NOT_FOR_NARRATOR), default=str
    )[:_MAX_FACTS_JSON]
    shown = len(facts.result_rows)
    rows_header = (
        f"{shown} of {facts.row_count} rows" if facts.row_count > shown else f"all {shown} rows"
    )
    rows_json = "\n".join(json.dumps(row, default=str) for row in facts.result_rows)
    prompt = get_prompt("chat_insight_narrator").render(
        facts_json=facts_json,
        rows_header=rows_header,
        rows_json=rows_json or "(none)",
        question=question or facts.question,
    )
    strict_suffix = (
        "\n\nSTRICT: Use ONLY the supplied facts. Do not invent numbers, rankings, "
        "trends, comparisons, entities, dates, or causation. If facts are insufficient, "
        "say there is not enough data."
    )

    last_invalid: str | None = None
    for attempt in range(_MAX_REGENERATIONS + 1):
        try:
            use_prompt = prompt if attempt == 0 else prompt + strict_suffix
            raw = llm.complete(use_prompt)
        except Exception:  # noqa: BLE001
            logger.info("chat_grounding_fallback reason=llm_error")
            return fallback, "deterministic_fallback", GroundingOutcome.FALLBACK

        if not raw or str(raw).strip().lower().startswith("[llm stub]"):
            logger.info("chat_grounding_fallback reason=llm_stub")
            return fallback, "deterministic_fallback", GroundingOutcome.FALLBACK

        content = _strip_markdown_fence(str(raw))
        try:
            if validate_grounded_narrative(content, ctx):
                outcome = (
                    GroundingOutcome.VALID
                    if attempt == 0
                    else GroundingOutcome.REGENERATED
                )
                logger.info(
                    "chat_grounding_valid" if attempt == 0 else "chat_grounding_regenerated"
                )
                return content, "llm", outcome
        except Exception:  # noqa: BLE001
            logger.info("chat_grounding_fallback reason=validator_error")
            return fallback, "deterministic_fallback", GroundingOutcome.ERROR

        last_invalid = content
        logger.info("chat_grounding_invalid attempt=%s", attempt)

    _ = last_invalid
    logger.info("chat_grounding_fallback reason=validation_failed")
    return fallback, "deterministic_fallback", GroundingOutcome.FALLBACK
