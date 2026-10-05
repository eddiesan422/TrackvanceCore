"""Report definitions, ephemeral streams and explicit durable publication."""
from __future__ import annotations

from typing import Literal, cast

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .db import SessionLocal, get_db, iso
from .governance import validate_classification
from .models import Dataset, User, uid
from .operations_common import OperationError
from .report_config import ReportLimits
from .report_executor import execute_messages
from .report_exports import csv_stream, preview_row, xlsx_stream
from .report_models import ReportDefinition, ReportExecution, ReportRevision
from .report_query import digest
from .report_service import (
    admission,
    check_execution,
    execution_dto,
    execution_sources,
    list_sources,
    owned,
    permission,
    resolve,
    resolved_context,
    start_execution,
    terminal,
)

router = APIRouter(prefix="/api/v1/reports", tags=["Reportes"])


def current_user(request: Request):
    return request.state.user


class ResolveBody(BaseModel):
    draft: dict
    revision_id: str | None = None


class ContextBody(BaseModel):
    context_id: str


class DownloadBody(ContextBody):
    format: Literal["CSV", "XLSX"] = "CSV"


class DefinitionBody(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=5000)
    draft: dict


class RevisionBody(BaseModel):
    expected_version: int = Field(ge=1)
    draft: dict


class DatasetBody(ContextBody):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=5000)
    macro_domain_id: str | None = None
    domain_id: str | None = None
    owner: str = Field(default="", max_length=120)
    criticality: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    business_owner_id: str | None = None
    steward_id: str | None = None
    technical_custodian_id: str | None = None
    information_classification: Literal["UNKNOWN", "PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"] = "UNKNOWN"


@router.get("/limits")
def limits():
    return {"profiles": {p: ReportLimits.configured(p).dto() for p in ("PREVIEW", "DOWNLOAD", "DATASET", "XLSX")},
            "sql_capabilities": {"ctes": False, "subqueries": False, "select_star": False,
                                 "joins": ["INNER", "LEFT", "RIGHT", "FULL"],
                                 "aggregates": ["COUNT", "SUM", "MIN", "MAX"],
                                 "decimal_average": "SUM_AND_COUNT"},
            "csv_contract": "UTF8_RFC4180_QUOTED_NULL_BACKSLASH_N_ESCAPED_PREFIXES_FORMULA_APOSTROPHE_V1",
            "xlsx_contract": "INLINE_TEXT_EXACT_DECIMAL_DATE_ISO_NULL_ABSENT_EMPTY_EXPLICIT_V1"}


@router.get("/sources")
def sources(search: str = Query(default="", max_length=160), offset: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=100), revision_offset: int = Query(0, ge=0), version_offset: int = Query(0, ge=0),
            contract_id: str | None = Query(None, max_length=64),
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    return list_sources(db, user, search, offset, limit, revision_offset, version_offset, contract_id)


@router.post("/resolve")
def resolve_draft(body: ResolveBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    context = resolve(db, user, body.draft, body.revision_id)
    return {"context_id": context.id, "expires_at": iso(context.expires_at),
            "sources": context.snapshot["sources"], "warnings": context.snapshot["warnings"],
            "query_hash": context.snapshot["plan"]["query_hash"]}


def _definition(db, definition, revision_offset=0, revision_limit=50):
    query = select(ReportRevision).where(ReportRevision.definition_id == definition.id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    revisions = db.scalars(query.order_by(ReportRevision.version.desc()).offset(revision_offset).limit(revision_limit)).all()
    return {"id": definition.id, "name": definition.name, "description": definition.description,
            "owner_user_id": definition.owner_user_id, "version": definition.version, "active": definition.active,
            "revision_total": total, "revision_offset": revision_offset, "revision_limit": revision_limit,
            "revisions": [{"id": r.id, "version": r.version, "draft": r.draft, "query_hash": r.query_hash,
                           "created_at": iso(r.created_at)} for r in revisions]}


@router.get("/definitions")
def definitions(search: str = Query("", max_length=160), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(ReportDefinition).where(ReportDefinition.organization_id == user.organization_id,
                                          ReportDefinition.name.ilike(f"%{search}%"))
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": [_definition(db, d, revision_limit=1) for d in db.scalars(query.order_by(ReportDefinition.created_at.desc()).offset(offset).limit(limit))],
            "total": count, "offset": offset, "limit": limit}


@router.get("/definitions/{identity}")
def definition(identity: str, revision_offset: int = Query(0, ge=0), revision_limit: int = Query(50, ge=1, le=100),
               revision_id: str | None = Query(None, max_length=64),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, ReportDefinition, identity, user)
    result = _definition(db, item, revision_offset, revision_limit)
    if revision_id:
        revision = owned(db, ReportRevision, revision_id, user)
        if revision.definition_id != item.id:
            raise OperationError(404, "NOT_FOUND", "La revisión no pertenece a esta definición.")
    else:
        revision = db.scalar(select(ReportRevision).where(ReportRevision.definition_id == item.id)
                             .order_by(ReportRevision.version.desc()).limit(1))
    if revision:
        result["selected_revision"] = {"id": revision.id, "version": revision.version, "draft": revision.draft,
                                       "query_hash": revision.query_hash, "created_at": iso(revision.created_at)}
    else:
        result["selected_revision"] = None
    return result


@router.post("/definitions")
def save_definition(body: DefinitionBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    permission(db, user, "reports:write")
    context = resolve(db, user, body.draft)
    draft = {**body.draft, "expected_schemas": context.snapshot["plan"]["expected_schemas"]}
    item = ReportDefinition(id=uid(), organization_id=user.organization_id, name=body.name.strip(),
                            description=body.description, owner_user_id=user.id)
    try:
        db.add(item)
        db.flush()
        db.add(ReportRevision(organization_id=user.organization_id, definition_id=item.id, version=1,
                              draft=draft, query_hash=digest(draft), created_by_user_id=user.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise OperationError(409, "REPORT_NAME_CONFLICT", "Ya existe una definición con ese nombre.") from None
    return _definition(db, item)


@router.post("/definitions/{identity}/revisions")
def save_revision(identity: str, body: RevisionBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    permission(db, user, "reports:write")
    item = owned(db, ReportDefinition, identity, user)
    context = resolve(db, user, body.draft)
    draft = {**body.draft, "expected_schemas": context.snapshot["plan"]["expected_schemas"]}
    result = db.execute(update(ReportDefinition).where(ReportDefinition.id == item.id,
        ReportDefinition.version == body.expected_version).values(version=body.expected_version + 1))
    if cast(CursorResult, result).rowcount != 1:
        db.rollback()
        raise OperationError(409, "VERSION_CONFLICT", "La definición cambió; consulta la revisión vigente.")
    db.add(ReportRevision(organization_id=user.organization_id, definition_id=item.id,
        version=body.expected_version + 1, draft=draft, query_hash=digest(draft), created_by_user_id=user.id))
    db.commit()
    db.refresh(item)
    return _definition(db, item)


@router.get("/executions")
def executions(profile: Literal["PREVIEW", "DOWNLOAD", "DATASET"] | None = None,
               status: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    # Unguarded temporary SQL/parameters remain private to their initiating user.
    query = select(ReportExecution).where(ReportExecution.organization_id == user.organization_id, ReportExecution.user_id == user.id)
    if profile:
        query = query.where(ReportExecution.profile == profile)
    if status:
        query = query.where(ReportExecution.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": [execution_dto(db, e) for e in db.scalars(query.order_by(ReportExecution.created_at.desc()).offset(offset).limit(limit))], "total": total}


@router.get("/executions/{identity}")
def execution(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, ReportExecution, identity, user)
    if item.user_id != user.id:
        raise OperationError(403, "REPORT_EXECUTION_OWNER", "El historial temporal pertenece a otro usuario.")
    return execution_dto(db, item)


@router.post("/preview")
def preview(body: ContextBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    context = resolved_context(db, body.context_id, user)
    run = start_execution(db, user, context, "PREVIEW")
    try:
        admission(db, run)
        inputs = execution_sources(db, context)
        plan, identity = context.snapshot["plan"], run.id
        db.commit()  # no long metadata transaction during engine work.
        rows: list[dict] = []
        columns, cardinality, metrics = [], [], {}
        for message in execute_messages(inputs, plan, "PREVIEW", check=lambda: check_execution(identity, "reports:preview")):
            if message["kind"] == "schema":
                columns = message["columns"]
            elif message["kind"] == "cardinality":
                cardinality = message["items"]
            elif message["kind"] == "batch":
                rows.extend(preview_row([c["name"] for c in columns], row) for row in message["rows"])
            elif message["kind"] == "complete":
                metrics = {k: v for k, v in message.items() if k != "kind"}
        check_execution(identity, "reports:preview")
        terminal(identity, status="SUCCESS", metrics={**metrics, "cardinality": cardinality}, generation="COMPLETE")
        return {"execution_id": identity, "context_id": context.id, "status": "SUCCESS", "rows": rows,
                "columns": columns, "cardinality": cardinality, "warnings": context.snapshot["warnings"],
                "sample": True, "total_rows": None, "query_limited": plan["query_limited"]}
    except OperationError as exc:
        terminal(run.id, status="FAILED", error=exc, generation="FAILED")
        resources = {"REPORT_MEMORY_LIMIT", "REPORT_TIMEOUT", "REPORT_BATCH_LIMIT", "REPORT_RESULT_LIMIT"}
        exc.details = {"context_id": context.id, "execution_id": run.id, "allow_generate": exc.code in resources}
        raise


class ReportStreamingResponse(StreamingResponse):
    def __init__(self, content, identity, *, on_close=None, **kwargs):
        self.identity = identity
        self.report_content, self.on_close = content, on_close
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        completed = False
        async def observed_send(message):
            nonlocal completed
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
        try:
            await super().__call__(scope, receive, observed_send)
        finally:
            def close():
                try:
                    self.report_content.close()
                finally:
                    if self.on_close:
                        self.on_close()
            try:
                # ASGI disconnect/cancellation must kill the already-started
                # executor even if the response generator was never consumed.
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(close)
            finally:
                if completed:
                    terminal(self.identity, status="SUCCESS", generation="COMPLETE", transmission="COMPLETE")
                else:
                    terminal(self.identity, status="INTERRUPTED", transmission="INTERRUPTED")


@router.post("/download")
def download(body: DownloadBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    context = resolved_context(db, body.context_id, user)
    run = start_execution(db, user, context, "DOWNLOAD")
    identity = run.id
    try:
        admission(db, run)
        inputs, plan = execution_sources(db, context), context.snapshot["plan"]
        db.commit()
        messages = execute_messages(inputs, plan, "XLSX" if body.format == "XLSX" else "DOWNLOAD",
                                    check=lambda: check_execution(identity, "reports:download"))
        columns, cardinality = [], []
        for message in messages:
            if message["kind"] == "cardinality":
                cardinality = message["items"]
            elif message["kind"] == "schema":
                columns = message["columns"]
                break
        check_execution(identity, "reports:download")
    except OperationError as exc:
        terminal(identity, status="FAILED", error=exc, generation="FAILED", transmission="NOT_STARTED")
        raise

    def batches():
        try:
            for message in messages:
                check_execution(identity, "reports:download")
                if message["kind"] == "batch":
                    yield message["rows"]
                elif message["kind"] == "complete":
                    with SessionLocal() as session:
                        item = session.get(ReportExecution, identity)
                        if item is None:
                            raise OperationError(404, "REPORT_EXECUTION_NOT_FOUND", "La ejecución ya no existe.")
                        item.generation_status = "COMPLETE"
                        item.metrics = {**{k: v for k, v in message.items() if k != "kind"}, "cardinality": cardinality}
                        session.commit()
        except OperationError as exc:
            terminal(identity, status="FAILED", error=exc, generation="FAILED", transmission="INTERRUPTED")
            raise
        finally:
            messages.close()

    def content():
        count = 0
        limits = ReportLimits.configured("XLSX" if body.format == "XLSX" else "DOWNLOAD")
        chunks = csv_stream(columns, batches()) if body.format == "CSV" else xlsx_stream(columns, batches())
        try:
            for chunk in chunks:
                check_execution(identity, "reports:download")
                count += len(chunk)
                if count > limits.max_bytes:
                    raise OperationError(422, "REPORT_DOWNLOAD_BYTES", "La serialización excedió los bytes autorizados.")
                yield chunk
        except OperationError as exc:
            terminal(identity, status="FAILED", error=exc, generation="FAILED", transmission="INTERRUPTED")
            raise
        finally:
            chunks.close()
            messages.close()
    extension = body.format.lower()
    return ReportStreamingResponse(content(), identity, on_close=messages.close,
        media_type="text/csv; charset=utf-8" if body.format == "CSV" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="reporte-{identity}.{extension}"',
                 "Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Report-Execution-Id": identity,
                 "X-Report-Serialization": "CSV_ESCAPED_TEXT_V1" if body.format == "CSV" else "XLSX_EXACT_TEXT_V1"})


@router.post("/datasets")
def generate(body: DatasetBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    context = resolved_context(db, body.context_id, user)
    publication = body.model_dump(exclude={"context_id", "idempotency_key"})
    publication["name"] = publication["name"].strip()
    if not publication["name"]:
        raise OperationError(422, "DATASET_NAME_INVALID", "Escribe el nombre del nuevo dataset.")
    validate_classification(db, user.organization_id, body.macro_domain_id, body.domain_id)
    existing = db.scalar(select(ReportExecution).where(ReportExecution.organization_id == user.organization_id,
        ReportExecution.user_id == user.id, ReportExecution.idempotency_key == body.idempotency_key))
    if not existing and db.scalar(select(Dataset.id).where(Dataset.organization_id == user.organization_id, Dataset.name == publication["name"])):
        raise OperationError(409, "DATASET_NAME_CONFLICT", "Ya existe un dataset con ese nombre; la generación siempre crea uno nuevo.")
    for field in ("business_owner_id", "steward_id", "technical_custodian_id"):
        if publication[field]:
            responsible = owned(db, User, publication[field], user)
            if not responsible.active or responsible.deleted:
                raise OperationError(422, "RESPONSIBLE_INVALID", "Selecciona responsables activos de tu organización.")
    try:
        item = start_execution(db, user, context, "DATASET", publication, body.idempotency_key)
    except IntegrityError:
        db.rollback()
        replay = db.scalar(select(ReportExecution).where(ReportExecution.organization_id == user.organization_id,
            ReportExecution.user_id == user.id, ReportExecution.idempotency_key == body.idempotency_key))
        if replay is None or replay.request_hash != digest({"context_id": context.id, "publication": publication}):
            raise OperationError(409, "IDEMPOTENCY_CONFLICT", "La clave ya corresponde a otra solicitud.") from None
        item = replay
    return execution_dto(db, item)


@router.post("/executions/{identity}/cancel")
def cancel(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, ReportExecution, identity, user)
    if item.user_id != user.id:
        raise OperationError(403, "REPORT_EXECUTION_OWNER", "Solo el iniciador puede cancelar esta generación.")
    if item.profile != "DATASET":
        raise OperationError(422, "REPORT_CANCEL_PROFILE", "Las previews y descargas se interrumpen con su consumidor.")
    if item.status in {"QUEUED", "RUNNING"}:
        item.cancel_requested = True
        db.commit()
    return execution_dto(db, item)
