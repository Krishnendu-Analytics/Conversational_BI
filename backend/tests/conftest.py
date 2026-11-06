import os

# Ensure tests never accidentally hit Postgres from a local .env
os.environ["DATABASE_URL"] = "sqlite://"

# Stable Fernet key shared by all tests
os.environ["FERNET_SECRET_KEY"] = (
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
)

# Stable JWT secret for tests
os.environ["JWT_SECRET_KEY"] = "test-secret"

# Avoid real Redis during unit tests (cache degrades / is mocked per test)
os.environ["REDIS_URL"] = ""
# Deterministic embeddings for RAG tests (no external API)
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["EMBEDDING_DIMENSION"] = "384"
# Chat memory makes an extra LLM call per turn; tests opt in explicitly.
os.environ["MEMORY_ENABLED"] = "false"
# Auto source routing (database vs uploaded files) likewise opts in per test.
os.environ["SOURCE_ROUTER_ENABLED"] = "false"
# Existing tests exercise the deterministic pipeline; supervisor tests opt in.
os.environ["AGENT_MODE"] = "pipeline"
# Most tests create accounts through public signup; invite-only has its own tests.
os.environ["ALLOW_PUBLIC_SIGNUP"] = "true"
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

get_settings.cache_clear()

from app.database import Base, get_db
from app.main import app as fastapi_app
import app.models  # noqa: F401 — register ORM models for create_all

TEST_FERNET_KEY = os.environ["FERNET_SECRET_KEY"]


@pytest.fixture(autouse=True)
def _reset_fernet_encryptor() -> Generator[None, None, None]:
    """Keep module-level encryptors aligned with the shared test Fernet key."""
    os.environ["FERNET_SECRET_KEY"] = TEST_FERNET_KEY
    get_settings.cache_clear()
    from app.encryption import CredentialEncryption
    import app.routes.connections as connections_routes

    connections_routes.encryptor = CredentialEncryption()
    yield


@pytest.fixture(autouse=True)
def _fresh_access_scopes() -> Generator[None, None, None]:
    """Tests flip roles straight in the DB; never reuse a cached access scope."""
    from app.access import invalidate_access_scopes

    invalidate_access_scopes()
    yield
    invalidate_access_scopes()


@pytest.fixture(scope="session", autouse=True)
def _discovery_always_sync() -> Generator[None, None, None]:
    """Keep schema discovery synchronous for deterministic tests."""
    from app.services.discovery_jobs import set_sync_mode

    set_sync_mode(True)
    yield
    set_sync_mode(False)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, dict[str, str]], None, None]:
    """Fresh in-memory DB per test; email sending is mocked."""
    monkeypatch.setattr("app.main.init_db", lambda: None)
    monkeypatch.setattr("app.main.soft_delete_stale_unverified_users", lambda db: 0)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db() -> Generator[Session, None, None]:
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    sent: dict[str, str] = {}

    def fake_send_email(to_email: str, otp: str) -> None:
        sent["to_email"] = to_email
        sent["otp"] = otp

    monkeypatch.setattr("app.routes.auth.send_email", fake_send_email)
    # Never email real invites; returning False makes the API hand back the link.
    monkeypatch.setattr(
        "app.services.org_service.send_invite_email", lambda *a, **k: False
    )
    fastapi_app.dependency_overrides[get_db] = override_get_db

    from app.services.discovery_jobs import set_sync_mode

    set_sync_mode(True)

    with TestClient(fastapi_app) as test_client:
        yield test_client, sent

    fastapi_app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)

