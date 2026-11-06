"""Semantic layer CRUD: metrics, dimensions, and the data dictionary."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import accessible_connection, scope_for_user
from app.audit import log_audit
from app.database import get_db
from app.models import (
    Connection,
    DataDictionaryEntry,
    Dimension,
    Metric,
    SemanticExample,
    SemanticRule,
    User,
)
from app.redis_client import schema_cache, semantic_cache
from app.routes.auth import get_current_user, require_role
from app.schemas import (
    DataDictionaryBulkRequest,
    DataDictionaryBulkResponse,
    DataDictionaryEntryResponse,
    DataDictionaryPreviewResponse,
    DataDictionaryPreviewRow,
    DataDictionaryUpdate,
    DimensionCreate,
    DimensionResponse,
    DimensionUpdate,
    ImportSkippedRow,
    MetricCreate,
    MetricResponse,
    MetricUpdate,
    SemanticCatalogResponse,
    SemanticDiscoverResponse,
    SemanticModelKpiPreview,
    SemanticModelPreviewResponse,
    SemanticModelExamplePreview,
    SemanticModelRelationshipPreview,
    SemanticModelRulePreview,
    SemanticModelSaveRequest,
    SemanticModelSaveResponse,
    SemanticRuleResponse,
    SemanticExampleResponse,
    ExampleEvaluationResponse,
    ExampleEvaluationRow,
)
from app.services.file_import import MAX_FILE_BYTES, ImportFileError
from app.services.data_dictionary import (
    DictRow,
    build_dictionary_preview,
    entry_key,
    load_entries,
    match_to_schema,
    upsert_entries,
)
from app.services.schema_discovery_service import get_stored_schema_for_tenant
from app.services import semantic_model_import as model_import
from app.services import semantic_examples, semantic_rules
from app.services import semantic_import_flow as import_flow
from app.services.semantic_discovery import discover_semantic_suggestions

def _clean_domain(value: str | None) -> str | None:
    """Normalise a curated domain label; blank means "not curated"."""
    if value is None:
        return None
    return value.strip() or None


router = APIRouter(
    prefix="/api/v1/semantic",
    tags=["semantic"],
)


def _metric_response(row: Metric) -> MetricResponse:
    return MetricResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        expression=row.expression,
        status=getattr(row, "status", None),
        domain=row.domain,
    )


def _dimension_response(row: Dimension) -> DimensionResponse:
    return DimensionResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        expression=row.expression,
        status=getattr(row, "status", None),
        domain=row.domain,
    )


def _load_catalog(
    db: Session, tenant_id: UUID, connection_id: UUID, *, user: User | None = None
) -> SemanticCatalogResponse:
    metric_q = select(Metric).where(
        Metric.tenant_id == tenant_id,
        Metric.connection_id == connection_id,
    )
    dim_q = select(Dimension).where(
        Dimension.tenant_id == tenant_id,
        Dimension.connection_id == connection_id,
    )
    metrics = db.scalars(metric_q.order_by(Metric.name)).all()
    dimensions = db.scalars(dim_q.order_by(Dimension.name)).all()
    if user is not None:
        metrics = [m for m in metrics if _visible(db, user, connection_id, m.physical_schema)]
        dimensions = [d for d in dimensions if _visible(db, user, connection_id, d.physical_schema)]
    return SemanticCatalogResponse(
        metrics=[_metric_response(m) for m in metrics],
        dimensions=[_dimension_response(d) for d in dimensions],
    )



def _invalidate(tenant_id: UUID, connection_id: UUID) -> None:
    semantic_cache.invalidate(tenant_id, connection_id)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _get_metric(db: Session, *, metric_id: UUID, tenant_id: UUID) -> Metric:
    row = db.scalar(
        select(Metric).where(Metric.id == metric_id, Metric.tenant_id == tenant_id)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Metric not found",
        )
    return row


def _get_dimension(db: Session, *, dimension_id: UUID, tenant_id: UUID) -> Dimension:
    row = db.scalar(
        select(Dimension).where(
            Dimension.id == dimension_id,
            Dimension.tenant_id == tenant_id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dimension not found",
        )
    return row


def _get_connection(db: Session, *, connection_id: UUID, user: User) -> Connection:
    """Connection the user's departments grant (admins: any); else 404."""
    row = accessible_connection(db, connection_id=connection_id, user=user)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connection not found",
        )
    return row


def _visible(db: Session, user: User, connection_id: UUID | None, schema: str | None) -> bool:
    """Whether a semantic object bound to ``schema`` is inside the user's grants.
    Objects without a known schema stay visible (execution is still gated)."""
    if connection_id is None:
        return user.role == "admin"
    scope = scope_for_user(db, user)
    if not scope.can_use_connection(connection_id):
        return False
    allowed = scope.allowed_schemas(connection_id)
    if allowed is None or not schema:
        return True
    return schema.lower() in allowed


@router.post(
    "/discover/{connection_id}",
    response_model=SemanticDiscoverResponse,
    status_code=status.HTTP_201_CREATED,
)
def discover_semantic_layer(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Infer suggested metrics, dimensions, entities, and relationships from schema."""
    _get_connection(db, connection_id=connection_id, user=current_user)
    counts = discover_semantic_suggestions(db, connection_id, current_user.tenant_id)
    _invalidate(current_user.tenant_id, connection_id)
    log_audit(
        db,
        user=current_user,
        action="semantic.discover",
        resource_type="connection",
        resource_id=str(connection_id),
        metadata=counts,
    )
    db.commit()
    return SemanticDiscoverResponse(connection_id=connection_id, **counts)


# --- Catalog (prompt assembly) -------------------------------------------------


@router.get("", response_model=SemanticCatalogResponse)
def get_semantic_catalog(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return metrics + dimensions for a connection."""
    _get_connection(db, connection_id=connection_id, user=current_user)
    # Read straight from the DB: the semantic cache holds the agent's catalog
    # (confirmed metrics plus rules), a different shape from this response.
    return _load_catalog(db, current_user.tenant_id, connection_id, user=current_user)


# --- Metrics ------------------------------------------------------------------


@router.get("/metrics", response_model=list[MetricResponse])
def list_metrics(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_connection(db, connection_id=connection_id, user=current_user)
    rows = db.scalars(
        select(Metric)
        .where(
            Metric.tenant_id == current_user.tenant_id,
            Metric.connection_id == connection_id,
        )
        .order_by(Metric.name)
    ).all()
    return [
        _metric_response(r)
        for r in rows
        if _visible(db, current_user, connection_id, r.physical_schema)
    ]


@router.post(
    "/metrics",
    response_model=MetricResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_metric(
    request: MetricCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    _get_connection(
        db, connection_id=request.connection_id, user=current_user
    )
    name = request.name.strip()
    existing = db.scalar(
        select(Metric).where(
            Metric.tenant_id == current_user.tenant_id,
            Metric.connection_id == request.connection_id,
            Metric.name == name,
        )
    )
    if existing is not None:
        raise _conflict("A metric with this name already exists")
    row = Metric(
        tenant_id=current_user.tenant_id,
        connection_id=request.connection_id,
        name=name,
        expression=request.expression.strip(),
        domain=_clean_domain(request.domain),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("A metric with this name already exists") from exc
    db.refresh(row)
    if row.connection_id is not None:
        _invalidate(current_user.tenant_id, row.connection_id)
    return _metric_response(row)


@router.get("/metrics/{metric_id}", response_model=MetricResponse)
def get_metric(
    metric_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_metric(db, metric_id=metric_id, tenant_id=current_user.tenant_id)
    if not _visible(db, current_user, row.connection_id, row.physical_schema):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found")
    return _metric_response(row)


@router.patch("/metrics/{metric_id}", response_model=MetricResponse)
def update_metric(
    metric_id: UUID,
    request: MetricUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = _get_metric(db, metric_id=metric_id, tenant_id=current_user.tenant_id)
    if request.name is not None:
        row.name = request.name.strip()
    if request.expression is not None:
        row.expression = request.expression.strip()
    if request.status is not None:
        row.status = request.status
    if "domain" in request.model_fields_set:
        row.domain = _clean_domain(request.domain)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("A metric with this name already exists") from exc
    db.refresh(row)
    if row.connection_id is not None:
        _invalidate(current_user.tenant_id, row.connection_id)
    return _metric_response(row)


@router.delete(
    "/metrics/{metric_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_metric(
    metric_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = _get_metric(db, metric_id=metric_id, tenant_id=current_user.tenant_id)
    connection_id = row.connection_id
    db.delete(row)
    db.commit()
    if connection_id is not None:
        _invalidate(current_user.tenant_id, connection_id)


# --- Dimensions ---------------------------------------------------------------


@router.get("/dimensions", response_model=list[DimensionResponse])
def list_dimensions(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_connection(db, connection_id=connection_id, user=current_user)
    rows = db.scalars(
        select(Dimension)
        .where(
            Dimension.tenant_id == current_user.tenant_id,
            Dimension.connection_id == connection_id,
        )
        .order_by(Dimension.name)
    ).all()
    return [
        _dimension_response(r)
        for r in rows
        if _visible(db, current_user, connection_id, r.physical_schema)
    ]


@router.post(
    "/dimensions",
    response_model=DimensionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_dimension(
    request: DimensionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    _get_connection(
        db, connection_id=request.connection_id, user=current_user
    )
    name = request.name.strip()
    existing = db.scalar(
        select(Dimension).where(
            Dimension.tenant_id == current_user.tenant_id,
            Dimension.connection_id == request.connection_id,
            Dimension.name == name,
        )
    )
    if existing is not None:
        raise _conflict("A dimension with this name already exists")
    row = Dimension(
        tenant_id=current_user.tenant_id,
        connection_id=request.connection_id,
        name=name,
        expression=request.expression.strip(),
        domain=_clean_domain(request.domain),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("A dimension with this name already exists") from exc
    db.refresh(row)
    if row.connection_id is not None:
        _invalidate(current_user.tenant_id, row.connection_id)
    return _dimension_response(row)


@router.get("/dimensions/{dimension_id}", response_model=DimensionResponse)
def get_dimension(
    dimension_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_dimension(db, dimension_id=dimension_id, tenant_id=current_user.tenant_id)
    if not _visible(db, current_user, row.connection_id, row.physical_schema):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dimension not found")
    return _dimension_response(row)


@router.patch("/dimensions/{dimension_id}", response_model=DimensionResponse)
def update_dimension(
    dimension_id: UUID,
    request: DimensionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = _get_dimension(
        db, dimension_id=dimension_id, tenant_id=current_user.tenant_id
    )
    if request.name is not None:
        row.name = request.name.strip()
    if request.expression is not None:
        row.expression = request.expression.strip()
    if request.status is not None:
        row.status = request.status
    if "domain" in request.model_fields_set:
        row.domain = _clean_domain(request.domain)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("A dimension with this name already exists") from exc
    db.refresh(row)
    if row.connection_id is not None:
        _invalidate(current_user.tenant_id, row.connection_id)
    return _dimension_response(row)


@router.delete(
    "/dimensions/{dimension_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_dimension(
    dimension_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = _get_dimension(
        db, dimension_id=dimension_id, tenant_id=current_user.tenant_id
    )
    connection_id = row.connection_id
    db.delete(row)
    db.commit()
    if connection_id is not None:
        _invalidate(current_user.tenant_id, connection_id)


# --- Data dictionary (per-connection table/column descriptions) ---------------------


def _dictionary_response(row: DataDictionaryEntry) -> DataDictionaryEntryResponse:
    return DataDictionaryEntryResponse(
        id=row.id,
        connection_id=row.connection_id,
        schema_name=row.schema_name,
        table_name=row.table_name,
        column_name=row.column_name,
        description=row.description,
        origin=row.origin if row.origin in ("import", "ai", "manual") else "import",
    )


def _schema_or_422(db: Session, connection_id: UUID, tenant_id: UUID) -> dict:
    schema = get_stored_schema_for_tenant(db, connection_id, tenant_id)
    if not schema.get("tables"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Run schema discovery on this connection first",
        )
    return schema


def _get_dictionary_entry(db: Session, *, entry_id: UUID, tenant_id: UUID) -> DataDictionaryEntry:
    row = db.scalar(
        select(DataDictionaryEntry).where(
            DataDictionaryEntry.id == entry_id,
            DataDictionaryEntry.tenant_id == tenant_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return row


@router.get("/data-dictionary", response_model=list[DataDictionaryEntryResponse])
def list_data_dictionary(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_connection(db, connection_id=connection_id, user=current_user)
    return [
        _dictionary_response(r)
        for r in load_entries(db, current_user.tenant_id, connection_id)
        if _visible(db, current_user, connection_id, r.schema_name)
    ]


@router.post(
    "/data-dictionary/import/preview",
    response_model=DataDictionaryPreviewResponse,
)
async def preview_data_dictionary_import(
    connection_id: UUID,
    summarize: bool = True,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Parse a CSV / XLSX data dictionary against this connection's schema.

    Only rows naming a real table/column are returned. With ``summarize``,
    AI adds a one-line summary for tables that have none. Nothing is saved.
    """
    tenant_id = current_user.tenant_id
    _get_connection(db, connection_id=connection_id, user=current_user)
    schema = _schema_or_422(db, connection_id, tenant_id)
    content = await file.read(MAX_FILE_BYTES + 1)
    filename = file.filename or "data-dictionary"
    try:
        parsed = await run_in_threadpool(
            lambda: build_dictionary_preview(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                schema=schema,
                filename=filename,
                content=content,
                summarize=summarize,
            )
        )
    except ImportFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    existing = {entry_key(e): e for e in load_entries(db, tenant_id, connection_id)}
    entries = []
    for row in parsed.rows:
        current = existing.get(row.key)
        if current is None:
            action = "create"
        elif current.description == row.description:
            action = "unchanged"
        else:
            action = "update"
        entries.append(
            DataDictionaryPreviewRow(
                schema_name=row.schema_name,
                table_name=row.table_name,
                column_name=row.column_name,
                description=row.description,
                origin=row.origin,
                action=action,
                existing_description=current.description if action == "update" else None,
            )
        )
    return DataDictionaryPreviewResponse(
        filename=filename,
        connection_id=connection_id,
        entries=entries,
        skipped=[ImportSkippedRow(row=r, reason=reason) for r, reason in parsed.skipped],
    )


@router.post("/data-dictionary/bulk", response_model=DataDictionaryBulkResponse)
def bulk_upsert_data_dictionary(
    request: DataDictionaryBulkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Save reviewed rows. Each row is re-matched against the connection's
    current schema; rows naming unknown tables/columns are rejected."""
    tenant_id = current_user.tenant_id
    _get_connection(db, connection_id=request.connection_id, user=current_user)
    schema = _schema_or_422(db, request.connection_id, tenant_id)
    submitted = [
        DictRow(
            e.schema_name.strip(),
            e.table_name.strip(),
            e.column_name.strip(),
            " ".join(e.description.split()),
            e.origin,
        )
        for e in request.entries
    ]
    matched = match_to_schema(submitted, schema)
    counts = upsert_entries(
        db,
        tenant_id=tenant_id,
        connection_id=request.connection_id,
        rows=matched.rows,
        overwrite=request.overwrite,
    )
    counts["rejected"] = len(submitted) - len(matched.rows)
    log_audit(
        db,
        user=current_user,
        action="data_dictionary.bulk_upsert",
        resource_type="connection",
        resource_id=request.connection_id,
        metadata={**counts, "overwrite": request.overwrite},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("Data dictionary changed while saving; please try again") from exc
    schema_cache.invalidate(tenant_id, request.connection_id)
    return DataDictionaryBulkResponse(**counts)


@router.patch(
    "/data-dictionary/{entry_id}", response_model=DataDictionaryEntryResponse
)
def update_data_dictionary_entry(
    entry_id: UUID,
    request: DataDictionaryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = _get_dictionary_entry(db, entry_id=entry_id, tenant_id=current_user.tenant_id)
    row.description = " ".join(request.description.split())
    row.origin = "manual"
    db.commit()
    db.refresh(row)
    schema_cache.invalidate(current_user.tenant_id, row.connection_id)
    return _dictionary_response(row)


@router.delete(
    "/data-dictionary/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_data_dictionary_entry(
    entry_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = _get_dictionary_entry(db, entry_id=entry_id, tenant_id=current_user.tenant_id)
    connection_id = row.connection_id
    db.delete(row)
    db.commit()
    schema_cache.invalidate(current_user.tenant_id, connection_id)


# --- Semantic model import (KPI + relationship sheets from CSV / Excel) -----------------


def _readonly_validator(tenant_id: UUID, connection_id: UUID):
    from app.query_execution import QueryCategory
    from app.tools.customer_db import run_readonly_query

    def validate(sql: str) -> None:
        run_readonly_query(
            str(connection_id), sql, tenant_id=tenant_id, category=QueryCategory.METADATA
        )

    return validate


@router.post("/model-import/preview", response_model=SemanticModelPreviewResponse)
async def preview_semantic_model_import(
    connection_id: UUID,
    generate_missing: bool = True,
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Preview a semantic-model import. Nothing is saved.

    With a file: every sheet is read (sheets with unfamiliar headers are
    mapped by AI) and, with ``generate_missing``, AI drafts whatever the
    file lacks. Without a file: AI drafts the whole model from the schema.
    Every row is checked against this connection; KPI formulas are compiled
    on the database with ``WHERE 1 = 0``, so no data is read.
    """
    tenant_id = current_user.tenant_id
    _get_connection(db, connection_id=connection_id, user=current_user)
    schema = _schema_or_422(db, connection_id, tenant_id)
    content = await file.read(MAX_FILE_BYTES + 1) if file is not None else None
    filename = (file.filename if file is not None else None) or "AI draft"

    def build():
        from app.llm.client import get_llm

        return import_flow.build_preview(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            schema=schema,
            filename=filename,
            content=content,
            generate_missing=generate_missing,
            llm=get_llm(),
            validator=_readonly_validator(tenant_id, connection_id),
        )

    try:
        flow = await run_in_threadpool(build)
    except ImportFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return SemanticModelPreviewResponse(
        filename=filename,
        connection_id=connection_id,
        kpis=[
            SemanticModelKpiPreview(
                name=p.resolved.kpi.name,
                formula=p.resolved.kpi.formula,
                description=p.resolved.kpi.description,
                domain=p.resolved.kpi.domain,
                synonyms=p.resolved.kpi.synonyms,
                action=p.action,
                origin="ai" if p.resolved.kpi.origin == "ai" else "import",
                expression=p.resolved.expression,
                error=p.resolved.error,
                warnings=p.resolved.warnings,
                existing_name=p.existing_name,
                existing_expression=p.existing_expression,
                row=p.resolved.kpi.row,
            )
            for p in flow.kpis
        ],
        relationships=[
            SemanticModelRelationshipPreview(
                from_schema=p.resolved.rel.from_schema,
                from_table=p.resolved.rel.from_table,
                from_column=p.resolved.rel.from_column,
                to_schema=p.resolved.rel.to_schema,
                to_table=p.resolved.rel.to_table,
                to_column=p.resolved.rel.to_column,
                cardinality=p.resolved.rel.cardinality[:40],
                description=p.resolved.rel.description,
                action=p.action,
                origin="inferred" if p.resolved.rel.origin == "inferred" else "import",
                join=(
                    f"{p.resolved.from_table}.{p.resolved.from_column} = "
                    f"{p.resolved.to_table}.{p.resolved.to_column}"
                    if not p.resolved.error
                    else ""
                ),
                error=p.resolved.error,
                warnings=p.resolved.warnings,
                note=p.note,
                row=p.resolved.rel.row,
            )
            for p in flow.relationships
        ],
        rules=[
            SemanticModelRulePreview(
                kind=p.row.kind,
                name=p.row.name,
                category=p.row.category,
                rule=p.row.rule,
                guidance=p.row.guidance,
                terms=p.row.terms,
                origin="ai" if p.row.origin == "ai" else "import",
                action=p.action,
                warnings=p.warnings,
                row=p.row.row,
            )
            for p in flow.rules
        ],
        dictionary=[
            DataDictionaryPreviewRow(
                schema_name=p.row.schema_name,
                table_name=p.row.table_name,
                column_name=p.row.column_name,
                description=p.row.description,
                origin="ai" if p.row.origin == "ai" else "import",
                action=p.action,
                existing_description=p.existing,
            )
            for p in flow.dictionary
        ],
        examples=[
            SemanticModelExamplePreview(
                question=p.row.question,
                tables=p.row.tables,
                domain=p.row.domain,
                notes=p.row.notes,
                origin="ai" if p.row.origin == "ai" else "import",
                action=p.action,
                warnings=p.warnings,
                row=p.row.row,
            )
            for p in flow.examples
        ],
        skipped=[ImportSkippedRow(row=r, reason=reason) for r, reason in flow.skipped],
        notes=flow.notes,
    )


@router.post("/model-import", response_model=SemanticModelSaveResponse)
def save_semantic_model_import(
    request: SemanticModelSaveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Save a reviewed import. KPIs become suggested metrics; joins, rules,
    descriptions and example questions are used right away. Every row is
    re-checked against the current schema."""
    tenant_id = current_user.tenant_id
    connection_id = request.connection_id
    _get_connection(db, connection_id=connection_id, user=current_user)
    schema = _schema_or_422(db, connection_id, tenant_id)
    kpi_counts = model_import.save_kpis(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        kpis=[
            model_import.KpiRow(
                name=k.name.strip(),
                formula=k.formula.strip(),
                description=" ".join(k.description.split()),
                domain=k.domain.strip(),
                synonyms=[s.strip() for s in k.synonyms if s.strip()],
            )
            for k in request.kpis
        ],
        schema=schema,
    )
    rel_counts = model_import.save_relationships(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        relationships=[
            model_import.RelationshipRow(
                from_table=r.from_table.strip(),
                from_column=r.from_column.strip(),
                to_table=r.to_table.strip(),
                to_column=r.to_column.strip(),
                from_schema=r.from_schema.strip(),
                to_schema=r.to_schema.strip(),
                cardinality=r.cardinality.strip(),
                description=r.description,
            )
            for r in request.relationships
        ],
        schema=schema,
    )
    rule_counts = semantic_rules.save_rules(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        rows=[
            semantic_rules.RuleRow(
                kind=r.kind,
                name=r.name.strip(),
                category=r.category.strip(),
                rule=" ".join(r.rule.split()),
                guidance=" ".join(r.guidance.split()),
                terms=semantic_rules.split_terms(";".join(r.terms)),
                origin=r.origin,
            )
            for r in request.rules
        ],
    )
    submitted = [
        DictRow(
            e.schema_name.strip(),
            e.table_name.strip(),
            e.column_name.strip(),
            " ".join(e.description.split()),
            e.origin,
        )
        for e in request.dictionary
    ]
    matched = match_to_schema(submitted, schema)
    dict_counts = upsert_entries(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        rows=matched.rows,
        overwrite=request.overwrite_dictionary,
    )
    dict_counts["rejected"] = len(submitted) - len(matched.rows)
    example_counts = semantic_examples.save_examples(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        rows=[
            semantic_examples.ExampleRow(
                question=" ".join(e.question.split()),
                tables=[t.strip() for t in e.tables if t.strip()],
                domain=e.domain.strip(),
                notes=e.notes.strip(),
                origin=e.origin,
            )
            for e in request.examples
        ],
        schema=schema,
    )
    counts = {
        "kpis": kpi_counts,
        "relationships": rel_counts,
        "rules": rule_counts,
        "dictionary": dict_counts,
        "examples": example_counts,
    }
    log_audit(
        db,
        user=current_user,
        action="semantic_model.import",
        resource_type="connection",
        resource_id=connection_id,
        metadata=counts,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("The semantic layer changed while saving; please try again") from exc
    _invalidate(tenant_id, connection_id)
    schema_cache.invalidate(tenant_id, connection_id)
    return SemanticModelSaveResponse(**counts)


def _rule_response(row: SemanticRule) -> SemanticRuleResponse:
    return SemanticRuleResponse(
        id=row.id, connection_id=row.connection_id, **semantic_rules.rule_dict(row)
    )


@router.get("/rules", response_model=list[SemanticRuleResponse])
def list_semantic_rules(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_connection(db, connection_id=connection_id, user=current_user)
    return [
        _rule_response(r)
        for r in semantic_rules.load_rules(db, current_user.tenant_id, connection_id)
    ]


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_semantic_rule(
    rule_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = db.scalar(
        select(SemanticRule).where(
            SemanticRule.id == rule_id, SemanticRule.tenant_id == current_user.tenant_id
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    connection_id = row.connection_id
    db.delete(row)
    db.commit()
    _invalidate(current_user.tenant_id, connection_id)


# --- Example questions and evaluation ------------------------------------------------------


def _example_response(row: SemanticExample) -> SemanticExampleResponse:
    return SemanticExampleResponse(
        id=row.id,
        connection_id=row.connection_id,
        question=row.question,
        tables=semantic_examples.example_tables(row),
        domain=row.domain,
        notes=row.notes,
        origin="ai" if row.origin == "ai" else "import",
    )


@router.get("/examples", response_model=list[SemanticExampleResponse])
def list_semantic_examples(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_connection(db, connection_id=connection_id, user=current_user)
    return [
        _example_response(r)
        for r in semantic_examples.load_examples(db, current_user.tenant_id, connection_id)
    ]


@router.delete("/examples/{example_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_semantic_example(
    example_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    row = db.scalar(
        select(SemanticExample).where(
            SemanticExample.id == example_id,
            SemanticExample.tenant_id == current_user.tenant_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Example not found")
    db.delete(row)
    db.commit()


def _example_runner(
    tenant_id: UUID, connection_id: UUID, db_type: str, schema: dict, catalog: dict
):
    """(answer, execute) callables that ask the assistant's SQL writer a
    question and run its query read-only — the same schema-grounded tier
    chat uses, with this connection's confirmed metrics and rules."""
    from app.agent.workflow import ensure_valid_sql, generate_schema_grounded_sql
    from app.llm.client import get_llm
    from app.query_execution import QueryCategory
    from app.tools.customer_db import run_readonly_query

    llm = get_llm()

    def answer(question: str) -> semantic_examples.Answer:
        sql, no_match = generate_schema_grounded_sql(
            llm=llm, db_type=db_type, schema=schema, semantic_context=catalog, question=question
        )
        if no_match:
            return semantic_examples.Answer(None, "the assistant found no matching data")
        safe, error, fallback = ensure_valid_sql(
            sql,
            schema=schema,
            db_type=db_type,
            semantic_context=catalog,
            question=question,
            llm=llm,
        )
        return semantic_examples.Answer(safe, error, bool(fallback))

    def execute(sql: str) -> list[dict]:
        return run_readonly_query(
            str(connection_id), sql, tenant_id=tenant_id, category=QueryCategory.ANALYTICS
        )

    return answer, execute


@router.post("/examples/evaluate", response_model=ExampleEvaluationResponse)
async def evaluate_semantic_examples(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Ask the assistant each saved example question (up to 20), run its
    query read-only and check it used the expected tables. Returns pass /
    fail per question; the SQL itself is not returned."""
    tenant_id = current_user.tenant_id
    connection = _get_connection(db, connection_id=connection_id, user=current_user)
    schema = _schema_or_422(db, connection_id, tenant_id)
    examples = semantic_examples.load_examples(db, tenant_id, connection_id)
    if not examples:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add example questions first",
        )

    from app.services.semantic_service import load_connection_semantic_catalog

    catalog = load_connection_semantic_catalog(db, tenant_id, connection_id)
    db_type = str(connection.db_type)

    def run():
        answer, execute = _example_runner(tenant_id, connection_id, db_type, schema, catalog)
        return semantic_examples.evaluate_examples(
            examples, schema, answer=answer, execute=execute
        )

    results = await run_in_threadpool(run)
    passed = sum(1 for r in results if r.status == "pass")
    log_audit(
        db,
        user=current_user,
        action="semantic_model.evaluate",
        resource_type="connection",
        resource_id=connection_id,
        metadata={"total": len(results), "passed": passed},
    )
    db.commit()
    return ExampleEvaluationResponse(
        total=len(results),
        passed=passed,
        results=[
            ExampleEvaluationRow(
                question=r.question,
                status=r.status,
                expected_tables=r.expected_tables,
                tables_used=r.tables_used,
                missing_tables=r.missing_tables,
                row_count=r.row_count,
                error=r.error,
            )
            for r in results
        ],
    )
