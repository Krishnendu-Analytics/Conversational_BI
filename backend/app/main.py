from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.cleanup import soft_delete_stale_unverified_users
from app.config import get_settings, is_production_env
from app.redis_client import auth_rate_limit_status
from app.database import SessionLocal, init_db
from app.llm.usage import UsageOwnerMiddleware
from app.routes.auth import router as auth_router
from app.routes.chat_websocket import router as chat_router
from app.routes.connections import router as connection_router
from app.routes.conversations import router as conversations_router
from app.routes.dashboards import router as dashboards_router
from app.routes.datasets import router as datasets_router
from app.routes.documents import router as documents_router
from app.routes.memories import router as memories_router
from app.routes.org import router as org_router
from app.routes.platform import router as platform_router
from app.routes.search import router as search_router
from app.routes.semantic import router as semantic_router
from app.routes.usage import router as usage_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Migrate schema and soft-delete stale unverified accounts on startup."""
    init_db()
    if is_production_env():
        status = auth_rate_limit_status()
        if not status.get("ok"):
            raise RuntimeError(str(status.get("message")))
        from app.services.embeddings import validate_embedding_configuration

        validate_embedding_configuration()
    db = SessionLocal()
    try:
        soft_delete_stale_unverified_users(db)
    finally:
        db.close()
    yield
    from app.tools.customer_db import CustomerDBPool

    CustomerDBPool.close_all()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]

    app = FastAPI(
        title="Conversational BI",
        description="FastAPI base with JWT auth and multi-tenant models.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(UsageOwnerMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        auth_limits = auth_rate_limit_status()
        payload: dict[str, str | bool] = {"status": "ok" if auth_limits.get("ok") else "degraded"}
        payload["auth_rate_limits_ok"] = bool(auth_limits.get("ok"))
        payload["auth_rate_limits_mode"] = str(auth_limits.get("mode"))
        payload["auth_rate_limits_redis"] = bool(auth_limits.get("redis_available"))
        return payload

    app.include_router(auth_router)
    app.include_router(connection_router)
    app.include_router(datasets_router)
    app.include_router(documents_router)
    app.include_router(semantic_router)
    app.include_router(conversations_router)
    app.include_router(dashboards_router)
    app.include_router(search_router)
    app.include_router(memories_router)
    app.include_router(usage_router)
    app.include_router(org_router)
    app.include_router(platform_router)
    app.include_router(chat_router)

    return app


app = create_app()
