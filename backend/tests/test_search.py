from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Conversation, Dashboard, DashboardWidget, Message, User
from app.services.search_service import snippet
from tests.test_semantic_api import _signup_and_headers
from tests.test_semantic_model_import import _db
from tests.test_tenant_isolation import _create_connection


def _seed(email: str, connection_id) -> dict:
    db, gen = _db()
    try:
        user = db.scalar(select(User).where(User.email == email))
        ids = {}

        def chat(title: str, messages: list[str], **extra) -> Conversation:
            row = Conversation(tenant_id=user.tenant_id, user_id=user.id, title=title, **extra)
            db.add(row)
            db.flush()
            for text in messages:
                db.add(Message(conversation_id=row.id, tenant_id=user.tenant_id, role="user", content=text))
            return row

        ids["title"] = chat("Quarterly revenue review", ["hello"]).id
        ids["message"] = chat(
            "Untitled",
            ["Which plants had the most downtime hours in March and why did it rise?"],
        ).id
        ids["archived"] = chat(
            "Old downtime chat", [], archived_at=datetime.now(timezone.utc)
        ).id
        chat("Deleted downtime chat", [], deleted_at=datetime.now(timezone.utc))
        ids["percent"] = chat("Margin 100% check", []).id

        dash = Dashboard(
            tenant_id=user.tenant_id, user_id=user.id, connection_id=connection_id, name="Plant overview"
        )
        db.add(dash)
        db.flush()
        db.add(DashboardWidget(dashboard_id=dash.id, tenant_id=user.tenant_id, type="kpi", title="Downtime hours"))
        ids["dashboard"] = dash.id
        db.commit()
        return {k: str(v) for k, v in ids.items()}
    finally:
        next(gen, None)


def test_snippet_centres_on_match() -> None:
    text = "a " * 200 + "downtime spike " + "b " * 200
    piece = snippet(text, "DOWNTIME")
    assert "downtime spike" in piece
    assert piece.startswith("…") and piece.endswith("…")
    assert snippet("short text", "zzz") == "short text"


def test_search_chats_and_dashboards(client: tuple[TestClient, dict[str, str]], monkeypatch) -> None:
    test_client, sent = client
    email = "search@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)
    ids = _seed(email, conn_id)

    def search(q: str) -> dict:
        res = test_client.get("/api/v1/search", params={"q": q}, headers=headers)
        assert res.status_code == 200, res.text
        return res.json()

    body = search("  DOWNtime ")
    assert body["query"] == "DOWNtime"
    chats = {c["id"]: c for c in body["chats"]}
    assert set(chats) == {ids["message"], ids["archived"]}  # deleted chat excluded
    assert "downtime hours in March" in chats[ids["message"]]["snippet"]
    assert chats[ids["archived"]]["archived"] is True
    assert chats[ids["archived"]]["snippet"] is None  # matched on title
    assert [d["id"] for d in body["dashboards"]] == [ids["dashboard"]]
    assert body["dashboards"][0]["snippet"] == "Downtime hours"

    assert [c["id"] for c in search("revenue")["chats"]] == [ids["title"]]
    # LIKE wildcards are literal characters, not patterns.
    assert [c["id"] for c in search("100%")["chats"]] == [ids["percent"]]
    assert search("%")["chats"] == []
    assert search("a") == {"query": "a", "chats": [], "dashboards": []}  # too short


def test_search_is_scoped_to_the_user(client: tuple[TestClient, dict[str, str]], monkeypatch) -> None:
    test_client, sent = client
    owner = _signup_and_headers(test_client, sent, "owner@acme.com")
    conn_id = _create_connection(test_client, owner, monkeypatch)
    _seed("owner@acme.com", conn_id)
    other = _signup_and_headers(test_client, sent, "other@globex.com")
    res = test_client.get("/api/v1/search", params={"q": "downtime"}, headers=other)
    assert res.json() == {"query": "downtime", "chats": [], "dashboards": []}
