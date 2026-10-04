"""Additive async HTTP contract; historical synchronous responses remain intact."""

import hashlib
import json
import os
import shutil
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .acquisition import (
    AcquisitionOperationError,
    _idempotent,
    _request_hash,
    acquisition_dto,
    cancel_acquisition,
    owned_acquisition,
    register_source,
    register_upload,
    upload_dto,
)
from .acquisition_config import AcquisitionLimits
from .acquisition_errors import AcquisitionReadError, processing_diagnostic
from .acquisition_models import AcquisitionRun, AcquisitionUpload
from .artifactstore import artifact_store, storage_provider
from .batch_readers import inspect_file
from .connections_service import owned_connection, saved_source
from .dataset_readers import dataset_reader_registry
from .db import get_db, utcnow
from .models import Dataset, DatasetSourceBinding, User, uid
from .permissions import effective_permissions
from .processing import ProcessingError
from .services import dataset_dto

router = APIRouter(prefix="/api/v1", tags=["Adquisición"])


def current_user(request: Request) -> User:
    return request.state.user


class AcquisitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: str = Field(min_length=1, max_length=64)
    reader_options: dict = Field(default_factory=dict)
    column_overrides: dict = Field(default_factory=dict)


class SourceAcquisitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    name: str = Field(min_length=1, max_length=160)
    domain: str = Field(default="Operaciones", min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    schema_name: str = Field(min_length=1, max_length=128)
    object_name: str = Field(min_length=1, max_length=128)
    column_overrides: dict = Field(default_factory=dict)

    @field_validator("name", "domain")
    @classmethod
    def business_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("El nombre y el área no pueden quedar vacíos.")
        return value.strip()


class AcquisitionResponse(BaseModel):
    id: str
    dataset_id: str
    source_type: Literal["UPLOAD", "POSTGRESQL", "SQLSERVER"]
    filename: str
    status: Literal["QUEUED", "RUNNING", "SUCCESS", "FAILED", "CANCELLED"]
    stage: str
    attempt_id: str | None
    attempts: int
    initiated_by_id: str
    initiated_by: str
    processed_rows: int
    processed_bytes: int = Field(description="Bytes UTF-8 observados, no tráfico de red.")
    total_rows: int | None
    total_bytes: int | None = Field(description="Bytes del archivo recibido; desconocido en SQL.")
    duration_seconds: float | None
    cancel_requested: bool
    output_version_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    source_snapshot: dict
    reader_options: dict
    column_overrides: dict
    effective_limits: dict[str, int]
    route_limits: dict | None
    received_bytes: int | None
    materialized_rows: int
    materialized_bytes: int
    published_rows: int | None
    error: dict | None


class AcquisitionListResponse(BaseModel):
    items: list[AcquisitionResponse]
    total: int
    limit: int
    offset: int


def _dataset(db: Session, user: User, identifier: str) -> Dataset:
    dataset = db.scalar(select(Dataset).where(Dataset.id == identifier, Dataset.organization_id == user.organization_id))
    if dataset is None:
        raise AcquisitionOperationError(404, "NOT_FOUND", "No se encontró el dataset solicitado.")
    return dataset


def _upload(db: Session, user: User, identifier: str) -> AcquisitionUpload:
    upload = db.scalar(select(AcquisitionUpload).where(AcquisitionUpload.id == identifier,
        AcquisitionUpload.organization_id == user.organization_id, AcquisitionUpload.user_id == user.id))
    if upload is None or upload.status != "RECEIVED" or upload.expires_at.replace(tzinfo=UTC) <= utcnow():
        raise AcquisitionOperationError(404, "UPLOAD_NOT_FOUND", "No se encontró una transferencia vigente de este usuario.")
    return upload


@router.get("/acquisitions/limits")
def acquisition_limits(source_format: Literal["CSV", "TXT", "JSON", "JSON_LINES", "PARQUET", "XLSX", "POSTGRESQL", "SQLSERVER"] = Query("CSV", alias="format"),
                       route: Literal["ASYNC_ACQUISITION", "LEGACY_UPLOAD"] = "ASYNC_ACQUISITION"):
    return AcquisitionLimits.configured().describe(source_format, route=route)


def _reception_format(filename: str, prefix: bytes = b"", *, complete: bool = False) -> str:
    """Classify only a bounded prefix; .json alone also admits NDJSON.

    An array or a complete non-NDJSON line identifies nonlinear JSON. An
    incomplete object remains unknown until inspection rather than imposing
    its smaller limit on a valid streaming source.
    """
    extension = Path(filename).suffix.lower()
    if prefix.startswith(b"PK"):
        return "XLSX"
    if prefix.startswith(b"PAR1"):
        return "PARQUET"
    # The bounded prefix may end inside a UTF-8 code point. Replacement is
    # used only for classification, never for materialized source values.
    sample = prefix.decode("utf-8-sig", errors="replace").lstrip()
    if sample.startswith("["):
        return "JSON"
    if extension in {".jsonl", ".ndjson"}:
        return "JSON_LINES"
    if sample.startswith("{"):
        found = 0
        for line in sample.splitlines(keepends=True)[:10]:
            if not line.strip():
                continue
            if not line.endswith(("\n", "\r")) and not complete:
                break
            try:
                value = json.loads(line, parse_int=Decimal, parse_float=Decimal)
            except ValueError:
                return "JSON"
            if not isinstance(value, dict):
                return "JSON"
            found += 1
            if found == 2:
                return "JSON_LINES"
        return "JSON" if complete else "UNKNOWN"
    return next((item["format"] for item in dataset_reader_registry.formats
                 if extension in item["extensions"] and item["format"] != "JSON"), "UNKNOWN")


def _receive_bound(limits: AcquisitionLimits, source_format: str, observed: int) -> None:
    maximum = limits.describe(source_format)["limits"]["compressed_bytes"]["max"]
    if observed > maximum:
        raise AcquisitionOperationError(413, "UPLOAD_TOO_LARGE", f"El archivo supera el límite efectivo de {maximum:,} bytes de adquisición.",
            {"limit": "compressed_bytes", "maximum": maximum, "observed": observed})


@router.post("/datasets/uploads/stage", status_code=201)
async def stage_file(request: Request, filename: str = Query(min_length=1, max_length=240),
                     db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Receive raw file bytes directly into bounded private staging.

    Unlike multipart/form parsing, the application applies size/disk guards while
    ASGI receives the body. Incomplete transfers are cleaned and never registered.
    Navigating after this response and Acquisition registration keeps the job.
    """
    limits = AcquisitionLimits.configured()
    safe_name = Path(filename.replace("\\", "/")).name
    if not safe_name or any(ord(character) < 32 for character in safe_name):
        raise AcquisitionOperationError(422, "INVALID_FILENAME", "El nombre del archivo no es válido.")
    content_length = request.headers.get("content-length")
    announced = None
    if content_length is not None:
        try:
            announced = int(content_length)
        except ValueError:
            raise AcquisitionOperationError(422, "INVALID_UPLOAD_LENGTH", "El tamaño de la transferencia no es válido.") from None
        if announced < 0:
            raise AcquisitionOperationError(422, "INVALID_UPLOAD_LENGTH", "El tamaño de la transferencia no es válido.")
        _receive_bound(limits, _reception_format(safe_name), announced)
    stream = request.stream()
    first_chunk = await anext(stream)
    prefix = first_chunk[:4096]
    source_format = _reception_format(safe_name, prefix, complete=announced == len(first_chunk) and len(first_chunk) <= 4096)
    _receive_bound(limits, source_format, max(len(first_chunk), announced or 0))
    suffix = Path(safe_name).suffix.lower()
    if not 1 <= len(suffix) <= 11 or not suffix[1:].isalnum():
        suffix = ".bin"
    path, complete = storage_provider.temporary_path(suffix), False
    try:
        total = 0
        digest = hashlib.sha256()
        with path.open("xb") as target:
            async def chunks():
                yield first_chunk
                async for subsequent in stream:
                    yield subsequent
            first = True
            async for chunk in chunks():
                total += len(chunk)
                if not first and len(prefix) < 4096:
                    prefix += chunk[:4096 - len(prefix)]
                first = False
                detected = _reception_format(safe_name, prefix)
                if detected != "UNKNOWN":
                    source_format = detected
                _receive_bound(limits, source_format, max(total, announced or 0))
                if shutil.disk_usage(path.parent).free - len(chunk) < limits.min_free_bytes:
                    raise AcquisitionOperationError(409, "RESOURCE_DISK_INSUFFICIENT", "No queda la reserva de disco necesaria para recibir el archivo.")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if total == 0:
            raise AcquisitionOperationError(422, "UPLOAD_EMPTY", "El archivo recibido está vacío.")
        _receive_bound(limits, _reception_format(safe_name, prefix, complete=total <= 4096), total)
        inspection = await run_in_threadpool(inspect_file, path, safe_name, limits=limits)
        upload = AcquisitionUpload(id=uid(), organization_id=user.organization_id, user_id=user.id,
            filename=safe_name, path=str(path), sha256=digest.hexdigest(), size_bytes=total,
            source_format=inspection["format"], status="RECEIVED",
            expires_at=utcnow() + timedelta(seconds=limits.upload_ttl_seconds))
        db.add(upload)
        db.commit()
        complete = True
        return {"upload": upload_dto(upload), "inspection": inspection}
    finally:
        if not complete:
            path.unlink(missing_ok=True)


@router.get("/datasets/uploads/{upload_id}/inspect")
def inspect_staged(upload_id: str, reader_options: str = "{}", db: Session = Depends(get_db), user: User = Depends(current_user)):
    upload = _upload(db, user, upload_id)
    try:
        options = json.loads(reader_options)
        if not isinstance(options, dict):
            raise TypeError()
    except (ValueError, TypeError):
        raise AcquisitionOperationError(422, "INVALID_READER_OPTIONS", "Las opciones de lectura deben ser un objeto JSON.") from None
    path = artifact_store.materialize_reference(upload.path, expected_sha256=upload.sha256, expected_size=upload.size_bytes)
    return inspect_file(path, upload.filename, options)


@router.post("/datasets/{dataset_id}/acquisitions", status_code=202, response_model=AcquisitionResponse)
def acquire_file(dataset_id: str, body: AcquisitionBody, idempotency_key: str | None = Header(default=None),
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        run = register_upload(db, user, _dataset(db, user, dataset_id), **body.model_dump(), idempotency_key=idempotency_key)
    except ProcessingError as exc:
        diagnostic = processing_diagnostic(exc)
        raise AcquisitionReadError(diagnostic.code, diagnostic.message, diagnostic.details) from None
    db.commit()
    return acquisition_dto(run)


@router.post("/connections/{connection_id}/acquisitions", status_code=202)
def acquire_source(connection_id: str, body: SourceAcquisitionBody, idempotency_key: str | None = Header(default=None),
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    if "datasets:write" not in effective_permissions(db, user):
        raise AcquisitionOperationError(403, "FORBIDDEN", "Se requiere permiso para registrar el dataset de la fuente.")
    request_identity = {"operation": "REGISTER_SOURCE", "connection_id": connection_id, **body.model_dump()}
    existing = _idempotent(db, user, idempotency_key, _request_hash(request_identity))
    if existing:
        return {"dataset": dataset_dto(db, _dataset(db, user, existing.dataset_id)), "acquisition": acquisition_dto(existing)}
    connection = owned_connection(db, connection_id, user, lock=True)
    source, _ = saved_source(db, connection)
    selected = next((item for item in source.objects(body.schema_name) if item["name"] == body.object_name), None)
    if selected is None:
        raise AcquisitionOperationError(422, "SOURCE_OBJECT_UNAVAILABLE", "La tabla o vista seleccionada no está disponible.")
    found = db.scalar(select(Dataset).where(Dataset.organization_id == user.organization_id, Dataset.name == body.name))
    if found:
        raise AcquisitionOperationError(409, "DATASET_NAME_EXISTS", "El nombre ya corresponde a un dataset; utiliza Actualizar desde fuente.")
    dataset = Dataset(id=uid(), organization_id=user.organization_id, name=body.name,
                      domain=body.domain, description=body.description, owner=user.name)
    db.add(dataset)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise AcquisitionOperationError(409, "DATASET_NAME_EXISTS", "El nombre ya corresponde a otro dataset.") from None
    binding = DatasetSourceBinding(organization_id=user.organization_id, dataset_id=dataset.id,
        connection_id=connection.id, schema_name=body.schema_name, object_name=body.object_name,
        object_kind=selected["kind"], column_overrides=body.column_overrides)
    db.add(binding)
    run = register_source(db, user, connection, binding, dataset, idempotency_key=idempotency_key,
                          request_identity=request_identity)
    db.commit()
    return {"dataset": dataset_dto(db, dataset), "acquisition": acquisition_dto(run)}


@router.post("/datasets/{dataset_id}/acquisitions/refresh", status_code=202, response_model=AcquisitionResponse)
def refresh_source(dataset_id: str, idempotency_key: str | None = Header(default=None),
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    if "datasets:write" not in effective_permissions(db, user):
        raise AcquisitionOperationError(403, "FORBIDDEN", "Se requiere permiso para registrar una versión adquirida.")
    dataset = _dataset(db, user, dataset_id)
    binding = db.scalar(select(DatasetSourceBinding).where(DatasetSourceBinding.dataset_id == dataset.id,
                         DatasetSourceBinding.organization_id == user.organization_id))
    if binding is None:
        raise AcquisitionOperationError(409, "DATASET_HAS_NO_SOURCE", "El dataset no tiene una fuente externa registrada.")
    connection = owned_connection(db, binding.connection_id, user, lock=True)
    run = register_source(db, user, connection, binding, dataset, idempotency_key=idempotency_key,
                          request_identity={"operation": "REFRESH_SOURCE", "dataset_id": dataset_id})
    db.commit()
    return acquisition_dto(run)


@router.get("/acquisitions", response_model=AcquisitionListResponse)
def acquisitions(dataset_id: str | None = None, status: str | None = None,
                 limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0),
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(AcquisitionRun).where(AcquisitionRun.organization_id == user.organization_id)
    if dataset_id:
        _dataset(db, user, dataset_id)
        query = query.where(AcquisitionRun.dataset_id == dataset_id)
    if status:
        query = query.where(AcquisitionRun.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(AcquisitionRun.created_at.desc(), AcquisitionRun.id).offset(offset).limit(limit)).all()
    return {"items": [acquisition_dto(run) for run in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/acquisitions/{acquisition_id}", response_model=AcquisitionResponse)
def acquisition_detail(acquisition_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return acquisition_dto(owned_acquisition(db, acquisition_id, user))


@router.post("/acquisitions/{acquisition_id}/cancel", response_model=AcquisitionResponse)
def cancel(acquisition_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = cancel_acquisition(db, user, acquisition_id)
    db.commit()
    return acquisition_dto(run)
