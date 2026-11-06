
from functools import lru_cache
import os
from dotenv import load_dotenv

from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    """Load settings from environment variables (or .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    database_url: str = os.getenv("DATABASE_URL")
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY")
    fernet_secret_key: str = os.getenv("FERNET_SECRET_KEY")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    sendgrid_api_key: str = os.getenv("SENDGRID_API_KEY")
    from_email: str = os.getenv("FROM_EMAIL")
    otp_expire_minutes: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_resend_max_per_hour: int = 5
    otp_verify_max_attempts: int = 5
    otp_lockout_minutes: int = 15
    login_max_failures: int = 10
    login_lockout_minutes: int = 15
    unverified_soft_delete_days: int = 1

    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Comma-separated browser origins allowed to call the API.
    cors_origins: str = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173"
    )

    # Redis (schema cache, conversation memory, pub/sub — optional in local tests)
    redis_url: str = os.getenv(
        "REDIS_URL",
        "redis://localhost:6379/0"
    )
    schema_cache_ttl_seconds: int = int(
        os.getenv("SCHEMA_CACHE_TTL_SECONDS", "3600")
    )

    # development | test | production
    app_env: str = os.getenv("APP_ENV", "development")

    # LLM (cloud API for now)
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv(
        "LLM_BASE_URL",
        "https://api.openai.com/v1"
    )
    llm_model: str = os.getenv("LLM_MODEL", "gpt-5.4-mini")

    # LLM and embedding pricing (USD per million tokens)
    # Zero defaults are used until actual provider pricing is configured.
    llm_input_price_per_mtok: float = float(
        os.getenv("LLM_INPUT_PRICE_PER_MTOK", "0")
    )
    llm_output_price_per_mtok: float = float(
        os.getenv("LLM_OUTPUT_PRICE_PER_MTOK", "0")
    )
    embedding_price_per_mtok: float = float(
        os.getenv("EMBEDDING_PRICE_PER_MTOK", "0")
    )

    # Customer query resource limits
    query_timeout_seconds: int = int(
        os.getenv("QUERY_TIMEOUT_SECONDS", "15")
    )
    query_max_rows: int = int(
        os.getenv("QUERY_MAX_ROWS", "10000")
    )
    query_max_payload_bytes: int = int(
        os.getenv("QUERY_MAX_PAYLOAD_BYTES", "5000000")
    )
    query_global_concurrency: int = int(
        os.getenv("QUERY_GLOBAL_CONCURRENCY", "32")
    )
    query_tenant_concurrency: int = int(
        os.getenv("QUERY_TENANT_CONCURRENCY", "8")
    )
    query_user_concurrency: int = int(
        os.getenv("QUERY_USER_CONCURRENCY", "4")
    )

    dashboard_query_timeout_seconds: int = int(
        os.getenv("DASHBOARD_QUERY_TIMEOUT_SECONDS", "15")
    )
    dashboard_query_max_rows: int = int(
        os.getenv("DASHBOARD_QUERY_MAX_ROWS", "10000")
    )
    dashboard_query_max_payload_bytes: int = int(
        os.getenv("DASHBOARD_QUERY_MAX_PAYLOAD_BYTES", "5000000")
    )
    dashboard_max_widgets_per_refresh: int = int(
        os.getenv("DASHBOARD_MAX_WIDGETS_PER_REFRESH", "80")
    )
    dashboard_widget_max_concurrency: int = int(
        os.getenv("DASHBOARD_WIDGET_MAX_CONCURRENCY", "4")
    )

    # Widget query results are reused from Redis.
    # Refresh always re-runs the queries. 0 disables caching.
    dashboard_result_cache_seconds: int = int(
        os.getenv("DASHBOARD_RESULT_CACHE_SECONDS", "300")
    )

    profiling_timeout_seconds: int = int(
        os.getenv("PROFILING_TIMEOUT_SECONDS", "10")
    )
    profiling_max_rows: int = int(
        os.getenv("PROFILING_MAX_ROWS", "100")
    )
    profiling_max_payload_bytes: int = int(
        os.getenv("PROFILING_MAX_PAYLOAD_BYTES", "500000")
    )
    profiling_max_queries_per_table: int = int(
        os.getenv("PROFILING_MAX_QUERIES_PER_TABLE", "50")
    )

    metadata_timeout_seconds: int = int(
        os.getenv("METADATA_TIMEOUT_SECONDS", "10")
    )
    metadata_max_rows: int = int(
        os.getenv("METADATA_MAX_ROWS", "5000")
    )
    metadata_max_payload_bytes: int = int(
        os.getenv("METADATA_MAX_PAYLOAD_BYTES", "2000000")
    )

    # File dataset ingestion
    file_storage_root: str = os.getenv(
        "FILE_STORAGE_ROOT",
        "./data/file_datasets"
    )
    file_max_size_bytes: int = int(
        os.getenv("FILE_MAX_SIZE_BYTES", str(50 * 1024 * 1024))
    )
    file_max_rows: int = int(
        os.getenv("FILE_MAX_ROWS", "500000")
    )
    file_max_columns: int = int(
        os.getenv("FILE_MAX_COLUMNS", "256")
    )
    file_ingest_timeout_seconds: int = int(
        os.getenv("FILE_INGEST_TIMEOUT_SECONDS", "120")
    )
    file_max_worksheets: int = int(
        os.getenv("FILE_MAX_WORKSHEETS", "20")
    )

    # Document RAG
    document_storage_root: str = os.getenv(
        "DOCUMENT_STORAGE_ROOT",
        "./data/documents"
    )
    document_max_size_bytes: int = int(
        os.getenv("DOCUMENT_MAX_SIZE_BYTES", str(10 * 1024 * 1024))
    )
    document_max_chunks: int = int(
        os.getenv("DOCUMENT_MAX_CHUNKS", "500")
    )
    document_chunk_size: int = int(
        os.getenv("DOCUMENT_CHUNK_SIZE", "800")
    )
    document_chunk_overlap: int = int(
        os.getenv("DOCUMENT_CHUNK_OVERLAP", "120")
    )
    rag_top_k: int = int(os.getenv("RAG_TOP_K", "5"))
    rag_min_similarity: float = float(
        os.getenv("RAG_MIN_SIMILARITY", "0.15")
    )

    # Files attached in earlier turns are only used when relevant.
    rag_followup_min_similarity: float = float(
        os.getenv("RAG_FOLLOWUP_MIN_SIMILARITY", "0.30")
    )

    # Development/tests default to fake; production must set a real provider.
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "fake")
    embedding_model: str = os.getenv(
        "EMBEDDING_MODEL",
        "text-embedding-3-small"
    )
    embedding_dimension: int = int(
        os.getenv("EMBEDDING_DIMENSION", "384")
    )
    embedding_api_key: str = (
        os.getenv("EMBEDDING_API_KEY", "")
        or os.getenv("LLM_API_KEY", "")
    )
    embedding_base_url: str = (
        os.getenv("EMBEDDING_BASE_URL", "")
        or os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    )

    # Standalone insurance fraud prediction API.
    # Optional so existing BI-only deployments continue to start.
    fraud_prediction_api_url: str = os.getenv(
        "FRAUD_PREDICTION_API_URL",
        ""
    )
    fraud_prediction_api_key: str = os.getenv(
        "FRAUD_PREDICTION_API_KEY",
        ""
    )
    fraud_prediction_timeout_seconds: int = int(
        os.getenv("FRAUD_PREDICTION_TIMEOUT_SECONDS", "15")
    )

    # "supervisor": an LLM orchestrates specialist agents.
    # "pipeline": the older classify-then-route graph.
    agent_mode: str = os.getenv(
        "AGENT_MODE",
        "supervisor"
    ).strip().lower()

    # Auto mode: an LLM decides between database and uploaded files.
    source_router_enabled: bool = (
        os.getenv("SOURCE_ROUTER_ENABLED", "true").lower()
        in {"1", "true", "yes"}
    )
    source_router_min_similarity: float = float(
        os.getenv("SOURCE_ROUTER_MIN_SIMILARITY", "0.25")
    )

    # Chat memory: per-user facts + past-chat recall.
    memory_enabled: bool = (
        os.getenv("MEMORY_ENABLED", "true").lower()
        in {"1", "true", "yes"}
    )
    memory_max_facts: int = int(
        os.getenv("MEMORY_MAX_FACTS", "30")
    )
    memory_recall_chats: int = int(
        os.getenv("MEMORY_RECALL_CHATS", "3")
    )
    memory_recall_min_similarity: float = float(
        os.getenv("MEMORY_RECALL_MIN_SIMILARITY", "0.35")
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def is_production_env(app_env: str | None = None) -> bool:
    """True when deployed for production (distributed auth limits required)."""
    env = (
        app_env if app_env is not None else get_settings().app_env
    ).strip().lower()
    return env in {"production", "prod"}
