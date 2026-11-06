"""T-56: WebSocket / Redis progress event contract tests."""

from app.agent.events import (
    ErrorEvent,
    InsightReadyEvent,
    IntentDetectedEvent,
    ProgressEventType,
    QueryExecutedEvent,
    SqlGeneratedEvent,
    parse_progress_event,
    progress_channel,
)


def test_progress_channel_prefers_conversation() -> None:
    assert (
        progress_channel("tenant-1", conversation_id="conv-9", user_id="user-2")
        == "progress:tenant-1:conv-9"
    )
    assert progress_channel("tenant-1", user_id="user-2") == "progress:tenant-1:user-2"


def test_intent_detected_round_trip() -> None:
    event = IntentDetectedEvent(
        run_id="run-1",
        tenant_id="t1",
        user_id="u1",
        conversation_id="c1",
        connection_id="conn-1",
        intent="sql",
        question="show revenue",
        message="Intent classified as sql",
    )
    raw = event.model_dump(mode="json")
    assert raw["type"] == ProgressEventType.INTENT_DETECTED.value
    parsed = parse_progress_event(raw)
    assert isinstance(parsed, IntentDetectedEvent)
    assert parsed.intent == "sql"


def test_all_event_types_parse() -> None:
    base = {
        "run_id": "run-1",
        "tenant_id": "t1",
        "user_id": "u1",
        "conversation_id": None,
        "connection_id": "conn-1",
    }
    cases = [
        IntentDetectedEvent(**base, intent="rag", question="policy?"),
        SqlGeneratedEvent(**base, sql="SELECT 1"),
        QueryExecutedEvent(**base, sql="SELECT 1", row_count=1, rows_preview=[{"x": 1}]),
        InsightReadyEvent(**base, answer="done", intent="sql"),
        ErrorEvent(**base, error="boom", detail="stack"),
    ]
    for event in cases:
        again = parse_progress_event(event.model_dump(mode="json"))
        assert again.type == event.type


def test_unknown_event_type_rejected() -> None:
    try:
        parse_progress_event(
            {
                "type": "not_a_real_event",
                "run_id": "r",
                "tenant_id": "t",
                "user_id": "u",
                "step": "x",
            }
        )
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Unknown progress event type" in str(exc)
