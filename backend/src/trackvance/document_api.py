"""Operational document API with independent original/review/publication grants."""
import base64
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .artifactstore import link_artifact, storage_provider
from .db import get_db, utcnow
from .document_header_review import HeaderConfirmation
from .document_models import (
    DocumentBatch,
    DocumentBuilderTest,
    DocumentFamily,
    DocumentOriginal,
    DocumentTemplate,
)
from .document_queries import DocumentOrder, PublicationFilter, list_originals
from .document_schemas import DocumentDefinition
from .document_service import (
    MAX_BYTES,
    activate_template,
    batch_dto,
    create_template,
    dispatch_individual,
    family_dto,
    lock_family,
    open_batch,
    original_dto,
    owned,
    permission,
    prepare_pipeline,
    publish_durable,
    review,
    template_dto,
)
from .document_template_builder_api import router as visual_router
from .models import Artifact, ArtifactLink, Job, User
from .operations_common import OperationError
from .services import audit

router = APIRouter(prefix="/api/v1/documents", tags=["Documentos"])
router.include_router(visual_router)
_preview_slot = threading.BoundedSemaphore(1)


def current_user(request: Request):
    return request.state.user


class FamilyBody(BaseModel):
    name: str = Field(min_length=1, max_length=140)
    description: str = Field(default="", max_length=5000)


class ActivateBody(BaseModel):
    test_extraction_id: str


class ReviewBody(BaseModel):
    expected_extraction_id: str
    changes: dict[str, str | None] = Field(default_factory=dict, max_length=10000)
    confirmed: list[str] = Field(default_factory=list, max_length=10000)
    reason: str = Field(min_length=1, max_length=2000)
    table_rows: dict[str, list[dict[str, str | None]]] = Field(default_factory=dict, max_length=10)
    header_confirmations: list[HeaderConfirmation] = Field(default_factory=list, max_length=50)


class BatchBody(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    template_id: str
    document_ids: list[str] = Field(min_length=1, max_length=1000)
    correction: bool = False


class PipelineBody(BaseModel):
    stop_after: Literal["PUBLICATION", "VALIDATION", "DELIVERY"] = "VALIDATION"
    intake_bindings: dict[str, str] = Field(default_factory=dict)
    delivery_bindings: dict[str, str] = Field(default_factory=dict)
    automatic_delivery: bool = False


@router.get("/limits")
def limits():
    from .document_builder_executor import MAX_BYTES as BUILDER_MAX_BYTES
    from .document_builder_executor import MAX_REFERENCES, MEMORY_BYTES, TIMEOUT_SECONDS
    from .document_engine import ARITHMETIC_PRECISION_LIMIT
    return {"file_bytes": MAX_BYTES, "pages": 50, "image_pixels": 20000000, "timeout_seconds": 120,
            "batch_documents": 1000, "batch_rows_per_document": 10000,
            "builder": {"timeout_seconds": TIMEOUT_SECONDS, "input_bytes": BUILDER_MAX_BYTES, "output_bytes": BUILDER_MAX_BYTES,
                "references": MAX_REFERENCES, "process_memory_bytes": MEMORY_BYTES,
                "arithmetic_precision_digits": ARITHMETIC_PRECISION_LIMIT},
            "formats": ["PDF", "PNG", "JPG"], "engine": "POPPLER_TESSERACT_CPU_V1"}


@router.get("/families")
def families(search: str = Query("", max_length=140), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
             db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(DocumentFamily).where(DocumentFamily.organization_id == user.organization_id,
                                         DocumentFamily.name.ilike("%" + search + "%"))
    return {"items": [family_dto(db, f) for f in db.scalars(query.order_by(DocumentFamily.name).offset(offset).limit(limit))],
            "total": db.scalar(select(func.count()).select_from(query.subquery()))}


@router.post("/families")
def new_family(body: FamilyBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    family = DocumentFamily(organization_id=user.organization_id, name=body.name.strip(), description=body.description,
                            responsible_user_id=user.id)
    if not family.name:
        raise OperationError(422, "DOCUMENT_FAMILY_NAME", "Indica un nombre de familia.")
    if db.scalar(select(DocumentFamily.id).where(DocumentFamily.organization_id == user.organization_id, DocumentFamily.name == family.name)):
        raise OperationError(409, "DOCUMENT_FAMILY_EXISTS", "Ya existe una familia con ese nombre.")
    db.add(family)
    db.flush()
    audit(db, "DOCUMENT_FAMILY_CREATED", "document_family", family.id, "Familia documental creada", user.name, user.organization_id)
    db.commit()
    return family_dto(db, family)


@router.get("/families/{identity}")
def family(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentFamily, identity, user)
    return {**family_dto(db, item), "templates": [template_dto(t) for t in db.scalars(select(DocumentTemplate)
            .where(DocumentTemplate.family_id == item.id).order_by(DocumentTemplate.revision.desc()))]}


@router.post("/families/{identity}/templates")
def template(identity: str, body: DocumentDefinition, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = create_template(db, owned(db, DocumentFamily, identity, user), body.model_dump(mode="json"), user)
    db.commit()
    return template_dto(item)


@router.post("/families/{identity}/pipeline")
def pipeline(identity: str, body: PipelineBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentFamily, identity, user)
    template = prepare_pipeline(db, item, user, body.stop_after, body.intake_bindings, body.delivery_bindings, body.automatic_delivery)
    db.commit()
    return {"template": template_dto(template), "next_action": "TEST_EXAMPLE_AND_ACTIVATE", "intake_configuration_ids": template.definition["publication"]["intake_configuration_ids"]}


@router.post("/families/{identity}/pipeline-draft")
def pipeline_draft(identity: str, body: PipelineBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .document_builder_workspace import draft_dto
    item = owned(db, DocumentFamily, identity, user)
    draft = prepare_pipeline(db, item, user, body.stop_after, body.intake_bindings, body.delivery_bindings, body.automatic_delivery, as_draft=True)
    db.commit()
    return {"draft": draft_dto(draft), "next_action": "SELECT_REFERENCES_TEST_AND_ACTIVATE"}


@router.post("/families/{identity}/templates/{template_id}/activate")
def activate(identity: str, template_id: str, body: ActivateBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentFamily, identity, user)
    activate_template(db, item, owned(db, DocumentTemplate, template_id, user), body.test_extraction_id, user)
    db.commit()
    return family_dto(db, item)


@router.get("/originals")
def originals(family_id: str | None = None, search: str = Query("", max_length=240), status: str | None = None,
              offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
              received_from: datetime | None = None, received_to: datetime | None = None,
              mode: Literal["INDIVIDUAL", "SCHEDULED"] | None = None,
              publication: PublicationFilter | None = None, order_by: DocumentOrder = "received_at",
              direction: Literal["asc", "desc"] = "desc",
              purpose: Literal["OPERATIONAL", "LEGACY_REFERENCE"] | None = None,
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    return list_originals(db, user, family_id=family_id, search=search, status=status,
        received_from=received_from, received_to=received_to, mode=mode, publication=publication,
        order_by=order_by, direction=direction, offset=offset, limit=limit, purpose=purpose)


@router.post("/families/{identity}/originals")
async def upload(identity: str, files: list[UploadFile] = File(...), template_id: str | None = Form(None),
                 is_example: bool = Form(False), correction_of_id: str | None = Form(None),
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentFamily, identity, user)
    if not 1 <= len(files) <= 20:
        raise OperationError(422, "DOCUMENT_UPLOAD_COUNT", "Recibe de 1 a 20 archivos por solicitud.")
    lock_family(db, item)
    selected = owned(db, DocumentTemplate, template_id or item.active_template_id or "", user)
    if selected.family_id != item.id or (not is_example and selected.id != item.active_template_id):
        raise OperationError(422, "DOCUMENT_TEMPLATE_ACTIVE", "Las recepciones usan la revisión activa de su familia.")
    if is_example:
        permission(db, user, "configure")
    if correction_of_id:
        prior = owned(db, DocumentOriginal, correction_of_id, user)
        if prior.family_id != item.id:
            raise OperationError(422, "DOCUMENT_CORRECTION_FAMILY", "La corrección debe pertenecer a la misma familia.")
    result = []
    total = 0
    for file in files:
        filename = (file.filename or "documento").replace("\\", "/").split("/")[-1][:240]
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".png", ".jpg", ".jpeg"}:
            raise OperationError(422, "DOCUMENT_FORMAT", "Recibe PDF, PNG o JPG.")
        temporary = storage_provider.temporary_path(suffix)
        try:
            count = 0
            with temporary.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    count += len(chunk)
                    total += len(chunk)
                    if count > MAX_BYTES or total > 64 * 1024**2:
                        raise OperationError(413, "DOCUMENT_BYTES_LIMIT", "El archivo excede 32 MiB o la solicitud supera 64 MiB.")
                    stream.write(chunk)
            with temporary.open("rb") as stream:
                header = stream.read(12)
            if count == 0 or (suffix == ".pdf" and not header.startswith(b"%PDF-")) or (suffix == ".png" and not header.startswith(b"\x89PNG\r\n\x1a\n")) or (suffix in {".jpg", ".jpeg"} and not header.startswith(b"\xff\xd8")):
                raise OperationError(422, "DOCUMENT_FORMAT_INVALID", "El contenido no coincide con el formato declarado.")
            artifact = storage_provider.put_file(db, temporary, "DOCUMENT_ORIGINAL", user.organization_id, filename,
                media_type="application/pdf" if suffix == ".pdf" else "image/png" if suffix == ".png" else "image/jpeg")
            duplicate = None if is_example else db.scalar(select(DocumentOriginal).where(DocumentOriginal.family_id == item.id,
                DocumentOriginal.sha256 == artifact.sha256, DocumentOriginal.is_example.is_(False)).order_by(DocumentOriginal.received_at).limit(1))
            original = DocumentOriginal(organization_id=user.organization_id, family_id=item.id, template_id=selected.id,
                artifact_id=artifact.id, filename=filename, sha256=artifact.sha256, received_at=utcnow(), uploaded_by_id=user.id,
                is_example=is_example, duplicate_of_id=duplicate.id if duplicate else None, correction_of_id=correction_of_id,
                status="DUPLICATE_FILE" if duplicate else "QUEUED")
            db.add(original)
            db.flush()
            if not duplicate:
                db.add(Job(organization_id=user.organization_id, document_id=original.id, lane="DOCUMENT"))
            link_artifact(db, user.organization_id, "DOCUMENT_ORIGINAL", "DOCUMENT", original.id, "ARTIFACT", artifact.id)
            audit(db, "DOCUMENT_RECEIVED", "document", original.id, "Recepción completa del original inmutable", user.name,
                  user.organization_id, {"family_id": item.id, "template_id": selected.id, "sha256": artifact.sha256,
                                         "duplicate_of_id": original.duplicate_of_id, "is_example": is_example})
            result.append(original_dto(db, original))
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()
    db.commit()
    return {"items": result, "total": len(result)}


@router.get("/originals/{identity}")
def original(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return original_dto(db, owned(db, DocumentOriginal, identity, user), detail=True)


@router.get("/originals/{identity}/file")
def file(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentOriginal, identity, user)
    artifact = owned(db, Artifact, item.artifact_id, user)
    path = storage_provider.materialize(artifact)
    audit(db, "DOCUMENT_ORIGINAL_ACCESSED", "document", item.id, "Original consultado con permiso independiente", user.name,
          user.organization_id, {"artifact_id": artifact.id, "sha256": artifact.sha256})
    db.commit()
    return FileResponse(path, media_type=artifact.media_type, filename=item.filename, content_disposition_type="inline")


@router.get("/originals/{identity}/pages/{page}")
async def page_preview(identity: str, page: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .document_worker import run_engine
    item = owned(db, DocumentOriginal, identity, user)
    if not 1 <= page <= 50 or not item.filename.lower().endswith(".pdf"):
        raise OperationError(422, "DOCUMENT_PAGE_LIMIT", "Selecciona una página PDF de 1 a 50.")
    relation = f"DOCUMENT_PAGE_{page}"
    found = db.scalar(select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
        ArtifactLink.source_type == "DOCUMENT", ArtifactLink.source_id == item.id, ArtifactLink.relation == relation))
    if found:
        artifact = owned(db, Artifact, found.target_id, user)
    else:
        if not _preview_slot.acquire(blocking=False):
            raise OperationError(429, "DOCUMENT_PREVIEW_BUSY", "La vista previa local está ocupada; reintenta en unos segundos.")
        temporary = storage_provider.temporary_path(".jpg")
        try:
            original = owned(db, Artifact, item.artifact_id, user)
            source = storage_provider.materialize(original)
            # No DB transaction is held while the confined child renders.
            db.rollback()
            result = await run_in_threadpool(run_engine, source, {}, preview_page=page)
            user = owned(db, User, user.id, user)
            db.refresh(user)
            permission(db, user, "download")
            temporary.write_bytes(base64.b64decode(result["preview"], validate=True))
            artifact = storage_provider.put_file(db, temporary, "DOCUMENT_PREVIEW", user.organization_id,
                                                 f"page-{page}.jpg", media_type="image/jpeg")
            link_artifact(db, user.organization_id, relation, "DOCUMENT", item.id, "ARTIFACT", artifact.id)
        finally:
            temporary.unlink(missing_ok=True)
            _preview_slot.release()
    audit(db, "DOCUMENT_PAGE_ACCESSED", "document", item.id, "Página original consultada bajo demanda", user.name,
          user.organization_id, {"page": page, "artifact_id": artifact.id})
    db.commit()
    return FileResponse(storage_provider.materialize(artifact), media_type="image/jpeg")


@router.post("/originals/{identity}/review")
def review_original(identity: str, body: ReviewBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentOriginal, identity, user)
    review(db, item, body.expected_extraction_id, body.changes, body.confirmed, body.reason, user, body.table_rows,
           [value.model_dump() for value in body.header_confirmations])
    # A successful human review remains durable even if a separate publication is blocked.
    db.commit()
    try:
        dispatch_individual(db, item, user)
        db.commit()
    except OperationError:
        db.rollback()
    return original_dto(db, item, detail=True)


@router.post("/originals/{identity}/cancel")
def cancel(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentOriginal, identity, user)
    if item.status not in {"QUEUED", "RUNNING"}:
        raise OperationError(409, "DOCUMENT_FINISHED", "La extracción ya terminó.")
    item.cancel_requested = True
    if item.status == "QUEUED":
        item.status, item.finished_at = "CANCELLED", utcnow()
        job = db.scalar(select(Job).where(Job.document_id == item.id))
        if job is None:
            raise OperationError(409, "DOCUMENT_JOB_MISSING", "No se puede verificar la ejecución documental.")
        job.status = "CANCELLED"
    audit(db, "DOCUMENT_CANCEL_REQUESTED", "document", item.id, "Cancelación documental solicitada", user.name, user.organization_id)
    db.commit()
    return original_dto(db, item)


@router.post("/originals/{identity}/retry")
def retry(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentOriginal, identity, user)
    lock_family(db, owned(db, DocumentFamily, item.family_id, user))
    if item.status not in {"FAILED", "CANCELLED"} or item.extraction_id:
        raise OperationError(409, "DOCUMENT_RETRY_STATE", "Solo se reintentan extracciones fallidas o canceladas sin resultado publicado.")
    item.status, item.cancel_requested, item.error_code = "QUEUED", False, None
    job = db.scalar(select(Job).where(Job.document_id == item.id))
    if job is None:
        raise OperationError(409, "DOCUMENT_JOB_MISSING", "No se puede verificar la ejecución documental.")
    job.status, job.lease_owner, job.lease_until = "QUEUED", None, None
    audit(db, "DOCUMENT_RETRY_QUEUED", "document", item.id, "Reintento con plantilla y original congelados", user.name, user.organization_id)
    db.commit()
    return original_dto(db, item)


@router.get("/families/{identity}/batches")
def batches(identity: str, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned(db, DocumentFamily, identity, user)
    query = select(DocumentBatch).where(DocumentBatch.family_id == identity)
    return {"items": [batch_dto(db, b) for b in db.scalars(query.order_by(DocumentBatch.created_at.desc()).offset(offset).limit(limit))],
            "total": db.scalar(select(func.count()).select_from(query.subquery()))}


@router.post("/families/{identity}/batches")
def new_batch(identity: str, body: BatchBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned(db, DocumentFamily, identity, user)
    template = owned(db, DocumentTemplate, body.template_id, user)
    verified = bool(template.tested_extraction_id)
    if template.family_id == item.id and not verified:
        # Native builder activation records its independent reference proof here;
        # it deliberately never fabricates a legacy operational example.
        tested = db.scalar(select(DocumentBuilderTest).where(
            DocumentBuilderTest.organization_id == user.organization_id,
            DocumentBuilderTest.activated_template_id == template.id))
        if tested is not None:
            from .document_builder_workspace import test_dto

            evidence = test_dto(tested)
            verified = evidence["snapshot"]["definition"] == template.definition and not any(evidence["issues"].values())
    if template.family_id != item.id or not verified:
        raise OperationError(422, "DOCUMENT_TEMPLATE_TEST_REQUIRED", "El lote requiere una plantilla probada de esta familia.")
    batch = open_batch(db, item, template, user, "manual:" + body.idempotency_key, document_ids=body.document_ids, correction=body.correction)
    publish_durable(db, batch, user)
    db.commit()
    return batch_dto(db, batch)


@router.get("/batches/{identity}")
def batch(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return batch_dto(db, owned(db, DocumentBatch, identity, user))


@router.post("/batches/{identity}/publish")
def publish(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = publish_durable(db, owned(db, DocumentBatch, identity, user), user)
    db.commit()
    return batch_dto(db, item)
