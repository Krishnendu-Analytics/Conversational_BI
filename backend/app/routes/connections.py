import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import (
    accessible_connection,
    accessible_connections,
    filter_schema,
    scope_for_user,
)
from app.audit import log_audit
from app.database import get_db
from app.encryption import CredentialEncryption
from app.models import Connection, User
from app.redis_client import schema_cache
from app.services.connection_service import delete_connection_record
from app.routes.auth import get_current_user, require_role
from app.schemas import (
    ConnectionCreate,
    ConnectionResponse,
    ConnectionTestResponse,
    ConnectionUpdate,
    SchemaCatalogResponse,
    SchemaDiscoverRequest,
    SchemaDiscoverResponse,
    SchemaDiscoveryStatusResponse,
    SchemaListResponse,
)
from app.services.discovery_jobs import (
    enqueue_schema_discovery,
    get_latest_discovery_status,
    is_sync_mode,
    run_schema_discovery_sync,
    set_sync_mode,
)
from app.services.schema_discovery_service import (
    get_stored_schema_for_tenant,
    run_schema_discovery,
)
from app.tools.customer_db import (
    ConnectionTestResult,
    ProviderFactory,
    UnsupportedDatabaseError,
    list_connection_schemas,
)

router = APIRouter(
    prefix="/api/v1/connections",
    tags=["connections"],
)

encryptor = CredentialEncryption()


def _run_connection_test(db_type: str, connection_url: str) -> ConnectionTestResult:
    """Test credentials and reachability. Raises HTTPException on failure."""
    provider = ProviderFactory.create(db_type, connection_url)
    try:
        result = provider.test_connection()
    except UnsupportedDatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message,
        )

    # Privilege inspection is diagnostic only. Write-capable customer DB
    # accounts are allowed; mutation SQL is blocked at execution time.
    if result.read_only_verified is False:
        return ConnectionTestResult(
            ok=True,
            message=(
                f"{result.message} "
                "(database account has write privileges; "
                "the platform still executes SELECT-only queries)"
            ),
            read_only_verified=False,
        )

    if result.read_only_verified is None:
        return ConnectionTestResult(
            ok=True,
            message=(
                f"{result.message} "
                "(read-only privileges could not be verified; queries remain SELECT-only)"
            ),
            read_only_verified=None,
        )

    return result


def _get_tenant_connection(
    db: Session,
    *,
    connection_id: UUID,
    user: User,
) -> Connection:
    """Tenant connection the user's departments grant (admins: any); else 404."""
    conn = accessible_connection(db, connection_id=connection_id, user=user)
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connection not found",
        )
    return conn


def _to_response(conn: Connection) -> ConnectionResponse:
    return ConnectionResponse(
        id=conn.id,
        name=conn.name,
        db_type=conn.db_type,
        tenant_id=conn.tenant_id,
        last_discover_status=conn.last_discover_status,
        discover_scope=conn.discover_scope,
    )


@router.post(
    "",
    response_model=ConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    request: ConnectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """
    Test the customer DB credentials, then save only if the test succeeds.
    The connection URL (host, port, database, username, password) is Fernet-encrypted.
    """
    _run_connection_test(request.db_type.value, request.connection_url)

    connection = Connection(
        tenant_id=current_user.tenant_id,
        name=request.name,
        db_type=request.db_type.value,
        encrypted_url=encryptor.encrypt(request.connection_url),
    )

    db.add(connection)
    log_audit(
        db,
        user=current_user,
        action="connection.create",
        resource_type="connection",
        resource_id=connection.id,
        metadata={"name": connection.name, "db_type": connection.db_type},
    )
    db.commit()
    db.refresh(connection)

    enqueue_schema_discovery(
        db,
        connection_id=connection.id,
        tenant_id=current_user.tenant_id,
        scope=connection.discover_scope or "default",
        profile=connection.profile_on_discover,
        include_views=connection.include_views,
    )
    db.refresh(connection)

    return _to_response(connection)


@router.get(
    "",
    response_model=list[ConnectionResponse],
)
def list_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List all database connections for the current tenant.
    """

    return [_to_response(conn) for conn in accessible_connections(db, current_user)]


@router.patch(
    "/{connection_id}",
    response_model=ConnectionResponse,
)
def update_connection(
    connection_id: UUID,
    request: ConnectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """
    Update name and/or credentials. New credentials are tested before save.
    """
    conn = _get_tenant_connection(
        db,
        connection_id=connection_id,
        user=current_user,
    )

    if request.name is not None:
        conn.name = request.name

    if request.connection_url is not None and request.db_type is not None:
        _run_connection_test(request.db_type.value, request.connection_url)
        conn.db_type = request.db_type.value
        conn.encrypted_url = encryptor.encrypt(request.connection_url)
        # Credentials changed — drop Redis schema cache for this connection.
        schema_cache.invalidate(conn.tenant_id, conn.id)

    if request.discover_scope is not None:
        conn.discover_scope = request.discover_scope
    if request.discover_schemas is not None:
        conn.discover_schemas_json = json.dumps(request.discover_schemas)

    db.commit()
    db.refresh(conn)
    return _to_response(conn)


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_connection(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Remove a tenant-owned connection and rows that still reference it."""
    conn = _get_tenant_connection(
        db,
        connection_id=connection_id,
        user=current_user,
    )

    delete_connection_record(db, conn)

    log_audit(
        db,
        user=current_user,
        action="connection.delete",
        resource_type="connection",
        resource_id=connection_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connection is still in use and could not be deleted.",
        ) from exc


@router.post(
    "/{connection_id}/test",
    response_model=ConnectionTestResponse,
)
def test_connection(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Validate tenant-owned connection credentials + reachability.
    """
    conn = _get_tenant_connection(
        db,
        connection_id=connection_id,
        user=current_user,
    )

    decrypted_url = encryptor.decrypt(conn.encrypted_url)
    provider = ProviderFactory.create(conn.db_type, decrypted_url)
    try:
        result = provider.test_connection()
    except UnsupportedDatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if not result.ok:
        return ConnectionTestResponse(ok=False, message=result.message)

    message = result.message or "Connection successful"
    if result.read_only_verified is False:
        message = (
            f"{message} "
            "(database account has write privileges; "
            "the platform still executes SELECT-only queries)"
        )
    elif result.read_only_verified is None:
        message = (
            f"{message} "
            "(read-only privileges could not be verified; queries remain SELECT-only)"
        )
    return ConnectionTestResponse(ok=True, message=message)


@router.post(
    "/{connection_id}/discover",
    response_model=SchemaDiscoverResponse,
)
def discover_connection_schema(
    connection_id: UUID,
    body: SchemaDiscoverRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """
    Discover tables/columns/FKs from the customer database
    and cache them in schema_tables + Redis.

    Body (optional):
      scope: default | all | selected
      schemas: list of schema names when scope=selected
    """

    conn = _get_tenant_connection(
        db,
        connection_id=connection_id,
        user=current_user,
    )
    request = body or SchemaDiscoverRequest()
    scope = request.scope or "default"

    # Remember this choice so future discovery runs (e.g. on re-connect,
    # or triggered elsewhere) use the same scope by default instead of
    # silently falling back to a single schema.
    conn.discover_scope = scope
    conn.discover_schemas_json = json.dumps(request.schemas or [])
    db.commit()

    try:
        if scope == "all" and not is_sync_mode():
            run = enqueue_schema_discovery(
                db,
                connection_id=conn.id,
                tenant_id=current_user.tenant_id,
                scope=scope,
                schemas=request.schemas,
                profile=conn.profile_on_discover,
                include_views=conn.include_views,
            )
            return {
                "connection_id": conn.id,
                "tables": [],
                "schemas_used": [],
                "status": run.status,
                "run_id": str(run.id),
            }

        result = run_schema_discovery(
            db,
            connection_id=conn.id,
            tenant_id=current_user.tenant_id,
            scope=scope,
            schemas=request.schemas,
            profile=conn.profile_on_discover,
            include_views=conn.include_views,
        )
        return {
            "connection_id": result.connection_id,
            "tables": result.tables,
            "schemas_used": result.schemas_used,
            "status": result.status,
            "warnings": result.warnings,
        }
    except UnsupportedDatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        detail = str(exc)
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in detail.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001 — surface discover failures cleanly
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Schema discovery failed: {type(exc).__name__}",
        ) from exc


@router.get(
    "/{connection_id}/schemas",
    response_model=SchemaCatalogResponse,
)
def list_schemas_for_connection(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List schemas/databases available for discovery on this connection."""
    conn = _get_tenant_connection(
        db,
        connection_id=connection_id,
        user=current_user,
    )
    try:
        catalog = list_connection_schemas(db, conn.id)
    except UnsupportedDatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not list schemas: {type(exc).__name__}",
        ) from exc
    allowed = scope_for_user(db, current_user).allowed_schemas(conn.id)
    if allowed is not None:
        catalog["schemas"] = [s for s in catalog["schemas"] if s.lower() in allowed]
    return catalog


@router.get(
    "/{connection_id}/schema",
    response_model=SchemaListResponse,
)
def get_connection_schema(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return cached schema for a connection (Redis first, then schema_tables).
    Run POST .../discover first if nothing has been discovered yet.
    """
    conn = _get_tenant_connection(
        db,
        connection_id=connection_id,
        user=current_user,
    )

    allowed = scope_for_user(db, current_user).allowed_schemas(conn.id)
    cached = schema_cache.get(conn.tenant_id, conn.id)
    if cached is not None:
        return filter_schema(cached, allowed)

    try:
        stored = get_stored_schema_for_tenant(
            db, conn.id, current_user.tenant_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    if stored["tables"]:
        schema_cache.set(conn.tenant_id, conn.id, stored)

    return filter_schema(stored, allowed)


@router.get(
    "/{connection_id}/discover/status",
    response_model=SchemaDiscoveryStatusResponse,
)
def get_discovery_status(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the latest schema discovery run for this connection."""
    conn = _get_tenant_connection(
        db,
        connection_id=connection_id,
        user=current_user,
    )
    run = get_latest_discovery_status(
        db, connection_id=conn.id, tenant_id=current_user.tenant_id
    )
    if run is None:
        return SchemaDiscoveryStatusResponse(
            connection_id=conn.id,
            status=conn.last_discover_status or "none",
        )
    import json

    warnings = []
    try:
        warnings = json.loads(run.warnings_json or "[]")
    except json.JSONDecodeError:
        pass
    return SchemaDiscoveryStatusResponse(
        connection_id=conn.id,
        run_id=run.id,
        status=run.status,
        scope=run.scope,
        warnings=warnings if isinstance(warnings, list) else [],
        tables_discovered=run.tables_discovered,
        error_message=run.error_message,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )
