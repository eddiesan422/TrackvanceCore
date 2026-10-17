"""Visual builder uses the native immutable originals, templates and DOCUMENT lane."""
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .artifactstore import link_artifact, storage_provider
from .db import get_db, utcnow
from .document_models import DocumentExtraction, DocumentFamily, DocumentOriginal
from .document_schemas import DocumentDefinition
from .document_service import (
    MAX_BYTES,
    create_template,
    extraction_for,
    lock_family,
    original_dto,
    owned,
    permission,
)
from .document_template_builder import propose
from .document_template_builder_schemas import BuilderProposal
from .models import Artifact, Job, User
from .operations_common import OperationError
from .services import audit

router = APIRouter()


def current_user(request: Request):
    return request.state.user


def authority(db, user):
    for action in ("configure", "download", "write"):
        permission(db, user, action)


def example_extraction(db, identity, user):
    authority(db, user)
    original = owned(db, DocumentOriginal, identity, user)
    owned(db, DocumentFamily, original.family_id, user)
    extraction = extraction_for(db, original)
    if not original.is_example:
        raise OperationError(422, "DOCUMENT_BUILDER_EXAMPLE", "Usa un documento de ejemplo de esta familia.")
    if not extraction:
        raise OperationError(409, "DOCUMENT_BUILDER_PENDING", "Espera a que termine la extracción local del ejemplo.")
    owned(db, DocumentExtraction, extraction.id, user)
    if extraction.document_id != original.id:
        raise OperationError(409, "DOCUMENT_EXTRACTION_INTEGRITY", "La extracción no pertenece al ejemplo.")
    return original, extraction


def queue_example(db, family, template, artifact, filename, user, *, previous=None):
    original = DocumentOriginal(organization_id=user.organization_id, family_id=family.id, template_id=template.id,
        artifact_id=artifact.id, filename=filename, sha256=artifact.sha256, received_at=utcnow(), uploaded_by_id=user.id,
        is_example=True, status="QUEUED")
    db.add(original)
    db.flush()
    db.add(Job(organization_id=user.organization_id, document_id=original.id, lane="DOCUMENT"))
    link_artifact(db, user.organization_id, "DOCUMENT_ORIGINAL", "DOCUMENT", original.id, "ARTIFACT", artifact.id)
    audit(db, "DOCUMENT_BUILDER_EXAMPLE_CREATED", "document", original.id,
          "Ejemplo visual inmutable para prueba de plantilla", user.name, user.organization_id,
          {"family_id": family.id, "template_id": template.id, "artifact_id": artifact.id,
           "sha256": artifact.sha256, "previous_example_id": previous})
    db.commit()
    return original_dto(db, original)


@router.post("/families/{identity}/builder/examples")
async def example(identity: str, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    authority(db, user)
    family = owned(db, DocumentFamily, identity, user)
    lock_family(db, family)
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
                if size > MAX_BYTES:
                    raise OperationError(413, "DOCUMENT_BYTES_LIMIT", "El archivo excede 32 MiB.")
                stream.write(chunk)
        with temporary.open("rb") as stream:
            header = stream.read(12)
        magic = b"%PDF-" if suffix == ".pdf" else b"\x89PNG\r\n\x1a\n" if suffix == ".png" else b"\xff\xd8"
        if not size or not header.startswith(magic):
            raise OperationError(422, "DOCUMENT_FORMAT_INVALID", "El contenido no coincide con el formato declarado.")
        artifact = storage_provider.put_file(db, temporary, "DOCUMENT_ORIGINAL", user.organization_id, filename,
            media_type="application/pdf" if suffix == ".pdf" else "image/png" if suffix == ".png" else "image/jpeg")
        definition = DocumentDefinition.model_validate({"fields": [{"name": "por_configurar", "label": "__constructor_por_configurar__", "required": True}]})
        template = create_template(db, family, definition.model_dump(mode="json"), user)
        return queue_example(db, family, template, artifact, filename, user)
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()


@router.get("/originals/{identity}/builder")
def geometry(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    original, extraction = example_extraction(db, identity, user)
    return {"document": original_dto(db, original, detail=True), "lines": extraction.payload.get("lines", []),
            "pages": extraction.payload.get("pages", [])}


@router.post("/originals/{identity}/builder/propose")
def proposal(identity: str, body: BuilderProposal, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, extraction = example_extraction(db, identity, user)
    return propose(extraction.payload.get("lines", []), body)


class BuilderTest(BaseModel):
    document_id: str
    definition: DocumentDefinition


@router.post("/families/{identity}/builder/test")
def test(identity: str, body: BuilderTest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    family = owned(db, DocumentFamily, identity, user)
    original, _ = example_extraction(db, body.document_id, user)
    if original.family_id != family.id:
        raise OperationError(422, "DOCUMENT_BUILDER_FAMILY", "La prueba debe pertenecer a esta familia.")
    lock_family(db, family)
    artifact = owned(db, Artifact, original.artifact_id, user)
    template = create_template(db, family, body.definition.model_dump(mode="json"), user)
    return queue_example(db, family, template, artifact, original.filename, user, previous=original.id)
