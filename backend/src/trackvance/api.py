"""Local prototype HTTP boundary; all business endpoints require a scoped session."""

import csv
import hashlib
import io
import json
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Literal

import polars as pl
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import __version__
from .artifactstore import ArtifactIntegrityError, storage_provider
from .audit_context import Actor, actor_context, request_id_context
from .config import (
    DEMO_ACCESS_ENABLED,
    DEMO_SEED_ENABLED,
    DEMO_USER_ID,
    MAX_ROWS,
    MAX_UPLOAD_BYTES,
    SESSION_HOURS,
    WEB_ORIGIN,
)
from .config_semantics import RuleDefinition, effective_config, validate_config
from .connections_api import router as connections_router
from .connections_service import ConnectionOperationError
from .credential_store import SecretStoreError
from .dashboard import DashboardFilters, build_dashboard
from .dataset_readers import (
    UnsupportedDatasetFormat,
    dataset_reader_registry,
    inspect_dataset,
)
from .dataset_sources import SourceError
from .db import SessionLocal, get_db, iso, utcnow
from .exceptions_api import router as exceptions_router
from .identity_api import router as identity_router
from .identity_api import user_dto
from .migrate import migrate, migration_ready
from .models import (
    Artifact,
    AuditEvent,
    AuthSession,
    Configuration,
    Dataset,
    DatasetVersion,
    Finding,
    IdempotencyKey,
    Job,
    Run,
    User,
    uid,
)
from .operations_common import OperationError
from .permissions import permissions_for, required_permission
from .processing import ProcessingError, money, profile_frame
from .scheduler import ScheduleError
from .sentinel_api import router as sentinel_router
from .services import (
    audit,
    audit_dto,
    backfill_artifacts,
    config_dto,
    create_exception,
    create_version,
    dataset_dto,
    enqueue,
    exception_dto,
    finding_dto,
    register_export,
    result_rows,
    run_actor,
    run_dto,
    version_dto,
)

COOKIE = "trackvance_session"



class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def error_response(request: Request, status: int, code: str, message: str, details=None):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details, "request_id": getattr(request.state, "request_id", uid())}})


@asynccontextmanager
async def lifespan(_app):
    migrate()
    with SessionLocal() as db:
        backfill_artifacts(db)
        db.commit()
    from .seed import ensure_demo_user, seed_demo

    if DEMO_ACCESS_ENABLED:
        ensure_demo_user()
    if DEMO_SEED_ENABLED:
        seed_demo()
    yield


app = FastAPI(title="Trackvance Core", version=__version__, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(dict.fromkeys([WEB_ORIGIN, "http://localhost:3000", "http://127.0.0.1:3000"])), allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"])


@app.middleware("http")
async def session_boundary(request: Request, call_next):
    request.state.request_id = uid()
    public = {"/api/v1/health", "/api/v1/health/ready", "/api/v1/auth/demo", "/api/v1/auth/login"}
    if request.url.path.startswith("/api/v1/") and request.url.path not in public and request.method != "OPTIONS":
        token = request.cookies.get(COOKIE)
        if not token:
            return error_response(request, 401, "UNAUTHENTICATED", "Inicia sesión para continuar.")
        with SessionLocal() as db:
            session = db.scalar(select(AuthSession).where(AuthSession.token_hash == hashlib.sha256(token.encode()).hexdigest(), AuthSession.expires_at > utcnow()))
            user = db.get(User, session.user_id) if session else None
            if not session or not user or not user.active:
                return error_response(request, 401, "UNAUTHENTICATED", "La sesión venció. Inicia sesión nuevamente.")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf_token):
                return error_response(request, 403, "CSRF_FAILED", "Token de seguridad ausente o inválido.")
            permission = required_permission(request.url.path.removeprefix("/api/v1"), request.method)
            if permission and permission not in permissions_for(user.role):
                return error_response(request, 403, "PERMISSION_DENIED", "Tu rol no permite realizar esta acción.")
            request.state.user, request.state.session = user, session
    request_token = request_id_context.set(request.state.request_id)
    user = getattr(request.state, "user", None)
    actor_token = actor_context.set(Actor("USER", user.id, user.name) if user else None)
    try:
        response = await call_next(request)
    finally:
        actor_context.reset(actor_token)
        request_id_context.reset(request_token)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(APIError)
async def api_error(request, exc):
    return error_response(request, exc.status, exc.code, exc.message, exc.details)


@app.exception_handler(OperationError)
async def operation_error(request, exc):
    return error_response(request, exc.status, exc.code, exc.message, exc.details)


@app.exception_handler(ConnectionOperationError)
async def connection_error(request, exc):
    return error_response(request, exc.status, exc.code, exc.message)


@app.exception_handler(ScheduleError)
async def schedule_error(request: Request, exc: ScheduleError):
    return error_response(request, exc.status, exc.code, exc.message)


@app.exception_handler(SourceError)
async def source_error(request, exc):
    return error_response(request, 422, exc.code, str(exc))


@app.exception_handler(SecretStoreError)
async def secret_store_error(request, _exc):
    return error_response(request, 503, "CREDENTIAL_STORE_UNAVAILABLE", "No se pudieron recuperar las credenciales de la conexión.")


@app.exception_handler(ProcessingError)
async def processing_error(request, exc):
    return error_response(request, 422, "INVALID_DATA", str(exc))


@app.exception_handler(UnsupportedDatasetFormat)
async def unsupported_dataset_format(request, exc):
    return error_response(request, 422, "UNSUPPORTED_FORMAT", str(exc))


@app.exception_handler(ArtifactIntegrityError)
async def artifact_error(request, exc):
    return error_response(request, 409, "ARTIFACT_INTEGRITY_ERROR", str(exc))


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    details = [{"field": ".".join(map(str, error["loc"])), "message": error["msg"]} for error in exc.errors()]
    return error_response(request, 422, "VALIDATION_ERROR", "Revisa los datos enviados.", details)


@app.exception_handler(IntegrityError)
async def conflict_error(request, _exc):
    return error_response(request, 409, "CONFLICT", "El registro ya existe o fue modificado. Actualiza e intenta nuevamente.")


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    import logging
    logging.getLogger(__name__).exception("Request %s failed", request.state.request_id, exc_info=exc)
    return error_response(request, 500, "INTERNAL_ERROR", "No se pudo completar la operación. Consulta el registro del servidor.")


def current_user(request: Request) -> User:
    return request.state.user


def owned(db: Session, model, record_id: str, user: User):
    item = db.get(model, record_id)
    if not item or item.organization_id != user.organization_id:
        raise APIError(404, "NOT_FOUND", "No se encontró el registro solicitado.")
    return item


def listing(items):
    return {"items": items, "total": len(items)}


def scoped(db, model, user):
    return db.scalars(select(model).where(model.organization_id == user.organization_id).order_by(model.created_at.desc())).all()


def identity(user, csrf):
    return {"user": user_dto(user), "organization": {"id": user.organization_id, "name": "Trackvance Demo"}, "csrf_token": csrf, "demo_mode": user.id == DEMO_USER_ID}


router = APIRouter(prefix="/api/v1")


@app.get("/health")
@router.get("/health")
def health():
    return {
        "status": "ok",
        "version": __version__,
        "mode": "local-prototype",
        # Kept for clients of the original health contract; it now means access.
        "demo_enabled": DEMO_ACCESS_ENABLED,
        "demo_access_enabled": DEMO_ACCESS_ENABLED,
        "demo_seed_enabled": DEMO_SEED_ENABLED,
    }


@app.get("/health/ready")
@router.get("/health/ready")
def ready(db: Session = Depends(get_db)):
    probe_path = None
    try:
        db.execute(text("SELECT 1"))
        if not migration_ready():
            raise RuntimeError("MIGRATION_REQUIRED")
        probe_path = storage_provider.temporary_path(".ready")
        probe_path.write_bytes(b"ready")
    except (SQLAlchemyError, OSError, RuntimeError):
        return JSONResponse(status_code=503, content={"status": "not_ready", "code": "DEPENDENCY_UNAVAILABLE"})
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)
    return {"status": "ready", "version": __version__, "database": "ready", "storage": "ready", "migrations": "head"}


def establish_session(request, response, db, user):
    origin = request.headers.get("origin")
    if origin and origin not in {WEB_ORIGIN, "http://localhost:3000", "http://127.0.0.1:3000"}:
        raise APIError(403, "ORIGIN_DENIED", "Origen de acceso no permitido.")
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    session = AuthSession(organization_id=user.organization_id, token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, csrf_token=csrf, expires_at=utcnow() + timedelta(hours=SESSION_HOURS))
    db.add(session)
    audit(db, "SESSION_STARTED", "user", user.id, "Sesión iniciada", Actor("USER", user.id, user.name), user.organization_id)
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https", samesite="lax", max_age=SESSION_HOURS * 3600, path="/")
    return identity(user, csrf)


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/auth/demo")
def demo_login(request: Request, response: Response, db: Session = Depends(get_db)):
    if not DEMO_ACCESS_ENABLED:
        raise APIError(404, "DEMO_DISABLED", "El acceso demo no está habilitado.")
    user = db.get(User, DEMO_USER_ID)
    if not user or not user.active:
        raise APIError(503, "DEMO_UNAVAILABLE", "El usuario demo aún no está disponible.")
    return establish_session(request, response, db, user)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower().strip(), User.active.is_(True)))
    try:
        if not user or not PasswordHasher().verify(user.password_hash, body.password):
            raise VerificationError()
    except VerificationError as exc:
        audit(db, "LOGIN_FAILED", "user", user.id if user else "unknown", "Inicio de sesión rechazado", Actor("SYSTEM", "local-auth", "Autenticación"), user.organization_id if user else "org-trackvance-demo")
        db.commit()
        raise APIError(401, "INVALID_CREDENTIALS", "Correo o contraseña incorrectos.") from exc
    return establish_session(request, response, db, user)


@router.get("/me")
def me(request: Request):
    return identity(request.state.user, request.state.session.csrf_token)


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(db.get(AuthSession, request.state.session.id))
    audit(db, "SESSION_ENDED", "user", user.id, "Sesión finalizada", user.name, user.organization_id)
    db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DatasetBody(InputModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    domain: str = Field(default="Operaciones", min_length=1, max_length=80)
    owner: str = Field(default="Equipo de datos", min_length=1, max_length=120)
    criticality: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"


class DatasetSchemaColumn(BaseModel):
    name: str
    logical_type: str
    native_type: str | None = None
    nullable: bool = False
    semantic_tag: str | None = None
    numeric: bool = False


class DatasetSchemaResponse(BaseModel):
    dataset_id: str
    dataset_name: str
    version_id: str | None = None
    version: int | None = None
    schema_hash: str | None = None
    version_created_at: str | None = None
    scan_mode: Literal["NO_VERSION", "PERSISTED_PROFILE", "CANONICAL_PARQUET_METADATA"]
    scanned_rows: int = 0
    scanned_at: str
    columns: list[DatasetSchemaColumn] = Field(default_factory=list)


class DatasetFileInspection(BaseModel):
    format: str
    format_label: str
    filename: str
    sheets: list[str] = Field(default_factory=list)
    selected_sheet: str | None = None
    detected_delimiter: str | None = None
    reader_options: dict[str, str] = Field(default_factory=dict)
    columns: list[DatasetSchemaColumn] = Field(default_factory=list)
    row_count: int | None = None
    sampled_rows: int = 0
    supported_formats: list[dict] = Field(default_factory=list)


NUMERIC_SCHEMA_TYPES = frozenset({
    "BYTE", "SHORT", "INTEGER", "INT", "INT8", "INT16", "INT32", "INT64",
    "UINT8", "UINT16", "UINT32", "UINT64", "LONG", "BIGINT", "SMALLINT",
    "FLOAT", "FLOAT16", "FLOAT32", "FLOAT64", "DOUBLE", "REAL", "DECIMAL",
    "NUMERIC", "NUMBER",
})


def schema_type_is_numeric(value: str | None) -> bool:
    normalized = (value or "").upper().replace(" ", "").split("(", 1)[0]
    return normalized in NUMERIC_SCHEMA_TYPES


def logical_type_from_native(value: str) -> str:
    normalized = value.upper().replace(" ", "").split("(", 1)[0]
    if normalized.startswith(("INT", "UINT")):
        return "INT64"
    if schema_type_is_numeric(normalized):
        return "DECIMAL"
    if normalized == "BOOLEAN":
        return "BOOLEAN"
    if normalized == "DATE":
        return "DATE"
    if normalized.startswith("DATETIME"):
        return "TIMESTAMP"
    return "STRING"


def dataset_schema_response(
    dataset: Dataset,
    version: DatasetVersion | None,
    *,
    native_types: dict[str, str] | None = None,
) -> DatasetSchemaResponse:
    if version is None:
        return DatasetSchemaResponse(
            dataset_id=dataset.id,
            dataset_name=dataset.name,
            scan_mode="NO_VERSION",
            scanned_at=utcnow().isoformat(),
        )
    persisted = {item["name"]: item for item in version.schema_json}
    names = list(native_types) if native_types is not None else list(persisted)
    if native_types is not None and persisted and set(names) != set(persisted):
        raise APIError(
            409,
            "SCHEMA_METADATA_MISMATCH",
            "El esquema físico no coincide con la identidad de esta versión. Carga una versión nueva.",
        )
    columns = []
    for name in names:
        stored = persisted.get(name, {})
        native_type = (
            native_types.get(name)
            if native_types is not None
            else stored.get("native_type")
        )
        logical_type = stored.get("logical_type") or logical_type_from_native(native_type or "")
        semantic_tag = stored.get("semantic_tag")
        columns.append(DatasetSchemaColumn(
            name=name,
            logical_type=logical_type,
            native_type=native_type,
            nullable=bool(stored.get("nullable", False)),
            semantic_tag=semantic_tag,
            numeric=semantic_tag != "IDENTIFIER" and schema_type_is_numeric(logical_type),
        ))
    return DatasetSchemaResponse(
        dataset_id=dataset.id,
        dataset_name=dataset.name,
        version_id=version.id,
        version=version.version,
        schema_hash=version.schema_hash,
        version_created_at=iso(version.created_at),
        scan_mode="CANONICAL_PARQUET_METADATA" if native_types is not None else "PERSISTED_PROFILE",
        scanned_rows=0,
        scanned_at=utcnow().isoformat(),
        columns=columns,
    )


def dataset_name_exists(dataset: Dataset) -> APIError:
    return APIError(
        409,
        "DATASET_NAME_EXISTS",
        "Ya existe un dataset con ese nombre. Carga el archivo como una nueva versión.",
        {"dataset_id": dataset.id, "dataset_name": dataset.name},
    )


def parse_json_object(value: str, code: str, label: str) -> dict:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError()
        return parsed
    except (ValueError, TypeError):
        raise APIError(422, code, f"{label} debe ser un objeto JSON válido.") from None


def upload_filename(file: UploadFile) -> str:
    raw = Path((file.filename or "dataset.csv").replace("\\", "/")).name
    if len(raw) <= 240:
        return raw
    suffix = Path(raw).suffix.lower()
    if not (1 <= len(suffix) <= 11 and suffix[1:].isalnum()):
        suffix = ""
    return f"{Path(raw).stem[: 240 - len(suffix)]}{suffix}"


def stage_upload(file: UploadFile, filename: str) -> Path:
    extension = Path(filename).suffix.lower()
    suffix = extension if 1 <= len(extension) <= 11 and extension[1:].isalnum() else ".bin"
    temp_path = storage_provider.temporary_path(suffix)
    try:
        with temp_path.open("xb") as temp:
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise APIError(
                        422,
                        "UPLOAD_TOO_LARGE",
                        f"El límite por archivo es {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                    )
                temp.write(chunk)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


@router.get("/datasets")
def datasets(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return listing([dataset_dto(db, d) for d in scoped(db, Dataset, user)])


@router.post("/datasets", status_code=201)
def new_dataset(body: DatasetBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    existing = db.scalar(select(Dataset).where(
        Dataset.organization_id == user.organization_id,
        Dataset.name == body.name,
    ))
    if existing:
        raise dataset_name_exists(existing)
    dataset = Dataset(organization_id=user.organization_id, **body.model_dump())
    db.add(dataset)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Dataset).where(
            Dataset.organization_id == user.organization_id,
            Dataset.name == body.name,
        ))
        if existing:
            raise dataset_name_exists(existing) from None
        raise
    audit(db, "DATASET_CREATED", "dataset", dataset.id, f"Dataset creado: {dataset.name}", user.name, user.organization_id)
    db.commit()
    return dataset_dto(db, dataset)


@router.post("/datasets/uploads/inspect", response_model=DatasetFileInspection)
def inspect_dataset_upload(
    file: UploadFile = File(...),
    reader_options: str = Form("{}"),
    user: User = Depends(current_user),
):
    """Inspect a bounded sample/embedded schema before creating an immutable version."""
    del user
    filename = upload_filename(file)
    options = parse_json_object(
        reader_options,
        "INVALID_READER_OPTIONS",
        "Las opciones de lectura",
    )
    temp_path = None
    try:
        temp_path = stage_upload(file, filename)
        result = inspect_dataset(temp_path, filename, options)
        schema, _, _ = profile_frame(result.frame, native_types=result.native_schema)
        columns = [
            DatasetSchemaColumn(
                name=item["name"],
                logical_type=item["logical_type"],
                native_type=result.native_schema.get(item["name"]),
                nullable=bool(item.get("nullable", False)),
                semantic_tag=item.get("semantic_tag"),
                numeric=item.get("semantic_tag") != "IDENTIFIER"
                and schema_type_is_numeric(item["logical_type"]),
            )
            for item in schema
        ]
        effective_options = {
            key: value
            for key, value in {
                "sheet_name": result.selected_sheet,
                "delimiter": result.detected_delimiter,
            }.items()
            if value is not None
        }
        return DatasetFileInspection(
            format=result.source_format,
            format_label=result.format_label,
            filename=filename,
            sheets=result.sheets,
            selected_sheet=result.selected_sheet,
            detected_delimiter=result.detected_delimiter,
            reader_options=effective_options,
            columns=columns,
            row_count=result.row_count,
            sampled_rows=result.frame.height,
            supported_formats=dataset_reader_registry.formats,
        )
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        file.file.close()


@router.get("/datasets/{dataset_id}")
def dataset_detail(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = owned(db, Dataset, dataset_id, user)
    versions = db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version.desc())).all()
    return {**dataset_dto(db, dataset), "versions": [version_dto(v, db) for v in versions]}


@router.get("/datasets/{dataset_id}/schema", response_model=DatasetSchemaResponse)
def dataset_schema(
    dataset_id: str,
    refresh: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Return the latest immutable version schema without scanning business rows.

    The default response uses the profile captured when the version was created.
    An explicit refresh reads only the canonical Parquet footer and combines its
    physical types with the persisted logical inference.
    """
    dataset = owned(db, Dataset, dataset_id, user)
    version = db.scalar(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version.desc())
    )
    if not version or not refresh:
        return dataset_schema_response(dataset, version)
    try:
        if version.canonical_artifact_id:
            canonical_artifact = owned(db, Artifact, version.canonical_artifact_id, user)
            canonical_path = storage_provider.materialize(canonical_artifact)
        else:
            canonical_path = storage_provider.materialize_reference(
                version.canonical_path,
                expected_sha256=version.sha256 if version.source_type == "INTAKE_OUTPUT" else None,
            )
        physical_schema = pl.scan_parquet(canonical_path).collect_schema()
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise APIError(
            409,
            "SCHEMA_SCAN_FAILED",
            "No fue posible leer la metadata de la última versión del dataset.",
        ) from exc
    native_types = {name: str(data_type) for name, data_type in physical_schema.items()}
    return dataset_schema_response(dataset, version, native_types=native_types)


@router.post("/datasets/{dataset_id}/versions/upload", status_code=201)
def upload_version(
    dataset_id: str,
    file: UploadFile = File(...),
    column_overrides: str = Form("{}"),
    reader_options: str = Form("{}"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    dataset = owned(db, Dataset, dataset_id, user)
    filename = upload_filename(file)
    overrides = parse_json_object(
        column_overrides,
        "INVALID_SCHEMA_OVERRIDE",
        "Las opciones de columnas",
    )
    options = parse_json_object(
        reader_options,
        "INVALID_READER_OPTIONS",
        "Las opciones de lectura",
    )
    temp_path = None
    try:
        temp_path = stage_upload(file, filename)
        version = create_version(
            db,
            dataset,
            temp_path,
            filename,
            user.name,
            column_overrides=overrides,
            reader_options=options,
        )
        db.commit()
        return version_dto(version, db)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        file.file.close()


@router.get("/dataset-versions/{version_id}/profile")
def profile(version_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    version = owned(db, DatasetVersion, version_id, user)
    artifact = verify_registered_file(db, version.canonical_path, user.organization_id)
    path = storage_provider.materialize(artifact)
    return {**version_dto(version, db), "sample": pl.read_parquet(path, n_rows=20).to_dicts()}


class ColumnRules(InputModel):
    @model_validator(mode="before")
    @classmethod
    def remove_derived_metadata(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key not in {"semantics_version", "normalization_policy", "key_normalization_policy"}}
        return value

    @field_validator("*", mode="after")
    @classmethod
    def validate_columns(cls, value):
        if isinstance(value, list) and all(isinstance(item, str) for item in value) and (len(value) > 100 or any(not item.strip() or len(item) > 240 for item in value) or len(value) != len(set(value))):
            raise ValueError("Las columnas deben tener nombres únicos, no vacíos; máximo 100.")
        return value


class IntakeRules(ColumnRules):
    schema_version: Literal[2] = 2
    rules: list[RuleDefinition] = Field(default_factory=list, max_length=100)
    transforms: list[dict] = Field(default_factory=list, max_length=100)
    required_columns: list[str] = Field(default_factory=list)
    unique_columns: list[str] = Field(default_factory=list)
    numeric_columns: list[str] = Field(default_factory=list)
    positive_columns: list[str] = Field(default_factory=list)
    max_error_rate: float = Field(default=0, ge=0, le=1)


class KeyNormalization(InputModel):
    trim: bool = False
    case: Literal["NONE", "UPPER", "LOWER"] = "NONE"
    unicode_normalization: Literal["NONE", "NFC", "NFKC"] = "NONE"


class ReconRules(ColumnRules):
    @model_validator(mode="before")
    @classmethod
    def remove_empty_row_rule_metadata(cls, value):
        # Effective snapshots exposed an empty shared row-rule collection in v2.
        # Accept round trips of that metadata, but reject unsupported nonempty rules.
        if isinstance(value, dict) and value.get("rules") == []:
            return {key: item for key, item in value.items() if key != "rules"}
        return value

    schema_version: Literal[2] = 2
    key_normalization: KeyNormalization = Field(default_factory=KeyNormalization)
    comparison_rules: list[dict] = Field(default_factory=list, max_length=100)
    aggregation: dict | None = None
    aggregations: list[dict] = Field(default_factory=list, max_length=100)
    source_transforms: list[dict] = Field(default_factory=list, max_length=100)
    target_transforms: list[dict] = Field(default_factory=list, max_length=100)
    key_columns: list[str] = Field(min_length=1)
    amount_column: str | None = Field(default=None, min_length=1, max_length=240)
    tolerance: str = "0.01"

    @field_validator("tolerance")
    @classmethod
    def decimal_tolerance(cls, value):
        parsed = money(value)
        if parsed is None or parsed < 0:
            raise ValueError("La tolerancia debe ser un decimal no negativo con punto.")
        return str(parsed)


class MonitorRules(ColumnRules):
    schema_version: Literal[2] = 2
    rules: list[RuleDefinition] = Field(default_factory=list, max_length=100)
    required_columns: list[str] = Field(default_factory=list)
    null_columns: list[str] = Field(default_factory=list)
    max_null_rate: float = Field(default=.05, ge=0, le=1)
    max_volume_change_pct: float = Field(default=15, ge=0, le=10000)
    max_age_hours: float = Field(default=48, gt=0, le=87600)


class ConfigurationBody(InputModel):
    name: str = Field(min_length=1, max_length=160)
    dataset_id: str
    owner: str = Field(default="Equipo de datos", max_length=120)
    description: str = Field(default="", max_length=4000)


class IntakeBody(ConfigurationBody):
    config: IntakeRules


class ReconBody(ConfigurationBody):
    target_dataset_id: str
    config: ReconRules


class MonitorBody(ConfigurationBody):
    config: MonitorRules


def save_configuration(module, body, db, user):
    owned(db, Dataset, body.dataset_id, user)
    if module == "recon":
        owned(db, Dataset, body.target_dataset_id, user)
    data = body.model_dump()
    try:
        data["config"] = validate_config(module, data["config"])
    except ValueError as exc:
        raise APIError(422, "INVALID_RULE_CONFIGURATION", str(exc)) from exc
    validate_rule_references(db, data["config"], user)
    config = Configuration(organization_id=user.organization_id, module=module, **data)
    db.add(config)
    db.flush()
    audit(db, "CONFIGURATION_PUBLISHED", "configuration", config.id, f"Configuración publicada: {config.name}", user.name, user.organization_id, {"module": module, "version": 1})
    db.commit()
    return config_dto(db, config)


def validate_rule_references(db: Session, config: dict, user: User) -> None:
    """Only immutable versions in this organization can enter a rule snapshot."""
    for rule in config.get("rules", []):
        if rule.get("type") != "reference":
            continue
        parameters = rule["parameters"]
        reference = owned(db, DatasetVersion, parameters["dataset_version_id"], user)
        available = {column["name"] for column in reference.schema_json}
        if set(parameters["reference_columns"]) - available:
            raise APIError(422, "INVALID_RULE_REFERENCE", "Las columnas de referencia no existen en la versión seleccionada.")


@router.get("/intake/contracts")
@router.get("/recon/controls")
@router.get("/monitors")
def configurations(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    module = "intake" if "/intake/" in request.url.path else "recon" if "/recon/" in request.url.path else "sentinel"
    return listing([config_dto(db, c) for c in scoped(db, Configuration, user) if c.module == module])


@router.post("/intake/contracts", status_code=201)
def intake_contract(body: IntakeBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return save_configuration("intake", body, db, user)


@router.post("/recon/controls", status_code=201)
def recon_control(body: ReconBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return save_configuration("recon", body, db, user)


@router.post("/monitors", status_code=201)
def monitor(body: MonitorBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return save_configuration("sentinel", body, db, user)


class IntakeRunBody(InputModel):
    contract_id: str
    dataset_version_id: str


class ReconRunBody(InputModel):
    control_id: str
    source_version_id: str
    target_version_id: str


class MonitorRunBody(InputModel):
    dataset_version_id: str | None = None


def new_run(module, config_id, source_id, target_id, db, user, request=None):
    key = request.headers.get("Idempotency-Key") if request else None
    route = request.url.path if request else ""
    request_hash = hashlib.sha256(json.dumps([module, config_id, source_id, target_id]).encode()).hexdigest()
    if key:
        if len(key) > 128 or not key.strip():
            raise APIError(422, "INVALID_IDEMPOTENCY_KEY", "La clave de idempotencia debe tener entre 1 y 128 caracteres.")
        existing = db.scalar(select(IdempotencyKey).where(IdempotencyKey.organization_id == user.organization_id, IdempotencyKey.route == route, IdempotencyKey.key == key))
        if existing:
            if existing.request_hash != request_hash:
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "Esta clave ya se utilizó con otros parámetros.")
            return run_dto(db, owned(db, Run, existing.run_id, user))
    config = owned(db, Configuration, config_id, user)
    if config.module != module:
        raise APIError(422, "WRONG_MODULE", "La configuración no pertenece a este módulo.")
    source = owned(db, DatasetVersion, source_id, user)
    target = owned(db, DatasetVersion, target_id, user) if target_id else None
    run = enqueue(db, config, source, target, user.name)
    if key:
        db.add(IdempotencyKey(organization_id=user.organization_id, route=route, key=key, request_hash=request_hash, run_id=run.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(IdempotencyKey).where(IdempotencyKey.organization_id == user.organization_id, IdempotencyKey.route == route, IdempotencyKey.key == key)) if key else None
        if not existing or existing.request_hash != request_hash:
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "La solicitud entró en conflicto con otra ejecución.") from None
        return run_dto(db, owned(db, Run, existing.run_id, user))
    return run_dto(db, run)


@router.post("/intake/runs", status_code=202)
def intake_run(body: IntakeRunBody, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return new_run("intake", body.contract_id, body.dataset_version_id, None, db, user, request)


@router.post("/recon/runs", status_code=202)
def recon_run(body: ReconRunBody, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return new_run("recon", body.control_id, body.source_version_id, body.target_version_id, db, user, request)


@router.post("/monitors/{monitor_id}/runs", status_code=202)
def monitor_run(monitor_id: str, body: MonitorRunBody, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    config = owned(db, Configuration, monitor_id, user)
    source_id = body.dataset_version_id
    if not source_id:
        version = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == config.dataset_id).order_by(DatasetVersion.version.desc()))
        if not version:
            raise APIError(422, "NO_VERSION", "Carga una versión del dataset antes de ejecutar el monitor.")
        source_id = version.id
    return new_run("sentinel", config.id, source_id, None, db, user, request)


@router.get("/runs")
def runs(module: Literal["intake", "recon", "sentinel"] | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return listing([run_dto(db, r) for r in scoped(db, Run, user) if module is None or r.module == module])


@router.get("/runs/{run_id}")
def run_detail(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = owned(db, Run, run_id, user)
    findings = db.scalars(select(Finding).where(Finding.run_id == run.id)).all()
    return {**run_dto(db, run), "findings": [finding_dto(db, f) for f in findings]}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = owned(db, Run, run_id, user)
    if run.status in {"SUCCESS", "FAILED", "CANCELLED"}:
        raise APIError(409, "RUN_FINISHED", "La ejecución ya terminó.")
    run.cancel_requested = True
    if run.status == "QUEUED":
        run.status, run.progress_stage, run.finished_at = "CANCELLED", "Cancelado", utcnow()
        job = db.scalar(select(Job).where(Job.run_id == run.id))
        if job:
            job.status = "CANCELLED"
    audit(db, "RUN_CANCEL_REQUESTED", "run", run.id, "Cancelación solicitada", user.name, user.organization_id)
    db.commit()
    return run_dto(db, run)


@router.get("/runs/{run_id}/results")
@router.get("/intake/runs/{run_id}/errors")
@router.get("/recon/runs/{run_id}/results")
def results(run_id: str, classification: str | None = None, offset: int = 0, limit: int = 50, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if offset < 0 or limit < 1 or limit > 1000:
        raise APIError(422, "INVALID_PAGINATION", "offset debe ser ≥ 0 y limit debe estar entre 1 y 1000.")
    run = owned(db, Run, run_id, user)
    if run.result_path:
        verify_registered_file(db, run.result_path, user.organization_id)
    return result_rows(run, classification or None, offset, limit, db=db)


def authorize_artifact_file(artifact):
    return storage_provider.materialize(artifact)


def verify_registered_file(db: Session, path: str | None, organization_id: str) -> Artifact:
    artifact = db.scalar(select(Artifact).where(Artifact.organization_id == organization_id, Artifact.path == path))
    if not artifact:
        raise APIError(409, "ARTIFACT_NOT_REGISTERED", "No hay una identidad verificable para este artefacto. Revisa el diagnóstico del almacenamiento.")
    authorize_artifact_file(artifact)
    return artifact


ARTIFACT_MEDIA_SUFFIXES = {
    "application/vnd.apache.parquet": ({".parquet", ".pq"}, ".parquet"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ({".xlsx"}, ".xlsx"),
    "application/json": ({".json", ".jsonl", ".ndjson"}, ".json"),
    "application/x-ndjson": ({".jsonl", ".ndjson"}, ".jsonl"),
    "text/csv": ({".csv"}, ".csv"),
    "text/plain": ({".txt", ".tsv"}, ".txt"),
    "text/tab-separated-values": ({".tsv", ".txt"}, ".tsv"),
}
KNOWN_ARTIFACT_SUFFIXES = {
    suffix for allowed, _default in ARTIFACT_MEDIA_SUFFIXES.values() for suffix in allowed
}


def artifact_download_suffix(artifact: Artifact) -> str:
    suffix = Path(artifact.name).suffix.lower()
    media_type = (artifact.media_type or "").partition(";")[0].lower()
    policy = ARTIFACT_MEDIA_SUFFIXES.get(media_type)
    if policy:
        allowed, default = policy
        return suffix if suffix in allowed else default
    return suffix if suffix in KNOWN_ARTIFACT_SUFFIXES else ".bin"


@router.get("/runs/{run_id}/evidence")
def evidence(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = owned(db, Run, run_id, user)
    if not run.evidence_path:
        raise APIError(409, "EVIDENCE_NOT_READY", "La evidencia estará disponible al completar la ejecución.")
    artifact = verify_registered_file(db, run.evidence_path, user.organization_id)
    path = authorize_artifact_file(artifact)
    audit(db, "EVIDENCE_DOWNLOADED", "run", run.id, "Evidencia descargada", user.name, user.organization_id, {"artifact_id": artifact.id if artifact else None}, run_id=run.id)
    db.commit()
    return FileResponse(path, media_type="application/json", filename=f"trackvance_{run.id}_evidence.json")


@router.get("/artifacts/{artifact_id}/download")
def artifact_download(artifact_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .exports import XLSX_MIME_TYPE, export_filename
    artifact = owned(db, Artifact, artifact_id, user)
    path = authorize_artifact_file(artifact)
    audit(db, "ARTIFACT_DOWNLOADED", "artifact", artifact.id, "Artefacto descargado", user.name, user.organization_id, {"kind": artifact.kind, "sha256": artifact.sha256})
    db.commit()
    media = XLSX_MIME_TYPE if artifact.kind == "EXPORT_XLSX" else artifact.media_type
    suffix = artifact_download_suffix(artifact)
    filename = export_filename("artifact", artifact.id) if artifact.kind == "EXPORT_XLSX" else f"trackvance_{artifact.id}{suffix}"
    return FileResponse(path, media_type=media, filename=filename)


@router.get("/runs/{run_id}/export.xlsx", response_class=Response, responses={200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {"schema": {"type": "string", "format": "binary"}}}}})
def export_excel(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .exports import XLSX_MIME_TYPE, build_run_workbook, export_filename
    from .manifests import read_manifest
    run = owned(db, Run, run_id, user)
    if run.status != "SUCCESS":
        raise APIError(409, "RESULTS_NOT_READY", "Los resultados estarán disponibles al completar la ejecución.")
    config = owned(db, Configuration, run.config_id, user)
    inputs = []
    for version_id in [run.dataset_version_id, run.target_version_id]:
        if version_id:
            version = owned(db, DatasetVersion, version_id, user)
            dataset = owned(db, Dataset, version.dataset_id, user)
            inputs.append({**version_dto(version, db), "dataset_name": dataset.name})
    artifacts = db.scalars(select(Artifact).where(Artifact.organization_id == user.organization_id)).all()
    evidence_artifact = verify_registered_file(db, run.evidence_path, user.organization_id)
    verify_registered_file(db, run.result_path, user.organization_id)
    manifest = read_manifest(
        storage_provider.materialize(evidence_artifact),
        actor=run_actor(run),
        artifacts=artifacts,
    )
    rows = result_rows(run, limit=MAX_ROWS * 100, db=db)["items"]
    try:
        content = build_run_workbook(run_dto(db, run), config_dto(db, config), inputs, manifest, rows)
    except ValueError as exc:
        raise APIError(422, "EXPORT_LIMIT_EXCEEDED", str(exc)) from exc
    path = storage_provider.temporary_path(".xlsx")
    path.write_bytes(content)
    try:
        artifact = register_export(db, run, path)
        audit(db, "EXPORT_DOWNLOADED", "run", run.id, "Informe Excel descargado", user.name, user.organization_id, {"artifact_id": artifact.id, "kind": artifact.kind, "sha256": artifact.sha256}, run_id=run.id)
        db.commit()
    finally:
        path.unlink(missing_ok=True)
    return Response(content=content, media_type=XLSX_MIME_TYPE, headers={"Content-Disposition": f'attachment; filename="{export_filename(run.module, run.id)}"', "X-Artifact-ID": artifact.id})


def csv_cell(value):
    if value is None:
        return ""
    rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return "'" + rendered if rendered.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else rendered


@router.get("/runs/{run_id}/export.csv", deprecated=True, description="Compatibilidad para clientes históricos. La interfaz utiliza el informe Excel estructurado.")
def export_csv(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = owned(db, Run, run_id, user)
    if run.status != "SUCCESS":
        raise APIError(409, "RESULTS_NOT_READY", "Los resultados estarán disponibles al completar la ejecución.")
    verify_registered_file(db, run.result_path, user.organization_id)
    rows = result_rows(run, limit=MAX_ROWS * 100, db=db)["items"]
    out = io.StringIO(newline="")
    fields = list(rows[0]) if rows else ["classification", "message"]
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key: csv_cell(value) for key, value in row.items()} for row in rows)
    audit(db, "EXPORT_DOWNLOADED", "run", run.id, "Exportación CSV legacy descargada", user.name, user.organization_id, {"format": "CSV_LEGACY"}, run_id=run.id)
    db.commit()
    return Response(content="\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="trackvance-{run.id}.csv"'})


@router.get("/monitors/{monitor_id}/metrics")
def monitor_metrics(monitor_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned(db, Configuration, monitor_id, user)
    rows = db.scalars(select(Run).where(Run.config_id == monitor_id, Run.status == "SUCCESS").order_by(Run.created_at)).all()
    return listing([{"run_id": run.id, "observed_at": iso(run.created_at), "row_count": run.metrics.get("row_count", 0), "null_rate": run.metrics.get("null_rate", 0), "health_score": run.metrics.get("health_score", 0), "status": run.decision} for run in rows])


@router.get("/findings")
def findings(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return listing([finding_dto(db, f) for f in scoped(db, Finding, user)])


@router.post("/findings/{finding_id}/exceptions", status_code=201)
def finding_exception(finding_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = create_exception(db, owned(db, Finding, finding_id, user), user.name)
    db.commit()
    return exception_dto(db, case)


@router.get("/audit-events")
def audit_events(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return listing([audit_dto(a) for a in scoped(db, AuditEvent, user)])


RULES = [
    ("NOT_NULL", "No nulo", "intake", "Rechaza null y conserva texto vacío; política distinta de REQUIRED."),
    ("TYPE", "Tipo de dato", "intake", "Verifica tipo lógico explícito por registro."),
    ("COMPOUND_UNIQUE", "Unicidad compuesta", "intake", "Comprueba la combinación de varias columnas sin concatenarlas."),
    ("LENGTH", "Longitud de texto", "intake", "Límites inclusivos de caracteres Unicode."),
    ("COLUMN_COMPARE", "Comparación entre columnas", "intake", "Compara dos columnas con operador y semántica declarados."),
    ("REFERENCE", "Integridad referencial", "intake", "Valida pertenencia a una DatasetVersion inmutable de la misma organización."),
    ("REQUIRED", "Campo obligatorio", "intake", "Rechaza valores nulos o vacíos en columnas obligatorias."),
    ("UNIQUE", "Clave única", "intake", "Detecta todas las filas con claves duplicadas."),
    ("NUMERIC", "Formato decimal", "intake", "Valida números finitos con punto decimal."),
    ("POSITIVE", "Valor positivo", "intake", "Exige un número decimal mayor que cero."),
    ("EXACT_COMPARE", "Comparación exacta", "recon", "Igualdad de una o varias columnas tras la normalización declarada."),
    ("NUMERIC_TOLERANCE", "Tolerancia numérica", "recon", "Compara importes Decimal con tolerancia absoluta y porcentual explícita."),
    ("DATE_TOLERANCE", "Tolerancia de fecha", "recon", "Compara fechas o timestamps con una diferencia máxima en horas o días."),
    ("AGGREGATE_COMPARE", "Agregación 1:N", "recon", "Compara un registro con la suma o el conteo de registros del otro lado."),
    ("RANGE", "Rango numérico", "intake", "Límites gt/gte/lt/lte declarados; también disponible en Sentinel."),
    ("ALLOWED_VALUES", "Valores permitidos", "intake", "Lista exacta de valores con política de nulos declarada; también en Sentinel."),
    ("REGEX", "Patrón de texto", "intake", "Expresión regular portable, sin scripting; también disponible en Sentinel."),
    ("DATE_RULE", "Regla de fecha", "intake", "Fecha no futura y límites mínimo/máximo explícitos; también en Sentinel."),
    ("DISTINCT_COUNT", "Valores distintos", "sentinel", "Conteo exacto de valores observados sin normalizaciones silenciosas."),
    ("DISTINCT_RATE", "Proporción de valores distintos", "sentinel", "Valores distintos respecto a la población declarada."),
    ("UNIQUENESS_RATIO", "Proporción de valores únicos", "sentinel", "Mide los valores que aparecen una sola vez."),
    ("SCHEMA_TYPE", "Cambio de tipo", "sentinel", "Comprueba el tipo lógico esperado de cada columna."),
    ("HISTORICAL_BAND", "Banda histórica", "sentinel", "Mediana e IQR sobre series compatibles por método y versión."),
    ("SCHEMA_REQUIRED", "Estructura requerida", "sentinel", "Comprueba columnas esperadas."),
    ("NULL_RATE", "Proporción de nulos", "sentinel", "Compara la proporción de nulos con un límite configurable."),
    ("FRESHNESS", "Actualización", "sentinel", "Valida la antigüedad de la carga."),
    ("VOLUME_CHANGE", "Variación de volumen", "sentinel", "Compara el volumen con la versión anterior; sin historial verifica volumen mínimo."),
]


@router.get("/rules")
def rules():
    return listing([{"id": code, "code": code, "name": name, "type": "BUILT_IN", "module": module, "description": description, "severity": "HIGH", "legacy_aliases": ["EXACT_MATCH"] if code == "NUMERIC_TOLERANCE" else []} for code, name, module, description in RULES])





@router.get("/system/engines")
def engines():
    from .worker import worker_status
    return {"items": [{"id": "polars", "name": "Polars", "version": pl.__version__, "available": True, "status": "ACTIVE", "description": "Procesamiento local CSV y Parquet; conciliación monetaria exacta con Decimal."}, {"id": "spark", "name": "Apache Spark local", "version": None, "available": False, "status": "UNAVAILABLE", "description": "Adaptador opcional aún no implementado. El plan rechaza cargas que superen el presupuesto local; no simula su ejecución."}], "worker": worker_status(), "limits": {"max_upload_mb": MAX_UPLOAD_BYTES / 1024 / 1024, "max_rows": MAX_ROWS}, "mode": "local-prototype"}


@router.get("/dashboard")
def dashboard(
    period: Literal["7d", "30d", "90d", "all"] = "30d",
    dataset_id: str | None = None,
    module: Literal["intake", "recon", "sentinel"] | None = None,
    status: Literal["ATTENTION", "HEALTHY", "IN_PROGRESS", "TECHNICAL_FAILURE"] | None = None,
    criticality: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if dataset_id:
        owned(db, Dataset, dataset_id, user)
    return build_dashboard(db, user.organization_id, DashboardFilters(
        period=period,
        dataset_id=dataset_id,
        module=module,
        status=status,
        criticality=criticality,
    ))


class ConfigurationVersionBody(InputModel):
    config: dict
    description: str | None = Field(default=None, max_length=4000)


@router.post("/intake/contracts/{configuration_id}/versions", status_code=201)
@router.post("/recon/controls/{configuration_id}/versions", status_code=201)
@router.post("/monitors/{configuration_id}/versions", status_code=201)
def publish_configuration_version(configuration_id: str, body: ConfigurationVersionBody, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    previous = owned(db, Configuration, configuration_id, user)
    module = "intake" if "/intake/" in request.url.path else "recon" if "/recon/" in request.url.path else "sentinel"
    if previous.module != module:
        raise APIError(422, "WRONG_MODULE", "La configuración no pertenece a este módulo.")
    if db.scalar(select(Configuration).where(Configuration.previous_version_id == previous.id)):
        raise APIError(409, "VERSION_CONFLICT", "Ya existe una versión posterior. Utilízala como base.")
    schemas: dict[str, type[BaseModel]] = {"intake": IntakeRules, "recon": ReconRules, "sentinel": MonitorRules}
    schema = schemas[module]
    try:
        config = validate_config(module, schema.model_validate(body.config).model_dump())
    except ValueError as exc:
        raise APIError(422, "INVALID_RULE_CONFIGURATION", str(exc)) from exc
    validate_rule_references(db, config, user)
    new = Configuration(organization_id=user.organization_id, name=previous.name, module=module, version=previous.version + 1, dataset_id=previous.dataset_id, target_dataset_id=previous.target_dataset_id, owner=previous.owner, description=body.description if body.description is not None else previous.description, config=config, previous_version_id=previous.id)
    db.add(new)
    db.flush()
    audit(db, "CONFIGURATION_PUBLISHED", "configuration", new.id, f"Nueva versión publicada: {new.name}", user.name, user.organization_id, {"module": module, "version": new.version, "previous_version_id": previous.id})
    db.commit()
    return config_dto(db, new)


@router.get("/runs/{run_id}/execution-plan")
def run_execution_plan(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return owned(db, Run, run_id, user).execution_plan


@router.get("/runs/{run_id}/diagnostics")
def run_diagnostics(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = owned(db, Run, run_id, user)
    configuration = owned(db, Configuration, run.config_id, user)
    effective = effective_config(run.module, configuration.config)
    return {"run_id": run.id, "status": run.status, "execution_plan": run.execution_plan, "key_normalization": effective.get("key_normalization"), "configuration_schema_version": effective.get("schema_version"), "started_at": iso(run.started_at), "finished_at": iso(run.finished_at), "error": run.error}


class PlanPreviewBody(InputModel):
    configuration_id: str
    dataset_version_id: str
    target_version_id: str | None = None


@router.post("/execution-plans/preview")
def preview_execution(body: PlanPreviewBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .planner import ExecutionPlanner, WorkloadInput
    config = owned(db, Configuration, body.configuration_id, user)
    versions = [owned(db, DatasetVersion, body.dataset_version_id, user)]
    if body.target_version_id:
        versions.append(owned(db, DatasetVersion, body.target_version_id, user))
    if versions[0].dataset_id != config.dataset_id or (config.module == "recon" and (len(versions) != 2 or versions[1].dataset_id != config.target_dataset_id)):
        raise APIError(422, "DATASET_MISMATCH", "Las versiones no corresponden a los datasets de la configuración.")
    return ExecutionPlanner().plan(config.module, [WorkloadInput(v.row_count, v.column_count, v.size_bytes) for v in versions], effective_config(config.module, config.config))


app.include_router(router)
app.include_router(connections_router)
app.include_router(identity_router)
app.include_router(exceptions_router)
app.include_router(sentinel_router)


def openapi_contract():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes,
                         description="Monolito modular local. SUCCESS es estado técnico; decision conserva el resultado de negocio. Configuraciones y evidence son snapshots versionados.")
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["LocalSession"] = {
        "type": "apiKey", "in": "cookie", "name": COOKIE,
        "description": "Sesión local HttpOnly. Las mutaciones requieren además X-CSRF-Token.",
    }
    public = {"/health", "/health/ready", "/api/v1/health", "/api/v1/health/ready", "/api/v1/auth/demo", "/api/v1/auth/login"}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "patch", "put", "delete"} or path in public:
                continue
            operation["security"] = [{"LocalSession": []}]
            permission = required_permission(path.removeprefix("/api/v1"), method.upper())
            if permission:
                operation["x-required-permission"] = permission
            if method != "get":
                operation.setdefault("parameters", []).append({"name": "X-CSRF-Token", "in": "header", "required": True, "schema": {"type": "string"}})
            for status, description in [(401, "Sesión ausente o vencida"), (403, "Permiso u origen no autorizado / CSRF inválido"), (404, "Recurso no disponible en la organización"), (409, "Conflicto de versión, precondición o integridad")]:
                operation["responses"].setdefault(str(status), {"description": description})
    app.openapi_schema = schema
    return schema


app.openapi = openapi_contract  # type: ignore[method-assign]  # FastAPI's documented schema customization hook.
