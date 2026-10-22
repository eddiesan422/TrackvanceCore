"""References and drafts never allocate formal revisions or operational receipts."""
import base64
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .artifactstore import link_artifact, storage_provider
from .db import get_db
from .document_builder_workspace import (
    activate_test,
    authority,
    create_draft,
    discover_draft,
    draft_dto,
    reference_dto,
    review_test,
    save_draft,
    test_draft,
    test_dto,
)
from .document_header_review import HeaderConfirmation
from .document_models import DocumentBuilderTest, DocumentDraft, DocumentFamily, DocumentReference
from .document_schemas import DocumentDraftDefinition
from .document_service import MAX_BYTES, lock_family, owned
from .document_template_builder import propose
from .document_template_builder_schemas import BuilderProposal
from .models import Artifact, ArtifactLink, Job, User
from .operations_common import OperationError
from .services import audit

router = APIRouter()
_preview_slot = threading.BoundedSemaphore(1)


def current_user(request: Request):
    return request.state.user


async def store_reference(db, family, file, user, role, association, budget):
    filename = (file.filename or "documento").replace("\\", "/").split("/")[-1][:240]
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg"}:
        raise OperationError(422, "DOCUMENT_FORMAT", "Recibe PDF, PNG o JPG.")
    temporary = storage_provider.temporary_path(suffix)
    try:
        size = 0
        with temporary.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                budget[0] += len(chunk)
                if size > MAX_BYTES or budget[0] > 64 * 1024**2:
                    raise OperationError(413, "DOCUMENT_BYTES_LIMIT", "Máximo 32 MiB por archivo y 64 MiB por solicitud.")
                stream.write(chunk)
        with temporary.open("rb") as stream:
            header = stream.read(12)
        magic = b"%PDF-" if suffix == ".pdf" else b"\x89PNG\r\n\x1a\n" if suffix == ".png" else b"\xff\xd8"
        if not size or not header.startswith(magic):
            raise OperationError(422, "DOCUMENT_FORMAT_INVALID", "El contenido no coincide con el formato declarado.")
        artifact = storage_provider.put_file(db, temporary, "DOCUMENT_REFERENCE", user.organization_id, filename,
            media_type="application/pdf" if suffix == ".pdf" else "image/png" if suffix == ".png" else "image/jpeg")
        item = DocumentReference(organization_id=user.organization_id, family_id=family.id, artifact_id=artifact.id,
            filename=filename, sha256=artifact.sha256, actor_id=user.id, role=role, association=association)
        db.add(item)
        db.flush()
        db.add(Job(organization_id=user.organization_id, document_reference_id=item.id, lane="DOCUMENT"))
        link_artifact(db, user.organization_id, "DOCUMENT_REFERENCE_ORIGINAL", "DOCUMENT_REFERENCE", item.id, "ARTIFACT", artifact.id)
        audit(db, "DOCUMENT_REFERENCE_CREATED", "document_reference", item.id, "Referencia independiente recibida para configurar", user.name,
              user.organization_id, {"family_id": family.id, "sha256": artifact.sha256, "role": role, "association_confirmed": association})
        return reference_dto(item)
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()


@router.post("/families/{identity}/builder/examples")
async def example(identity: str, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    result = await store_reference(db, owned(db, DocumentFamily, identity, user), file, user, "CONFIGURATION", None, [0])
    db.commit()
    return result


@router.post("/families/{identity}/builder/references")
async def upload_references(identity: str, files: list[UploadFile] = File(...), role: Literal["CONFIGURATION", "HOLDOUT"] = Form("CONFIGURATION"),
    association: str | None = Form(None, max_length=120), db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    family = owned(db, DocumentFamily, identity, user)
    if not 1 <= len(files) <= 20:
        raise OperationError(422, "DOCUMENT_UPLOAD_COUNT", "Selecciona de 1 a 20 referencias.")
    budget = [0]
    result = []
    try:
        for file in files:
            result.append(await store_reference(db, family, file, user, role, association, budget))
        db.commit()
    finally:
        for file in files:
            await file.close()
    return {"items": result, "total": len(result)}


@router.get("/families/{identity}/builder/references")
def references(identity: str, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    owned(db, DocumentFamily, identity, user)
    query = select(DocumentReference).where(DocumentReference.organization_id == user.organization_id, DocumentReference.family_id == identity)
    return {"items": [reference_dto(item) for item in db.scalars(query.order_by(DocumentReference.created_at).offset(offset).limit(limit))],
            "total": db.scalar(select(func.count()).select_from(query.subquery()))}


@router.get("/builder/references/{identity}")
def reference(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    return reference_dto(owned(db, DocumentReference, identity, user), detail=True)


@router.post("/builder/references/{identity}/propose")
def proposal(identity: str, body: BuilderProposal, db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    item = owned(db, DocumentReference, identity, user)
    lock_family(db, owned(db, DocumentFamily, item.family_id, user))
    db.refresh(item)
    if item.role == "HOLDOUT":
        raise OperationError(422, "DOCUMENT_HOLDOUT_CONFIGURATION", "La variante reservada solo puede probarse. Usa otra referencia para ajustar reglas.")
    data = reference_dto(item, detail=True)
    if not data["payload"]:
        raise OperationError(409, "DOCUMENT_BUILDER_PENDING", "Espera a que termine la lectura local.")
    result = propose(data["payload"]["lines"], body)
    item.configuration_used = True
    audit(db, "DOCUMENT_REFERENCE_RULE_PROPOSED", "document_reference", item.id, "Referencia utilizada para configurar una regla", user.name, user.organization_id)
    db.commit()
    return result


class ReferenceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["CONFIGURATION", "HOLDOUT"]
    association: str | None = Field(default=None, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


@router.patch("/builder/references/{identity}")
def configure_reference(identity: str, body: ReferenceSettings, db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    item = owned(db, DocumentReference, identity, user)
    lock_family(db, owned(db, DocumentFamily, item.family_id, user))
    db.refresh(item)
    if body.role == "HOLDOUT" and item.configuration_used:
        raise OperationError(422, "DOCUMENT_HOLDOUT_CONFIGURATION", "Esta referencia ya se utilizó para configurar y no puede presentarse como variante reservada.")
    item.role, item.association = body.role, body.association
    audit(db, "DOCUMENT_REFERENCE_CLASSIFIED", "document_reference", item.id, "Uso y asociación confirmados por una persona", user.name,
          user.organization_id, body.model_dump())
    db.commit()
    return reference_dto(item)


@router.get("/builder/references/{identity}/file")
def reference_file(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    item = owned(db, DocumentReference, identity, user)
    artifact = owned(db, Artifact, item.artifact_id, user)
    audit(db, "DOCUMENT_REFERENCE_ACCESSED", "document_reference", item.id, "Original de referencia consultado", user.name, user.organization_id)
    db.commit()
    return FileResponse(storage_provider.materialize(artifact), media_type=artifact.media_type, filename=item.filename, content_disposition_type="inline")


@router.get("/builder/references/{identity}/pages/{page}")
async def reference_page(identity: str, page: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .document_worker import run_engine
    authority(db, user)
    item = owned(db, DocumentReference, identity, user)
    if not 1 <= page <= 50 or not item.filename.lower().endswith(".pdf"):
        raise OperationError(422, "DOCUMENT_PAGE_LIMIT", "Selecciona una página PDF de 1 a 50.")
    relation = f"DOCUMENT_REFERENCE_PAGE_{page}"
    found = db.scalar(select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
        ArtifactLink.source_type == "DOCUMENT_REFERENCE", ArtifactLink.source_id == identity, ArtifactLink.relation == relation))
    if found:
        artifact = owned(db, Artifact, found.target_id, user)
    else:
        if not _preview_slot.acquire(blocking=False):
            raise OperationError(429, "DOCUMENT_PREVIEW_BUSY", "El visor local está ocupado. Reintenta en unos segundos.")
        temporary = storage_provider.temporary_path(".jpg")
        try:
            source = storage_provider.materialize(owned(db, Artifact, item.artifact_id, user))
            db.rollback()
            result = await run_in_threadpool(run_engine, source, {}, preview_page=page)
            user = owned(db, User, user.id, user)
            db.refresh(user)
            authority(db, user)
            owned(db, DocumentReference, identity, user)
            temporary.write_bytes(base64.b64decode(result["preview"], validate=True))
            artifact = storage_provider.put_file(db, temporary, "DOCUMENT_PREVIEW", user.organization_id, f"reference-page-{page}.jpg", media_type="image/jpeg")
            link_artifact(db, user.organization_id, relation, "DOCUMENT_REFERENCE", identity, "ARTIFACT", artifact.id)
            db.commit()
        finally:
            temporary.unlink(missing_ok=True)
            _preview_slot.release()
    return FileResponse(storage_provider.materialize(artifact), media_type="image/jpeg")


class NewDraft(BaseModel):
    name: str = Field(default="Borrador", min_length=1, max_length=140)
    source_template_id: str | None = None


class SaveDraft(BaseModel):
    expected_version: int = Field(ge=1)
    definition: DocumentDraftDefinition
    reference_ids: list[str] = Field(default_factory=list, max_length=20)
    name: str = Field(default="Borrador", min_length=1, max_length=140)


class ExpectedVersion(BaseModel):
    expected_version: int = Field(ge=1)


@router.get("/families/{identity}/builder/drafts")
def drafts(identity: str, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    owned(db, DocumentFamily, identity, user)
    query = select(DocumentDraft).where(DocumentDraft.organization_id == user.organization_id, DocumentDraft.family_id == identity)
    return {"items": [draft_dto(item) for item in db.scalars(query.order_by(DocumentDraft.created_at.desc()).offset(offset).limit(limit))],
            "total": db.scalar(select(func.count()).select_from(query.subquery()))}


@router.post("/families/{identity}/builder/drafts")
def new_draft(identity: str, body: NewDraft, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = create_draft(db, identity, user, body.source_template_id, body.name)
    db.commit()
    return draft_dto(item)


@router.get("/builder/drafts/{identity}")
def draft(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    item = owned(db, DocumentDraft, identity, user)
    latest = db.scalar(select(DocumentBuilderTest).where(DocumentBuilderTest.draft_id == item.id,
        DocumentBuilderTest.draft_version == item.version).order_by(DocumentBuilderTest.created_at.desc()).limit(1))
    return {**draft_dto(item), "latest_test": test_dto(latest) if latest else None}


@router.put("/builder/drafts/{identity}")
def save(identity: str, body: SaveDraft, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = save_draft(db, owned(db, DocumentDraft, identity, user), body.expected_version, body.definition.model_dump(mode="json"), body.reference_ids, body.name, user)
    db.commit()
    return draft_dto(item)


@router.post("/builder/drafts/{identity}/discover")
def discovery(identity: str, body: ExpectedVersion, db: Session = Depends(get_db), user: User = Depends(current_user)):
    result = discover_draft(db, owned(db, DocumentDraft, identity, user), body.expected_version, user)
    db.commit()
    return result


@router.post("/builder/drafts/{identity}/test")
def test(identity: str, body: ExpectedVersion, db: Session = Depends(get_db), user: User = Depends(current_user)):
    result = test_draft(db, owned(db, DocumentDraft, identity, user), body.expected_version, user)
    db.commit()
    return test_dto(result)


class ReviewTest(BaseModel):
    expected_version: int = Field(ge=1)
    reference_id: str
    changes: dict[str, str | None] = Field(default_factory=dict, max_length=10000)
    confirmed: list[str] = Field(default_factory=list, max_length=10000)
    reason: str = Field(min_length=1, max_length=2000)
    header_confirmations: list[HeaderConfirmation] = Field(default_factory=list, max_length=50)


@router.post("/builder/tests/{identity}/review")
def review(identity: str, body: ReviewTest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = review_test(db, owned(db, DocumentBuilderTest, identity, user), body.reference_id, body.expected_version, body.changes, body.confirmed, body.reason, user,
                       [value.model_dump() for value in body.header_confirmations])
    db.commit()
    return test_dto(item)


@router.post("/builder/tests/{identity}/activate")
def activate(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    result = activate_test(db, owned(db, DocumentBuilderTest, identity, user), user)
    db.commit()
    return result
