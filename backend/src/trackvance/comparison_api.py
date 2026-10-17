"""Dataset versions → Compare; every evidence read rechecks both source permissions."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifactstore import storage_provider
from .comparison_models import VersionComparison
from .db import get_db, iso, utcnow
from .governance import authorize_dataset_content
from .models import Artifact, DatasetVersion, Job, User
from .operations_common import OperationError
from .services import audit
from .version_comparison import MAX_ROWS, MAX_SPILL_BYTES, TIMEOUT_SECONDS, report_rows

router = APIRouter(prefix="/api/v1", tags=["Comparación de versiones"])


def current_user(request: Request) -> User:
    return request.state.user


def authorize_comparison(db: Session, item: VersionComparison, user: User | None) -> list[DatasetVersion]:
    if user is None or not user.active or user.deleted:
        raise OperationError(403, "SOURCE_ACCESS_DENIED", "El usuario ya no tiene acceso a la comparación.")
    if item.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró la comparación.")
    versions: list[DatasetVersion] = []
    for identity in (item.before_version_id, item.after_version_id):
        version = db.get(DatasetVersion, identity)
        if version is None:
            raise OperationError(404, "NOT_FOUND", "No se encontró una versión de la comparación.")
        authorize_dataset_content(db, user, version)
        versions.append(version)
    return versions


def owned_comparison(db: Session, identity: str, user: User) -> VersionComparison:
    item = db.get(VersionComparison, identity)
    if item is None:
        raise OperationError(404, "NOT_FOUND", "No se encontró la comparación.")
    authorize_comparison(db, item, user)
    return item


def dto(item: VersionComparison) -> dict:
    return {"id": item.id, "before_version_id": item.before_version_id,
            "after_version_id": item.after_version_id, "status": item.status,
            "settings": item.settings, "progress_percent": item.progress_percent,
            "progress_stage": item.progress_stage, "summary": item.summary,
            "cancel_requested": item.cancel_requested, "artifact_id": item.artifact_id,
            "error_code": item.error_code, "error_message": item.error_message,
            "created_at": iso(item.created_at), "started_at": iso(item.started_at),
            "finished_at": iso(item.finished_at)}


class ComparisonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before_version_id: str = Field(min_length=1, max_length=64)
    after_version_id: str = Field(min_length=1, max_length=64)
    keys: list[str] = Field(default_factory=list, max_length=100)


@router.post("/datasets/{dataset_id}/comparisons", status_code=202)
def create_comparison(dataset_id: str, body: ComparisonBody, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    if body.before_version_id == body.after_version_id:
        raise OperationError(422, "COMPARISON_SAME_VERSION", "Selecciona dos versiones diferentes.")
    if len(set(body.keys)) != len(body.keys) or any(not key or len(key) > 240 for key in body.keys):
        raise OperationError(422, "COMPARISON_KEYS", "Declara claves únicas y no vacías.")
    item = VersionComparison(organization_id=user.organization_id,
        before_version_id=body.before_version_id, after_version_id=body.after_version_id,
        actor_id=user.id, settings={"keys": body.keys, "max_rows": MAX_ROWS,
            "max_spill_bytes": MAX_SPILL_BYTES, "timeout_seconds": TIMEOUT_SECONDS, "algorithm_version": 1})
    versions = authorize_comparison(db, item, user)
    if any(version.dataset_id != dataset_id for version in versions):
        raise OperationError(422, "COMPARISON_DATASET", "Las dos versiones deben pertenecer al dataset seleccionado.")
    db.add(item)
    db.flush()
    db.add(Job(organization_id=user.organization_id, run_id=None, comparison_id=item.id,
               lane="COMPARISON", status="QUEUED"))
    audit(db, "VERSION_COMPARISON_QUEUED", "version_comparison", item.id,
          "Comparación completa en cola", user.name, user.organization_id,
          {"before_version_id": item.before_version_id, "after_version_id": item.after_version_id, "keys": body.keys})
    db.commit()
    return dto(item)


@router.get("/datasets/{dataset_id}/comparisons")
def list_comparisons(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    items = db.scalars(select(VersionComparison).join(DatasetVersion,
        VersionComparison.before_version_id == DatasetVersion.id).where(
        VersionComparison.organization_id == user.organization_id,
        DatasetVersion.dataset_id == dataset_id).order_by(VersionComparison.created_at.desc()).limit(100)).all()
    for item in items:
        authorize_comparison(db, item, user)
    return {"items": [dto(item) for item in items], "total": len(items)}


@router.get("/version-comparisons/{comparison_id}")
def comparison_detail(comparison_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return dto(owned_comparison(db, comparison_id, user))


@router.get("/version-comparisons/{comparison_id}/rows")
def comparison_rows(comparison_id: str, kind: str | None = None, search: str = Query(default="", max_length=200),
                    offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned_comparison(db, comparison_id, user)
    artifact = db.get(Artifact, item.artifact_id) if item.artifact_id else None
    if item.status != "SUCCESS" or artifact is None:
        raise OperationError(409, "COMPARISON_NOT_READY", "El informe completo todavía no está disponible.")
    return report_rows(storage_provider.materialize(artifact), kind=kind, search=search, offset=offset, limit=limit)


@router.get("/version-comparisons/{comparison_id}/download")
def comparison_download(comparison_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned_comparison(db, comparison_id, user)
    artifact = db.get(Artifact, item.artifact_id) if item.artifact_id else None
    if item.status != "SUCCESS" or artifact is None:
        raise OperationError(409, "COMPARISON_NOT_READY", "El informe todavía no está disponible.")
    path = storage_provider.materialize(artifact)
    audit(db, "VERSION_COMPARISON_DOWNLOADED", "version_comparison", item.id,
          "Informe completo descargado", user.name, user.organization_id, {"sha256": artifact.sha256})
    db.commit()
    return FileResponse(path, filename=f"comparacion-{item.id}.sqlite", media_type="application/vnd.sqlite3")


@router.post("/version-comparisons/{comparison_id}/cancel")
def cancel_comparison(comparison_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = owned_comparison(db, comparison_id, user)
    # All claim, completion and cancellation paths take Job before its subject.
    job = db.scalar(select(Job).where(Job.comparison_id == item.id).with_for_update())
    db.refresh(item)
    if item.status in {"QUEUED", "RUNNING"}:
        item.cancel_requested = True
        if item.status == "QUEUED":
            item.status, item.finished_at = "CANCELLED", utcnow()
            if job:
                job.status = "CANCELLED"
        audit(db, "VERSION_COMPARISON_CANCEL_REQUESTED", "version_comparison", item.id,
              "Cancelación solicitada", user.name, user.organization_id)
        db.commit()
    return dto(item)
