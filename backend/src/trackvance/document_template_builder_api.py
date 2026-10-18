"""Visual builder uses the native immutable originals, templates and DOCUMENT lane."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .document_models import DocumentExtraction, DocumentFamily, DocumentOriginal
from .document_schemas import DocumentDefinition
from .document_service import (
    extraction_for,
    original_dto,
    owned,
    permission,
)
from .document_template_builder import propose
from .document_template_builder_schemas import BuilderProposal
from .document_workspace_api import router as workspace_router
from .models import User
from .operations_common import OperationError

router = APIRouter()
router.include_router(workspace_router)


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


@router.get("/originals/{identity}/builder")
def geometry(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    original, extraction = example_extraction(db, identity, user)
    return {"document": original_dto(db, original, detail=True), "lines": extraction.payload.get("lines", []),
            "pages": extraction.payload.get("pages", [])}


@router.post("/originals/{identity}/builder/propose")
def proposal(identity: str, body: BuilderProposal, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, extraction = example_extraction(db, identity, user)
    return propose(extraction.payload.get("lines", []), body)


class LegacyBuilderTest(BaseModel):
    document_id: str
    definition: DocumentDefinition


@router.post("/families/{identity}/builder/test")
def legacy_test(identity: str, body: LegacyBuilderTest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Retain the historical route without recreating revision-per-attempt behavior."""
    authority(db, user)
    family = owned(db, DocumentFamily, identity, user)
    raise OperationError(409, "DOCUMENT_BUILDER_DRAFT_REQUIRED",
        "Para probar esta configuración, crea o selecciona un borrador de la familia y prueba todas sus referencias. La revisión formal se crea al aprobar y activar.",
        {"action": "CREATE_OR_SELECT_DRAFT", "drafts_path": f"/api/v1/documents/families/{family.id}/builder/drafts",
         "family_href": f"/documents/families/{family.id}?view=references"})
