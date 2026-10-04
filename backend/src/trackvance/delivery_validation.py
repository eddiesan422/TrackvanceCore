"""Persisted complete preflight executions on the existing delivery job lane."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit_context import Actor
from .data_sinks import DeliveryError
from .db import get_db, iso, utcnow
from .delivery_schemas import DeliveryDraft
from .delivery_service import (
    DeliveryOperationError,
    _owned_version,
    delivery_control,
    preflight_delivery,
)
from .jobqueue import job_queue
from .manifests import configuration_hash
from .models import Configuration, Job, Run, User, uid
from .permissions import effective_permissions
from .services import _verify_lease, audit

router = APIRouter(prefix="/api/v1/delivery", tags=["Delivery validation"])


def current_user(request: Request):
    return request.state.user


def validation_dto(run, db=None):
    config = db.get(Configuration, run.config_id) if db else None
    return {"id": run.id, "status": run.status, "stage": run.progress_stage,
            "dataset_version_id": run.dataset_version_id, "created_at": iso(run.created_at),
            "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
            "error": run.error, "result": run.metrics.get("preflight"),
            "validation_hash": run.execution_plan.get("draft_hash"),
            "cancel_requested": run.cancel_requested, "draft": config.config if config else None,
            "dataset_id": config.dataset_id if config else None}


def owned_validation(db, user, run_id, *, lock=False):
    query = select(Run).where(Run.id == run_id, Run.organization_id == user.organization_id,
                              Run.module == "DELIVERY_PREFLIGHT")
    run = db.scalar(query.with_for_update() if lock else query)
    if run is None or run.initiated_by_id != user.id:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró la validación solicitada.")
    return run


@router.post("/validations", status_code=202)
def register_validation(body: DeliveryDraft, db: Session = Depends(get_db), user: User = Depends(current_user)):
    version, dataset, artifact, _records = _owned_version(db, body.dataset_version_id, user.organization_id)
    configuration = Configuration(id=uid(), organization_id=user.organization_id,
        module="DELIVERY", dataset_id=dataset.id, name="Preflight completo",
        owner=user.name, status="VALIDATION_PRIVATE", config=body.snapshot())
    db.add(configuration)
    db.flush()
    run = Run(id=uid(), organization_id=user.organization_id, module="DELIVERY_PREFLIGHT",
        name="Preflight completo", config_id=configuration.id, dataset_version_id=version.id,
        initiated_by=user.name, initiated_by_type="USER", initiated_by_id=user.id,
        execution_plan={"operation": "PREFLIGHT", "draft_hash": configuration_hash(body.snapshot()),
                        "canonical_sha256": artifact.sha256, "engine": "DELIVERY_SQL"})
    db.add(run)
    db.flush()
    job = job_queue.submit(db, run, executable=True)
    job.lane = "DELIVERY"
    audit(db, "DELIVERY_PREFLIGHT_QUEUED", "run", run.id, "Validación completa registrada",
          Actor("USER", user.id, user.name), user.organization_id)
    db.commit()
    return validation_dto(run, db)


@router.get("/validations")
def list_validations(db: Session = Depends(get_db), user: User = Depends(current_user)):
    runs = db.scalars(select(Run).where(Run.organization_id == user.organization_id,
        Run.module == "DELIVERY_PREFLIGHT", Run.initiated_by_id == user.id)
        .order_by(Run.created_at.desc()).limit(50)).all()
    return {"items": [validation_dto(run, db) for run in runs], "total": len(runs)}


@router.get("/validations/{run_id}")
def read_validation(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return validation_dto(owned_validation(db, user, run_id), db)


@router.post("/validations/{run_id}/cancel")
def cancel_validation(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = owned_validation(db, user, run_id, lock=True)
    if run.status in {"QUEUED", "RUNNING"}:
        run.cancel_requested = True
        if run.status == "QUEUED":
            run.status, run.finished_at, run.progress_stage = "CANCELLED", utcnow(), "Cancelado"
            job = db.scalar(select(Job).where(Job.run_id == run.id).with_for_update())
            if job:
                job.status = "CANCELLED"
        db.commit()
    return validation_dto(run, db)


def execute_validation_run(db, run, *, lease_owner=None):
    user = db.get(User, run.initiated_by_id)
    if user is None or user.organization_id != run.organization_id or "delivery:configure" not in effective_permissions(db, user):
        run.status, run.error, run.finished_at = "FAILED_PRECONDITION", "La autorización del iniciador ya no está vigente.", utcnow()
        return
    if run.cancel_requested:
        run.status, run.finished_at = "CANCELLED", utcnow()
        return
    config = db.get(Configuration, run.config_id)
    draft = DeliveryDraft.model_validate(config.config)
    run.status, run.started_at, run.progress_stage = "RUNNING", run.started_at or utcnow(), "Validando toda la población"
    _verify_lease(db, run, lease_owner)
    db.commit()
    try:
        result = preflight_delivery(db, run.organization_id, draft, raise_on_failure=False, control=delivery_control(run.id, lease_owner))
        _verify_lease(db, run, lease_owner)
        db.refresh(run)
        if run.cancel_requested:
            run.status, run.progress_stage = "CANCELLED", "Cancelado"
        else:
            run.status, run.decision = "SUCCESS", result["status"]
            run.metrics = {"preflight": result}
            run.progress_stage = "Preflight completo terminado"
    except (DeliveryError, DeliveryOperationError) as error:
        if error.code == "WORKER_LEASE_LOST":
            raise
        _verify_lease(db, run, lease_owner)
        run.status, run.error, run.progress_stage = "CANCELLED" if error.code == "RUN_CANCELLED" else "FAILED_PRECONDITION", error.message, "Preflight fallido"
    run.finished_at = utcnow()


def validated_preflight(db, user, draft, validation_run_id):
    run = owned_validation(db, user, validation_run_id)
    result = run.metrics.get("preflight")
    _version, _dataset, artifact, _records = _owned_version(db, draft.dataset_version_id, user.organization_id)
    if (run.status != "SUCCESS" or run.decision != "PASS" or not result
            or run.execution_plan.get("draft_hash") != configuration_hash(draft.snapshot())
            or run.dataset_version_id != draft.dataset_version_id):
        raise DeliveryOperationError(412, "VALIDATION_BINDING_MISMATCH", "Se requiere una validación completa aprobada para este mismo borrador y DatasetVersion.")
    if run.execution_plan.get("canonical_sha256") != artifact.sha256:
        raise DeliveryOperationError(412, "VALIDATION_BINDING_MISMATCH", "La validación no corresponde a la identidad canónica de esta versión.")
    return result
