from uuid import UUID

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, model_validator, field_validator

from app.security import PASSWORD_POLICY_MESSAGE, validate_password_complexity


def validate_password_strength(password: str) -> str:
    """Validate password strength (unified policy message)."""
    return validate_password_complexity(password)


class SignupRequest(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return validate_password_complexity(value)


class OTPVerificationRequest(BaseModel):
    email: EmailStr
    otp_code: str = Field(min_length=4, max_length=10)


class ResendOTPRequest(BaseModel):
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    tenant_id: UUID
    user_id: UUID
    email: str


class SignupResponse(BaseModel):
    message: str
    email: str


class MessageResponse(BaseModel):
    message: str


class DepartmentRef(BaseModel):
    id: UUID
    name: str


class UserMeResponse(BaseModel):
    id: UUID
    email: str
    tenant_id: UUID
    tenant_name: str
    is_verified: bool
    role: str = "analyst"
    is_super_admin: bool = False
    departments: list[DepartmentRef] = Field(default_factory=list)


class UpdateProfileRequest(BaseModel):
    email: EmailStr | None = None
    tenant_name: str | None = Field(default=None, min_length=2, max_length=120)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "UpdateProfileRequest":
        if self.email is None and self.tenant_name is None:
            raise ValueError("Provide email and/or tenant_name")
        return self


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return validate_password_strength(value)


class LogoutResponse(BaseModel):
    message: str


# Stubs kept for later features (connections, chat, docs, metrics)
class DatabaseType(str, Enum):
    postgres = "postgres"
    mysql = "mysql"
    mssql = "mssql"
    csv = "csv"
    excel = "excel"

class ConnectionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    db_type: DatabaseType
    connection_url: str

    @model_validator(mode="after")
    def validate_connection_url(self):
        url = self.connection_url.lower()

        if self.db_type == DatabaseType.postgres:
            if not url.startswith("postgresql://"):
                raise ValueError(
                    "Postgres URL must start with 'postgresql://'"
                )

        elif self.db_type == DatabaseType.mysql:
            if not url.startswith("mysql://"):
                raise ValueError(
                    "MySQL URL must start with 'mysql://'"
                )

        elif self.db_type == DatabaseType.mssql:
            if not url.startswith("mssql://"):
                raise ValueError(
                    "MSSQL URL must start with 'mssql://'"
                )

        elif self.db_type in {DatabaseType.csv, DatabaseType.excel}:
            raise ValueError(
                "File datasets must be created via POST /api/v1/datasets/upload"
            )

        return self


class ConnectionResponse(BaseModel):
    id: UUID
    name: str
    db_type: DatabaseType
    tenant_id: UUID
    last_discover_status: str | None = None
    discover_scope: str | None = None


class SchemaDiscoveryStatusResponse(BaseModel):
    connection_id: UUID
    run_id: UUID | None = None
    status: str
    scope: str | None = None
    warnings: list[str] = Field(default_factory=list)
    tables_discovered: int = 0
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ConnectionUpdate(BaseModel):
    """Partial update. Credentials are optional; omit to keep the stored URL."""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    db_type: DatabaseType | None = None
    connection_url: str | None = None
    discover_scope: Literal["default", "all", "selected"] | None = None
    discover_schemas: list[str] | None = None

    @model_validator(mode="after")
    def validate_update(self):
        if self.name is None and self.db_type is None and self.connection_url is None:
            raise ValueError("Provide at least one field to update")

        if self.db_type is not None and self.connection_url is None:
            raise ValueError("connection_url is required when changing db_type")

        if self.connection_url is not None:
            if self.db_type is None:
                raise ValueError("db_type is required when connection_url is provided")
            url = self.connection_url.lower()
            if self.db_type == DatabaseType.postgres and not url.startswith("postgresql://"):
                raise ValueError("Postgres URL must start with 'postgresql://'")
            if self.db_type == DatabaseType.mysql and not url.startswith("mysql://"):
                raise ValueError("MySQL URL must start with 'mysql://'")
            if self.db_type == DatabaseType.mssql and not url.startswith("mssql://"):
                raise ValueError("MSSQL URL must start with 'mssql://'")

        return self


class ConnectionTestResponse(BaseModel):
    ok: bool
    message: str


# ---------------------------------------------------------------------------
# Schema discovery contract
#
# One table row in schema_tables stores:
#   table_name   = "orders"
#   columns_json = JSON string of SchemaTablePayload (columns + foreign_keys)
#
# Example columns_json for orders:
# {
#   "columns": [
#     {"name": "order_id", "data_type": "integer", "nullable": false, "is_pk": true},
#     {"name": "customer_id", "data_type": "integer", "nullable": true, "is_pk": false},
#     {"name": "product_id", "data_type": "integer", "nullable": true, "is_pk": false},
#     {"name": "quantity", "data_type": "integer", "nullable": false, "is_pk": false},
#     {"name": "order_date", "data_type": "date", "nullable": true, "is_pk": false}
#   ],
#   "foreign_keys": [
#     {
#       "column": "customer_id",
#       "referred_table": "customers",
#       "referred_column": "customer_id"
#     },
#     {
#       "column": "product_id",
#       "referred_table": "products",
#       "referred_column": "product_id"
#     }
#   ]
# }
# ---------------------------------------------------------------------------


class SchemaColumn(BaseModel):
    """One column on a discovered table."""

    name: str
    data_type: str
    nullable: bool
    is_pk: bool = False


class SchemaForeignKey(BaseModel):
    """One foreign key on a discovered table."""

    column: str
    referred_table: str
    referred_column: str
    referred_schema: str | None = None


class SchemaTablePayload(BaseModel):
    """What we store inside schema_tables.columns_json (as a JSON string)."""

    columns: list[SchemaColumn]
    foreign_keys: list[SchemaForeignKey] = []
    schema_name: str | None = None


class DiscoveredTable(BaseModel):
    """One table as returned by introspect_schema / discover."""

    table_name: str
    schema_name: str = "public"
    columns: list[SchemaColumn]
    foreign_keys: list[SchemaForeignKey] = []


class SchemaDiscoverRequest(BaseModel):
    """POST /connections/{id}/discover body."""

    scope: Literal["default", "all", "selected"] = "default"
    schemas: list[str] = []


class SchemaDiscoverResponse(BaseModel):
    """POST /connections/{id}/discover response."""

    connection_id: UUID
    tables: list[DiscoveredTable]
    schemas_used: list[str] = []
    # Present when scope="all" enqueues a background run instead of
    # discovering inline — the caller must poll /discover/status.
    status: str | None = None
    run_id: UUID | None = None


class SchemaListResponse(BaseModel):
    """GET /connections/{id}/schema response (cached rows)."""

    connection_id: UUID
    tables: list[DiscoveredTable]
    schemas_used: list[str] = []


class SchemaCatalogResponse(BaseModel):
    """GET /connections/{id}/schemas — schemas available to discover."""

    connection_id: UUID
    schemas: list[str]
    default_schema: str


class QuestionRequest(BaseModel):
    question: str


class ConversationCreate(BaseModel):
    title: str = Field(default="New chat", min_length=1, max_length=200)
    connection_id: UUID | None = None


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ConversationMessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationDocumentResponse(BaseModel):
    id: UUID
    filename: str = ""


class ConversationResponse(BaseModel):
    id: UUID
    title: str
    tenant_id: UUID
    user_id: UUID | None = None
    connection_id: UUID | None = None
    dashboard_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    messages: list[ConversationMessageResponse] = Field(default_factory=list)
    # Files attached in this conversation; later questions can use them.
    documents: list[ConversationDocumentResponse] = Field(default_factory=list)
    # Sticky @ scope: {"source": "auto|documents|database", "document_ids": [...]}.
    source_scope: dict[str, Any] | None = None


class ConversationListItem(BaseModel):
    id: UUID
    title: str
    tenant_id: UUID
    connection_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None


class SearchChatHit(BaseModel):
    id: UUID
    title: str
    archived: bool = False
    updated_at: datetime | None = None
    # Matching message text; None when the title matched.
    snippet: str | None = None


class SearchDashboardHit(BaseModel):
    id: UUID
    name: str
    updated_at: datetime | None = None
    # Matching widget title; None when the name matched.
    snippet: str | None = None


class SearchResponse(BaseModel):
    query: str
    chats: list[SearchChatHit]
    dashboards: list[SearchDashboardHit]


class ConversationConnectionCreate(BaseModel):
    connection_id: UUID


class ConversationConnectionResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    connection_id: UUID
    created_at: datetime | None = None


class AnalysisStateResponse(BaseModel):
    conversation_id: UUID
    title: str
    message_count: int = 0
    analysis_state: dict[str, Any] = Field(default_factory=dict)
    last_sql: str | None = None
    turn_count: int = 0


class DashboardWidgetResponse(BaseModel):
    id: str
    type: str
    title: str
    sql: str | None = None
    chart: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    position: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    value: Any | None = None
    error: str | None = None
    content: str | None = None
    insight_status: str | None = None
    insight_generated_at: str | None = None
    # Presentation metadata the dashboard page groups, orders and formats by.
    section: str | None = None
    metric: str | None = None
    purpose: str | None = None
    dimension: str | None = None
    polarity: str | None = None
    value_kind: str | None = None
    aggregation: str | None = None
    measure_labels: dict[str, str] | None = None
    result: dict[str, Any] | None = None
    unit: str | None = None
    note: str | None = None
    data_warning: str | None = None
    # "rolling_12m" (last 12 months vs prior 12) or "snapshot" (latest state).
    period_kind: str | None = None
    # Not re-queried this time (over the per-refresh cap); rows are the last stored.
    stale: bool = False


class DashboardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connection_id: UUID
    conversation_id: UUID | None = None
    spec: dict[str, Any] | None = None


class DashboardSummaryResponse(BaseModel):
    id: UUID
    name: str
    connection_id: UUID
    conversation_id: UUID | None = None
    widget_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DashboardResponse(BaseModel):
    id: UUID
    name: str
    tenant_id: UUID
    user_id: UUID
    connection_id: UUID
    conversation_id: UUID | None = None
    filters: list[Any] = Field(default_factory=list)
    layout: dict[str, Any] = Field(default_factory=dict)
    source_table: str | None = None
    widgets: list[DashboardWidgetResponse] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DashboardVersionResponse(BaseModel):
    version_number: int
    created_at: datetime | None = None
    created_by: UUID | None = None


class DashboardFilterItem(BaseModel):
    field: str = Field(min_length=1, max_length=120)
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte", "in", "like"] = "eq"
    value: str | int | float | list[str] | None = None


class DashboardFiltersUpdate(BaseModel):
    filters: list[DashboardFilterItem] = Field(default_factory=list)


class SemanticDiscoverResponse(BaseModel):
    connection_id: UUID
    metrics: int = 0
    dimensions: int = 0
    entities: int = 0
    relationships: int = 0


class DocumentResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    filename: str
    original_filename: str | None = None
    content_type: str | None = None
    status: str
    error_message: str | None = None
    chunk_count: int = 0
    file_size_bytes: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]


class MetricCreate(BaseModel):
    connection_id: UUID
    name: str = Field(min_length=1, max_length=120)
    expression: str = Field(min_length=1)
    # Business domain label (e.g. "Sales", "Inventory") used by the
    # multi-domain investigator. Omit to let it be auto-derived.
    domain: str | None = Field(default=None, max_length=120)


class MetricUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    expression: str | None = Field(default=None, min_length=1)
    status: Literal["suggested", "confirmed"] | None = None
    # Sending ``null`` or a blank string clears a curated domain so the
    # investigator falls back to the auto-derived one; omitting it leaves it.
    domain: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "MetricUpdate":
        if not self.model_fields_set & {"name", "expression", "status", "domain"}:
            raise ValueError("Provide name, expression, status, and/or domain")
        return self


class MetricResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    expression: str
    status: str | None = None
    domain: str | None = None


class DimensionCreate(BaseModel):
    connection_id: UUID
    name: str = Field(min_length=1, max_length=120)
    expression: str = Field(min_length=1)
    # Business domain label (e.g. "Sales", "Inventory") used by the
    # multi-domain investigator. Omit to let it be auto-derived.
    domain: str | None = Field(default=None, max_length=120)


class DimensionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    expression: str | None = Field(default=None, min_length=1)
    status: Literal["suggested", "confirmed"] | None = None
    # Sending ``null`` or a blank string clears a curated domain so the
    # investigator falls back to the auto-derived one; omitting it leaves it.
    domain: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "DimensionUpdate":
        if not self.model_fields_set & {"name", "expression", "status", "domain"}:
            raise ValueError("Provide name, expression, status, and/or domain")
        return self


class DimensionResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    expression: str
    status: str | None = None
    domain: str | None = None


class ImportSkippedRow(BaseModel):
    # 1-based row number in the file, or None when not tied to one row.
    row: int | None = None
    reason: str


class DataDictionaryRow(BaseModel):
    schema_name: str = Field(default="", max_length=200)
    table_name: str = Field(min_length=1, max_length=200)
    # "" = a description of the table itself.
    column_name: str = Field(default="", max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    origin: Literal["import", "ai", "manual"] = "import"


class DataDictionaryPreviewRow(DataDictionaryRow):
    action: Literal["create", "update", "unchanged"]
    existing_description: str | None = None


class DataDictionaryPreviewResponse(BaseModel):
    filename: str
    connection_id: UUID
    entries: list[DataDictionaryPreviewRow]
    skipped: list[ImportSkippedRow]


class DataDictionaryBulkRequest(BaseModel):
    connection_id: UUID
    entries: list[DataDictionaryRow] = Field(min_length=1, max_length=5000)
    overwrite: bool = False


class DataDictionaryBulkResponse(BaseModel):
    created: int
    updated: int
    unchanged: int
    kept_existing: int
    # Rows naming a table/column that no longer exists in the connection.
    rejected: int


class DataDictionaryEntryResponse(DataDictionaryRow):
    id: UUID
    connection_id: UUID


class DataDictionaryUpdate(BaseModel):
    description: str = Field(min_length=1, max_length=2000)


class SemanticModelKpi(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    formula: str = Field(min_length=1, max_length=1000)
    description: str = Field(default="", max_length=2000)
    domain: str = Field(default="", max_length=120)
    synonyms: list[str] = Field(default_factory=list, max_length=50)


class SemanticModelKpiPreview(SemanticModelKpi):
    action: Literal["create", "update", "link", "unchanged", "conflict", "invalid"]
    origin: Literal["import", "ai"] = "import"
    # The SQL the metric will store; empty when the formula is invalid.
    expression: str = ""
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    existing_name: str | None = None
    existing_expression: str | None = None
    row: int | None = None


class SemanticModelRelationship(BaseModel):
    from_schema: str = Field(default="", max_length=200)
    from_table: str = Field(min_length=1, max_length=200)
    from_column: str = Field(min_length=1, max_length=200)
    to_schema: str = Field(default="", max_length=200)
    to_table: str = Field(min_length=1, max_length=200)
    to_column: str = Field(min_length=1, max_length=200)
    cardinality: str = Field(default="", max_length=40)
    description: str = Field(default="", max_length=2000)


class SemanticModelRelationshipPreview(SemanticModelRelationship):
    action: Literal["create", "confirm", "unchanged", "invalid"]
    origin: Literal["import", "inferred"] = "import"
    # Canonical join, e.g. "data_mart.fact_sales.customer_key = data_mart.dim_customer.customer_key".
    join: str = ""
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    note: str | None = None
    row: int | None = None


class SemanticModelRule(BaseModel):
    # rule: business rule; setting: default such as "default_volume_unit = MT";
    # vocabulary: a concept (in ``rule``) and the words users say for it.
    kind: Literal["rule", "setting", "vocabulary"]
    name: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=120)
    rule: str = Field(min_length=1, max_length=2000)
    guidance: str = Field(default="", max_length=2000)
    terms: list[str] = Field(default_factory=list, max_length=30)
    origin: Literal["import", "ai"] = "import"


class SemanticModelRulePreview(SemanticModelRule):
    action: Literal["create", "update", "unchanged"]
    warnings: list[str] = Field(default_factory=list)
    row: int | None = None


class SemanticRuleResponse(SemanticModelRule):
    id: UUID
    connection_id: UUID


class SemanticModelExample(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    tables: list[str] = Field(default_factory=list, max_length=30)
    domain: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=2000)
    origin: Literal["import", "ai"] = "import"


class SemanticModelExamplePreview(SemanticModelExample):
    action: Literal["create", "unchanged"]
    warnings: list[str] = Field(default_factory=list)
    row: int | None = None


class SemanticExampleResponse(SemanticModelExample):
    id: UUID
    connection_id: UUID


class SemanticModelPreviewResponse(BaseModel):
    filename: str
    connection_id: UUID
    kpis: list[SemanticModelKpiPreview]
    relationships: list[SemanticModelRelationshipPreview]
    rules: list[SemanticModelRulePreview] = Field(default_factory=list)
    dictionary: list[DataDictionaryPreviewRow] = Field(default_factory=list)
    examples: list[SemanticModelExamplePreview] = Field(default_factory=list)
    skipped: list[ImportSkippedRow]
    # How sheets were read and what AI drafted, for the reviewer.
    notes: list[str] = Field(default_factory=list)


class SemanticModelSaveRequest(BaseModel):
    connection_id: UUID
    kpis: list[SemanticModelKpi] = Field(default_factory=list, max_length=1000)
    relationships: list[SemanticModelRelationship] = Field(default_factory=list, max_length=2000)
    rules: list[SemanticModelRule] = Field(default_factory=list, max_length=1000)
    dictionary: list[DataDictionaryRow] = Field(default_factory=list, max_length=5000)
    examples: list[SemanticModelExample] = Field(default_factory=list, max_length=500)
    # Replace existing data dictionary descriptions that differ.
    overwrite_dictionary: bool = False


class SemanticModelSaveResponse(BaseModel):
    kpis: dict[str, int]
    relationships: dict[str, int]
    rules: dict[str, int] = Field(default_factory=dict)
    dictionary: dict[str, int] = Field(default_factory=dict)
    examples: dict[str, int] = Field(default_factory=dict)


class ExampleEvaluationRow(BaseModel):
    question: str
    status: Literal["pass", "wrong_tables", "fallback", "error", "no_answer"]
    expected_tables: list[str]
    tables_used: list[str]
    missing_tables: list[str]
    row_count: int | None = None
    error: str | None = None


class ExampleEvaluationResponse(BaseModel):
    total: int
    passed: int
    results: list[ExampleEvaluationRow]


class SemanticCatalogResponse(BaseModel):
    """Full semantic layer for prompt assembly (metrics + dimensions)."""

    metrics: list[MetricResponse]
    dimensions: list[DimensionResponse]


class WebSocketMessage(BaseModel):
    """Legacy thin envelope.
    """

    type: str
    content: str


class DatasetResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    connection_id: UUID
    name: str
    display_name: str
    source_type: str
    physical_table: str
    sheet_name: str | None = None
    status: str
    version: int
    row_count: int
    column_count: int
    original_filename: str | None = None
    content_type: str | None = None
    file_size_bytes: int | None = None
    ingestion_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DatasetListResponse(BaseModel):
    datasets: list[DatasetResponse]


class DatasetUploadResponse(BaseModel):
    connection_id: UUID
    datasets: list[DatasetResponse]
    discovery_status: str
    semantic_counts: dict[str, int] = Field(default_factory=dict)


# ----- Organizations: invites, users, departments (org admin) + orgs (super admin) -----

OrgRole = Literal["admin", "analyst", "viewer"]


class AuthConfigResponse(BaseModel):
    allow_public_signup: bool


class InvitePreviewResponse(BaseModel):
    email: str
    tenant_name: str


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return validate_password_complexity(value)


class InviteResult(BaseModel):
    """invite_url is returned only when the email could not be sent, so the
    admin can share the link another way."""

    email_sent: bool
    invite_url: str | None = None


class OrgUserResponse(BaseModel):
    id: UUID
    email: str
    role: str
    status: Literal["active", "invited"]
    department_ids: list[UUID]
    created_at: datetime | None = None


class OrgUserInviteRequest(BaseModel):
    email: EmailStr
    role: OrgRole = "analyst"
    department_ids: list[UUID] = Field(default_factory=list)


class OrgUserInviteResponse(BaseModel):
    user: OrgUserResponse
    invite: InviteResult


class OrgUserUpdate(BaseModel):
    role: OrgRole | None = None
    department_ids: list[UUID] | None = None


class DepartmentGrantItem(BaseModel):
    connection_id: UUID
    # None/blank = the whole connection.
    schema_name: str | None = Field(default=None, max_length=200)


class DepartmentResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    member_ids: list[UUID]
    grants: list[DepartmentGrantItem]


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class DepartmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class DepartmentMembersUpdate(BaseModel):
    user_ids: list[UUID]


class DepartmentGrantsUpdate(BaseModel):
    grants: list[DepartmentGrantItem]


class PlatformOrgResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime | None = None
    disabled: bool
    user_count: int
    admin_emails: list[str]


class PlatformOrgCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    admin_email: EmailStr


class PlatformOrgCreateResponse(BaseModel):
    org: PlatformOrgResponse
    invite: InviteResult


class PlatformOrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    disabled: bool | None = None


class PlatformOrgAdminCreate(BaseModel):
    email: EmailStr
