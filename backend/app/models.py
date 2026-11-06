from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.db_types import EmbeddingVector


class Tenant(Base):
    """One customer organization. Every other row points back here via tenant_id."""

    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    users: Mapped[list["User"]] = relationship(back_populates="tenant")


class User(Base):
    """A person who belongs to exactly one tenant."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="analyst", index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    otp_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    otp_verify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    otp_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Long-term chat memory (facts + past-chat recall); the user can turn it off.
    memory_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # Platform operator: creates organizations. Independent of the org role.
    is_super_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user")


class UserInvite(Base):
    """One-time invite link: the invitee sets a password and is verified."""

    __tablename__ = "user_invites"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    invited_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Department(Base):
    """A group of org members who share access to connections/schemas."""

    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_departments_tenant_name"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DepartmentMember(Base):
    __tablename__ = "department_members"

    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)


class DepartmentGrant(Base):
    """Access to a whole connection (schema_name NULL) or one of its schemas."""

    __tablename__ = "department_grants"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


class RefreshToken(Base):
    """Opaque refresh tokens stored as hashes (revocable)."""

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class Connection(Base):
    """Encrypted credentials for a customer's own database (filled in later)."""

    __tablename__ = "connections"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    db_type: Mapped[str] = mapped_column(String(40), nullable=False)  # postgres / mysql / mssql
    encrypted_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Defaults to "all" so a newly connected database with multiple schemas
    # is fully visible to the agent without an extra manual discovery step.
    discover_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="all")
    discover_schemas_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    include_views: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    profile_on_discover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_discover_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_discover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SchemaDiscoveryRun(Base):
    """Lifecycle record for a schema discovery job."""

    __tablename__ = "schema_discovery_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="default")
    schemas_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    tables_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SemanticMigrationReport(Base):
    """Manual-resolution report for unmapped semantic objects after migration."""

    __tablename__ = "semantic_migration_reports"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(40), nullable=False)
    object_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    object_name: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaTable(Base):
    """One discovered table for a tenant connection.

    columns_json holds SchemaTablePayload JSON:
    {"columns": [...], "foreign_keys": [...]}
    See app.schemas.
    """

    __tablename__ = "schema_tables"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "schema_name",
            "table_name",
            name="uq_schema_tables_connection_schema_table",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id"), nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    table_name: Mapped[str] = mapped_column(String(200), nullable=False)
    columns_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    estimated_row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profiling_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Metric(Base):
    """Semantic metric bound to one physical column within a connection."""

    __tablename__ = "metrics"
    __table_args__ = (
        UniqueConstraint("tenant_id", "connection_id", "name", name="uq_metrics_tenant_conn_name"),
        Index(
            "uq_metrics_tenant_conn_source_ref",
            "tenant_id",
            "connection_id",
            "source_column_ref",
            unique=True,
            sqlite_where=text("source_column_ref IS NOT NULL"),
            postgresql_where=text("source_column_ref IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("connections.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str | None] = mapped_column(String(40), nullable=True)
    synonyms_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    source_column_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    physical_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    physical_table: Mapped[str | None] = mapped_column(String(200), nullable=True)
    physical_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mapping_status: Mapped[str] = mapped_column(String(20), nullable=False, default="mapped")
    domain: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Dimension(Base):
    """Semantic dimension bound to one physical column within a connection."""

    __tablename__ = "dimensions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "connection_id", "name", name="uq_dimensions_tenant_conn_name"
        ),
        Index(
            "uq_dimensions_tenant_conn_source_ref",
            "tenant_id",
            "connection_id",
            "source_column_ref",
            unique=True,
            sqlite_where=text("source_column_ref IS NOT NULL"),
            postgresql_where=text("source_column_ref IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("connections.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str | None] = mapped_column(String(40), nullable=True)
    synonyms_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    source_column_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    physical_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    physical_table: Mapped[str | None] = mapped_column(String(200), nullable=True)
    physical_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mapping_status: Mapped[str] = mapped_column(String(20), nullable=False, default="mapped")
    domain: Mapped[str | None] = mapped_column(String(120), nullable=True)


class SemanticEntity(Base):
    """Business entity inferred from a physical table within a connection."""

    __tablename__ = "semantic_entities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "name",
            name="uq_semantic_entities_tenant_conn_name",
        ),
        Index(
            "uq_semantic_entities_tenant_conn_source_table",
            "tenant_id",
            "connection_id",
            "source_table",
            unique=True,
            sqlite_where=text("source_table IS NOT NULL"),
            postgresql_where=text("source_table IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("connections.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_table: Mapped[str | None] = mapped_column(String(300), nullable=True)
    physical_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    physical_table: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mapping_status: Mapped[str] = mapped_column(String(20), nullable=False, default="mapped")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="suggested")


class DataDictionaryEntry(Base):
    """Business description of a table or column in a connection.

    Kept apart from schema_tables (which rediscovery deletes and rebuilds)
    so curated descriptions survive rediscovery. ``column_name`` is "" for
    a table-level description; ``schema_name`` is "" when unknown. Empty
    strings rather than NULL keep the unique constraint effective.
    """

    __tablename__ = "data_dictionary_entries"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "schema_name",
            "table_name",
            "column_name",
            name="uq_data_dictionary_conn_table_column",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("connections.id"), nullable=False, index=True
    )
    schema_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    table_name: Mapped[str] = mapped_column(String(200), nullable=False)
    column_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # "import" (from a file), "ai" (AI-written table summary), "manual".
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="import")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    """Uploaded document for RAG."""

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentChunk(Base):
    """One text chunk from a document with optional embedding vector."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_tenant_document", "tenant_id", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(384), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    """Saved chat history for a tenant."""

    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New chat")
    connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("connections.id"), nullable=True)
    dashboard_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("dashboards.id", use_alter=True), nullable=True
    )
    analysis_state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserMemory(Base):
    """Long-term memory for one user, recalled across conversations.

    ``kind="fact"``: a durable note about the user ("works on the North
    region", "prefers monthly figures"). ``kind="conversation"``: the running
    summary of one chat (one row per conversation), searched by embedding so a
    new chat can recall related earlier ones."""

    __tablename__ = "user_memories"
    __table_args__ = (
        Index("ix_user_memories_owner_kind", "tenant_id", "user_id", "kind"),
        UniqueConstraint("conversation_id", name="uq_user_memories_conversation"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(384), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConversationConnection(Base):
    """Extra connections a conversation can investigate across, beyond its
    primary ``Conversation.connection_id`` — additive only, for the
    multi-domain investigator (app.agent.investigator). The primary
    connection stays the source of truth for the existing single-shot
    chat/dashboard REST paths, which are untouched by this table; it only
    ever adds connections an investigation may also check, never replaces
    the primary or changes single-query chat behavior."""

    __tablename__ = "conversation_connections"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "connection_id", name="uq_conversation_connections_pair"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("connections.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    """One message in a persisted conversation."""

    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dashboard(Base):
    """Persistent dashboard artifact."""

    __tablename__ = "dashboards"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id"), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    layout_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DashboardWidget(Base):
    """One widget on a dashboard."""

    __tablename__ = "dashboard_widgets"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    dashboard_id: Mapped[UUID] = mapped_column(ForeignKey("dashboards.id"), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    position_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sql_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DashboardVersion(Base):
    """Snapshot of a dashboard for undo/restore."""

    __tablename__ = "dashboard_versions"
    __table_args__ = (
        UniqueConstraint(
            "dashboard_id",
            "version_number",
            name="uq_dashboard_versions_dashboard_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    dashboard_id: Mapped[UUID] = mapped_column(ForeignKey("dashboards.id"), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SemanticRule(Base):
    """Business rule, agent setting or vocabulary entry for one connection.

    Imported from a customer's semantic-model workbook and shown to the SQL
    prompts: ``rule`` (e.g. "Default revenue is net_sales_value"), ``setting``
    (e.g. default_volume_unit = MT) or ``vocabulary`` (a concept and the
    words users say for it, in ``terms_json``). ``rule_key`` is the id or
    parameter from the file, else the normalised rule text.
    """

    __tablename__ = "semantic_rules"
    __table_args__ = (
        UniqueConstraint("connection_id", "kind", "rule_key", name="uq_semantic_rules_conn_kind_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("connections.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    guidance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    terms_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "import" (from a file) or "ai" (drafted by AI from the schema).
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="import")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SemanticExample(Base):
    """An example business question for a connection, with the tables a
    correct answer should use. Used to evaluate the assistant."""

    __tablename__ = "semantic_examples"
    __table_args__ = (
        UniqueConstraint("connection_id", "question_key", name="uq_semantic_examples_conn_question"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("connections.id"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_key: Mapped[str] = mapped_column(String(300), nullable=False)
    tables_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="import")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SemanticRelationship(Base):
    """Relationship between semantic entities."""

    __tablename__ = "semantic_relationships"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("connections.id"), nullable=True, index=True)
    from_entity: Mapped[str] = mapped_column(String(200), nullable=False)
    to_entity: Mapped[str] = mapped_column(String(200), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(40), nullable=False, default="many_to_one")
    source_fk_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    mapping_status: Mapped[str] = mapped_column(String(20), nullable=False, default="mapped")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="suggested")


class Dataset(Base):
    """File-backed analytical dataset (CSV sheet or Excel worksheet)."""

    __tablename__ = "datasets"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    physical_table: Mapped[str] = mapped_column(String(120), nullable=False)
    sheet_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    column_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ingestion_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    columns_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LLMUsage(Base):
    """LLM/embedding usage in 5-minute buckets: one row per user per bucket
    per model per feature (chat, schema_discovery, semantic_model, …).

    Each call upserts (adds to) its row — see app.llm.usage. Sum rows for
    per-user, per-org, per-feature or per-period totals."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "bucket_start",
            "model",
            "feature",
            name="uq_llm_usage_key",
        ),
        Index("ix_llm_usage_tenant_bucket", "tenant_id", "bucket_start"),
        Index("ix_llm_usage_user_bucket", "user_id", "bucket_start"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    feature: Mapped[str] = mapped_column(String(40), nullable=False, default="other")
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    """Immutable audit trail for security-sensitive actions."""

    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
