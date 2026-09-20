"""HTTP boundary for read-only external dataset sources."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .connections_schemas import (
    ConnectionDeleteResponse,
    ConnectionListResponse,
    ConnectionResponse,
    ConnectionTestResponse,
    SourceDatasetResponse,
    SourceObjectsResponse,
    SourcePreviewResponse,
    SourceSchemasResponse,
)
from .connections_service import (
    ConnectionOperationError,
    acquire_snapshot,
    connection_dto,
    current_version,
    owned_connection,
    require_password_for_endpoint_change,
    save_connection,
    saved_source,
    test_saved_connection,
)
from .credential_store import secret_store
from .dataset_sources import ConnectionSettings, SourceError, source_registry
from .db import get_db, iso, utcnow
from .models import Dataset, DatasetSourceBinding, ExternalConnection, User, uid
from .services import audit, dataset_dto, version_dto

router = APIRouter(prefix="/api/v1", tags=["Conexiones"])


def current_user(request: Request) -> User:
    return request.state.user


class ConnectionInput(BaseModel):
    # Never normalize credentials or quoted database identifiers.
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "host", check_fields=False, mode="before")
    @classmethod
    def trim_display_fields(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("options", check_fields=False)
    @classmethod
    def safe_options(cls, value):
        # Never accept opaque connection strings, credentials or driver callbacks.
        if value is not None and set(value) - {"connect_timeout", "query_timeout", "sslmode", "encryption"}:
            raise ValueError("Parámetros de conexión no admitidos.")
        return value


class ConnectionBody(ConnectionInput):
    name: str = Field(min_length=1, max_length=160)
    source_type: Literal["POSTGRESQL", "SQLSERVER"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = Field(default=None, max_length=1024)
    options: dict = Field(default_factory=dict)


class ConnectionTestBody(ConnectionBody):
    connection_id: str | None = None


class ConnectionPatch(ConnectionInput):
    version: int = Field(ge=1, validation_alias=AliasChoices("version", "expected_version"))
    name: str | None = Field(default=None, min_length=1, max_length=160)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, min_length=1, max_length=128)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: SecretStr | None = Field(default=None, max_length=1024)
    options: dict | None = None
    enabled: bool | None = None


class SourceDatasetBody(ConnectionInput):
    # Database identifiers are observed values: quoted names may contain outer spaces.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    name: str = Field(min_length=1, max_length=160)
    domain: str = Field(default="Operaciones", min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    schema_name: str = Field(min_length=1, max_length=128)
    object_name: str = Field(min_length=1, max_length=128)
    column_overrides: dict = Field(default_factory=dict)


@router.get("/connections", response_model=ConnectionListResponse)
def list_connections(db: Session = Depends(get_db), user: User = Depends(current_user)):
    connections = db.scalars(select(ExternalConnection).where(
        ExternalConnection.organization_id == user.organization_id, ExternalConnection.deleted.is_(False),
    ).order_by(ExternalConnection.created_at.desc())).all()
    return {"items": [connection_dto(db, item) for item in connections], "total": len(connections)}


@router.post("/connections/test", response_model=ConnectionTestResponse)
def test_draft(body: ConnectionTestBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    password = body.password.get_secret_value() if body.password else ""
    if body.connection_id:
        connection = owned_connection(db, body.connection_id, user)
        if body.source_type != connection.source_type:
            raise ConnectionOperationError(422, "SOURCE_TYPE_IMMUTABLE", "El tipo de una conexión existente no puede cambiar.")
        if not password:
            version = current_version(db, connection)
            require_password_for_endpoint_change(
                connection,
                version,
                host=body.host,
                port=body.port,
                database=body.database,
                username=body.username,
                options=body.options,
            )
            password = secret_store.get(user.organization_id, version.secret_reference)
    if not password:
        raise ConnectionOperationError(422, "PASSWORD_REQUIRED", "Indica la contraseña de la conexión.")
    settings = ConnectionSettings(source_type=body.source_type, host=body.host, port=body.port,
                                  database=body.database, username=body.username, password=password, options=body.options)
    subject_type = "connection" if body.connection_id else "connection_test"
    subject_id = body.connection_id or uid()
    metadata = {"source_type": body.source_type, "draft": True}
    try:
        source_registry.create(settings).test()
    except SourceError as error:
        audit(db, "CONNECTION_TESTED", subject_type, subject_id, "Prueba de borrador fallida", user.name,
              user.organization_id, {**metadata, "status": "FAILED", "error_code": error.code})
        db.commit()
        raise
    audit(db, "CONNECTION_TESTED", subject_type, subject_id, "Prueba de borrador exitosa", user.name,
          user.organization_id, {**metadata, "status": "SUCCESS"})
    db.commit()
    return {"status": "SUCCESS", "message": "Conexión verificada correctamente.", "tested_at": iso(utcnow())}


@router.post("/connections", status_code=201, response_model=ConnectionResponse)
def create_connection(body: ConnectionBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return connection_dto(db, save_connection(db, user, body.model_dump()))


@router.get("/connections/{connection_id}", response_model=ConnectionResponse)
def connection_detail(connection_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return connection_dto(db, owned_connection(db, connection_id, user))


@router.patch("/connections/{connection_id}", response_model=ConnectionResponse)
def update_connection(connection_id: str, body: ConnectionPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    connection = owned_connection(db, connection_id, user, lock=True)
    updated = save_connection(db, user, body.model_dump(exclude_none=True), connection=connection)
    return connection_dto(db, updated)


@router.delete("/connections/{connection_id}", response_model=ConnectionDeleteResponse)
def delete_connection(connection_id: str, version: int = Query(ge=1),
                      db: Session = Depends(get_db), user: User = Depends(current_user)):
    connection = owned_connection(db, connection_id, user, lock=True)
    if version != connection.version:
        raise ConnectionOperationError(409, "VERSION_CONFLICT", "La conexión fue modificada. Actualiza antes de eliminar.")
    connection.deleted, connection.enabled, connection.updated_at = True, False, utcnow()
    audit(db, "CONNECTION_DELETED", "connection", connection.id, "Conexión retirada; historial y snapshots conservados",
          user.name, user.organization_id, {"version": connection.version})
    db.commit()
    return {"ok": True}


@router.post("/connections/{connection_id}/test", response_model=ConnectionTestResponse)
def test_connection(connection_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return test_saved_connection(db, owned_connection(db, connection_id, user, lock=True), user)


@router.get("/connections/{connection_id}/schemas", response_model=SourceSchemasResponse)
def schemas(connection_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    source, _ = saved_source(db, owned_connection(db, connection_id, user))
    items = source.schemas()
    return {"items": items, "total": len(items)}


@router.get("/connections/{connection_id}/objects", response_model=SourceObjectsResponse)
def objects(connection_id: str, schema_name: str = Query(min_length=1, max_length=128),
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    source, _ = saved_source(db, owned_connection(db, connection_id, user))
    items = source.objects(schema_name)
    return {"items": items, "total": len(items)}


@router.get("/connections/{connection_id}/preview", response_model=SourcePreviewResponse)
def preview(connection_id: str, schema_name: str = Query(min_length=1, max_length=128),
            object_name: str = Query(min_length=1, max_length=128), limit: int = Query(default=20, ge=1, le=100),
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    connection = owned_connection(db, connection_id, user)
    source, version = saved_source(db, connection, schema_name=schema_name, object_name=object_name)
    result = source.read({"limit": limit}, inspect=True)
    audit(db, "CONNECTION_PREVIEWED", "connection", connection.id, "Vista previa limitada de una fuente externa",
          user.name, user.organization_id, {"connection_version": version.version,
                                           "schema_name": schema_name, "object_name": object_name,
                                           "sampled_rows": result.frame.height})
    db.commit()
    return {"columns": result.metadata["source_native_schema"], "rows": result.frame.to_dicts(),
            "sampled_rows": result.frame.height}


@router.post("/connections/{connection_id}/datasets", status_code=201, response_model=SourceDatasetResponse)
def register_source_dataset(connection_id: str, body: SourceDatasetBody,
                            db: Session = Depends(get_db), user: User = Depends(current_user)):
    connection = owned_connection(db, connection_id, user, lock=True)
    source, _ = saved_source(db, connection)
    selected = next((item for item in source.objects(body.schema_name) if item["name"] == body.object_name), None)
    if selected is None:
        raise ConnectionOperationError(404, "SOURCE_OBJECT_NOT_FOUND", "No se encontró la tabla o vista accesible seleccionada.")
    dataset = Dataset(name=body.name, description=body.description, domain=body.domain,
                      organization_id=user.organization_id, owner=user.name)
    db.add(dataset)
    db.flush()
    binding = DatasetSourceBinding(organization_id=user.organization_id, dataset_id=dataset.id,
                                   connection_id=connection.id, schema_name=body.schema_name,
                                   object_name=body.object_name, object_kind=selected["kind"],
                                   column_overrides=body.column_overrides)
    db.add(binding)
    version = acquire_snapshot(db, user, connection, binding, dataset)
    db.commit()
    return {"dataset": dataset_dto(db, dataset), "version": version_dto(version, db)}


@router.post("/datasets/{dataset_id}/refresh-source", status_code=201)
def refresh_source(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = db.scalar(select(Dataset).where(Dataset.id == dataset_id,
                        Dataset.organization_id == user.organization_id).with_for_update())
    if dataset is None:
        raise ConnectionOperationError(404, "NOT_FOUND", "No se encontró el dataset solicitado.")
    binding = db.scalar(select(DatasetSourceBinding).where(DatasetSourceBinding.dataset_id == dataset.id,
                         DatasetSourceBinding.organization_id == user.organization_id))
    if binding is None:
        raise ConnectionOperationError(409, "DATASET_HAS_NO_SOURCE", "El dataset no tiene una fuente externa registrada.")
    connection = owned_connection(db, binding.connection_id, user, lock=True)
    version = acquire_snapshot(db, user, connection, binding, dataset)
    db.commit()
    return version_dto(version, db)
