"""JSON-safe serialization for WebSocket / API payloads."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from app.agent.types import AgentResult
from app.routes.chat_websocket import _result_to_done
from app.serialize import json_safe


def test_json_safe_datetime_decimal_uuid() -> None:
    uid = uuid4()
    raw = {
        "ordered_at": datetime(2024, 1, 15, 12, 30, 0),
        "day": date(2024, 1, 15),
        "amount": Decimal("12.50"),
        "qty": Decimal("3"),
        "id": uid,
    }
    out = json_safe(raw)
    assert out["ordered_at"] == "2024-01-15T12:30:00"
    assert out["day"] == "2024-01-15"
    assert out["amount"] == 12.5
    assert out["qty"] == 3
    assert out["id"] == str(uid)


def test_result_to_done_serializes_datetime_rows() -> None:
    result = AgentResult(
        answer="ok",
        sql="SELECT 1",
        rows=[{"ts": datetime(2024, 6, 1, 9, 0, 0), "n": Decimal("1")}],
        steps=["generate_insight"],
        intent="sql",
        run_id="r1",
    )
    done = _result_to_done(result)
    assert done.get("dashboard") is None
    # Must not raise when dumped (same path as websocket.send_json).
    import json

    body = json.dumps(done)
    assert "2024-06-01T09:00:00" in body
