"""
Database connection for the platform (users, tenants, etc.).

get_db() gives one Session per HTTP request, then closes it.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

is_sqlite = settings.database_url.startswith("sqlite")

# SQLite needs this flag when used with FastAPI
if is_sqlite:
    connect_args = {"check_same_thread": False}
    engine = create_engine(settings.database_url, connect_args=connect_args)
else:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Parent class for all SQLAlchemy table models."""


def get_db() -> Generator[Session, None, None]:
    """Open a DB session, yield it to the route, then close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations() -> None:
    """Apply Alembic migrations (Postgres)."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(alembic_cfg, "head")


def init_db() -> None:
    """Start DB: migrations on Postgres, create_all on sqlite/tests."""
    from app import models  # noqa: F401

    if is_sqlite:
        Base.metadata.create_all(bind=engine)
    else:
        run_migrations()
