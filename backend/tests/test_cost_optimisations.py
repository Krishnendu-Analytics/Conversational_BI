"""LLM cost: cheap model tier, cache-friendly prompt order, batched memory."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

from app.agent.prompts import get_prompt
from app.agent.supervisor import _SYSTEM
from app.agent.workflow import _format_schema_for_prompt, _schema_focus_for_prompt
from app.config import get_settings
from app.llm.client import LLMClient, fast_llm
from app.llm.usage import current_usage_owner, usage_owner
from app.services import memory_service

SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {"schema_name": "public", "table_name": "dim_customer", "columns": [{"name": "city", "data_type": "text"}]},
        {"schema_name": "public", "table_name": "fact_sales", "columns": [{"name": "net_sales", "data_type": "numeric"}]},
    ],
}


# ----- 1. model tiers -----


def test_fast_tier_uses_fast_model_only_when_configured(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_fast_model", "")
    base = LLMClient()
    assert fast_llm(base) is base

    monkeypatch.setattr(settings, "llm_fast_model", "tiny-model")
    assert fast_llm(base).model == "tiny-model"
    assert base.model == settings.llm_model  # the main tier is untouched

    fake = object()
    assert fast_llm(fake) is fake  # test doubles pass straight through


# ----- 3. prompt prefix stays identical across questions -----


def _sql_prompt(question: str) -> str:
    return get_prompt("sql_generation").render(
        db_type="postgres",
        schema=_format_schema_for_prompt(SCHEMA, question=question),
        semantic_context="None.",
        conversation_context="None.",
        question=question,
        question_hints=_schema_focus_for_prompt(SCHEMA, question) or "None.",
        dialect_rules="",
    )


def test_sql_prompt_shares_its_prefix_across_questions() -> None:
    a, b = _sql_prompt("net sales by city"), _sql_prompt("how many customers")
    shared = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
    # Rules, schema and semantic layer are all before the first difference.
    assert a.index("Schema rules (critical)") < a.index("public.fact_sales(") < shared
    assert shared >= a.index("Conversation context:")
    assert "Tables most relevant to this question: public.fact_sales" in a[shared:]


def test_supervisor_puts_per_turn_parts_last() -> None:
    assert _SYSTEM.index("Guidelines:") < _SYSTEM.index("{databases}") < _SYSTEM.index("{context}")
    assert _SYSTEM.rstrip().endswith("{context}")


# ----- 4. memory updates are batched -----


def _capture_updates(monkeypatch) -> tuple[list[dict], threading.Event]:
    calls: list[dict] = []
    done = threading.Event()

    def fake(**kwargs):
        calls.append({**kwargs, "owner": current_usage_owner()})
        done.set()

    monkeypatch.setattr(memory_service, "_update_safely", fake)
    monkeypatch.setattr(memory_service, "_sync_mode", False)
    settings = get_settings()
    monkeypatch.setattr(settings, "memory_enabled", True)
    return calls, done


def _turn(conversation_id, n: int, tenant_id, user_id) -> None:
    memory_service.schedule_update(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        question=f"q{n}",
        answer=f"a{n}",
        intent="sql",
    )


def test_turns_in_a_burst_share_one_update(monkeypatch) -> None:
    calls, done = _capture_updates(monkeypatch)
    monkeypatch.setattr(get_settings(), "memory_update_delay_seconds", 0.2)
    conv, tenant_id, user_id = uuid4(), uuid4(), uuid4()

    with usage_owner(tenant_id, user_id):
        _turn(conv, 1, tenant_id, user_id)
        _turn(conv, 2, tenant_id, user_id)
    assert calls == []  # nothing until the chat goes quiet

    assert done.wait(3)
    time.sleep(0.1)
    assert len(calls) == 1
    assert calls[0]["turns"] == [("q1", "a1"), ("q2", "a2")]
    assert calls[0]["owner"].user_id == user_id  # tokens charged to the chatting user


def test_full_buffer_flushes_without_waiting(monkeypatch) -> None:
    calls, done = _capture_updates(monkeypatch)
    monkeypatch.setattr(get_settings(), "memory_update_delay_seconds", 60)
    conv, tenant_id, user_id = uuid4(), uuid4(), uuid4()

    for n in range(memory_service._MAX_BUFFERED_TURNS):
        _turn(conv, n, tenant_id, user_id)

    assert done.wait(3)
    assert len(calls[0]["turns"]) == memory_service._MAX_BUFFERED_TURNS
    assert conv not in memory_service._pending


def test_greetings_are_never_queued(monkeypatch) -> None:
    calls, _ = _capture_updates(monkeypatch)
    conv = uuid4()
    memory_service.schedule_update(
        tenant_id=uuid4(), user_id=uuid4(), conversation_id=conv,
        question="hi", answer="Hello!", intent="greeting",
    )
    assert conv not in memory_service._pending and calls == []
