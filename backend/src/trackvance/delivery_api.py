"""Authenticated internal FastAPI boundary for Data Delivery and destinations."""

import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .artifactstore import storage_provider
from .data_sinks import DeliveryError, DestinationSettings, sink_registry
from .db import get_db, iso, utcnow
from .delivery_credential_store import destination_secret_store
from .delivery_schemas import (
    DeliveryAttemptsResponse,
    DeliveryConfigurationBody,
    DeliveryConfigurationVersionBody,
    DeliveryDraft,
    DeliveryPreflightResponse,
    DeliveryPreviewResponse,
    DeliveryRepairResponse,
    DeliveryReviewBody,
    DeliveryReviewResponse,
    DeliveryReviewsResponse,
    DeliveryRunBody,
    DestinationBody,
    DestinationListResponse,
    DestinationPatch,
    DestinationResponse,
    DestinationTestBody,
    DestinationTestResponse,
)
from .delivery_service import (
    DeliveryOperationError,
    attempt_dto,
    create_delivery_configuration,
    current_destination_version,
    delivery_config_dto,
    destination_dto,
    enqueue_delivery,
    owned_delivery_run,
    owned_destination,
    preflight_delivery,
    preview_delivery,
    receipt_artifact,
    record_delivery_review,
    repair_delivery_evidence,
    require_password_for_destination_change,
    review_dto,
    save_destination,
    settings_for,
    test_saved_destination,
)
from .models import (
    Configuration,
    DatasetVersion,
    DeliveryAttempt,
    DeliveryDestination,
    DeliveryOperationalReview,
    IdempotencyKey,
    Run,
    User,
    uid,
)
from .services import audit, run_dto

router = APIRouter(prefix="/api/v1/delivery", tags=["Data Delivery"])


def current_user(request: Request) -> User:
    return request.state.user


@router.get("/destinations", response_model=DestinationListResponse)
def list_destinations(
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    items = db.scalars(
        select(DeliveryDestination)
        .where(
            DeliveryDestination.organization_id == user.organization_id,
            DeliveryDestination.deleted.is_(False),
        )
        .order_by(DeliveryDestination.updated_at.desc())
    ).all()
    return {"items": [destination_dto(db, item) for item in items], "total": len(items)}


@router.post("/destinations/test", response_model=DestinationTestResponse)
def test_destination_draft(
    body: DestinationTestBody,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    password = body.password.get_secret_value() if body.password else ""
    config = {
        "host": body.host,
        "port": body.port,
        "database": body.database,
        "username": body.username,
        "options": body.options,
    }
    subject_id = body.destination_id or uid()
    if body.destination_id:
        destination = owned_destination(db, body.destination_id, user)
        if destination.sink_type != body.sink_type:
            raise DeliveryOperationError(
                422,
                "SINK_TYPE_IMMUTABLE",
                "El motor de un destino existente no puede cambiar.",
            )
        version = current_destination_version(db, destination)
        if not password:
            require_password_for_destination_change(destination, version, config)
            password = destination_secret_store.get(
                user.organization_id, version.secret_reference
            )
    if not password:
        raise DeliveryOperationError(
            422, "PASSWORD_REQUIRED", "Indica la contraseña del destino."
        )
    settings = DestinationSettings(
        sink_type=body.sink_type,
        host=body.host,
        port=body.port,
        database=body.database,
        username=body.username,
        password=password,
        options=body.options,
    )
    try:
        sink_registry.create(settings).test()
    except DeliveryError as error:
        audit(
            db,
            "DESTINATION_TESTED",
            "delivery_destination" if body.destination_id else "delivery_destination_test",
            subject_id,
            "Prueba de borrador fallida",
            user.name,
            user.organization_id,
            {"sink_type": body.sink_type, "status": "FAILED", "error_code": error.code},
        )
        db.commit()
        raise
    audit(
        db,
        "DESTINATION_TESTED",
        "delivery_destination" if body.destination_id else "delivery_destination_test",
        subject_id,
        "Prueba de borrador exitosa",
        user.name,
        user.organization_id,
        {"sink_type": body.sink_type, "status": "SUCCESS", "draft": True},
    )
    db.commit()
    return {
        "status": "SUCCESS",
        "message": "Destino verificado correctamente.",
        "tested_at": iso(utcnow()),
    }


@router.post("/destinations", status_code=201, response_model=DestinationResponse)
def create_destination(
    body: DestinationBody,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return destination_dto(db, save_destination(db, user, body.model_dump()))


@router.get("/destinations/{destination_id}", response_model=DestinationResponse)
def destination_detail(
    destination_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return destination_dto(db, owned_destination(db, destination_id, user))


@router.patch("/destinations/{destination_id}", response_model=DestinationResponse)
def update_destination(
    destination_id: str,
    body: DestinationPatch,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    destination = owned_destination(db, destination_id, user, lock=True)
    updated = save_destination(
        db, user, body.model_dump(exclude_none=True), destination=destination
    )
    return destination_dto(db, updated)


@router.delete("/destinations/{destination_id}")
def delete_destination(
    destination_id: str,
    version: int = Query(ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    destination = owned_destination(db, destination_id, user, lock=True)
    if destination.version != version:
        raise DeliveryOperationError(
            409, "VERSION_CONFLICT", "El destino fue modificado. Actualiza antes de retirarlo."
        )
    destination.deleted = True
    destination.enabled = False
    destination.updated_at = utcnow()
    audit(
        db,
        "DESTINATION_DISABLED",
        "delivery_destination",
        destination.id,
        "Destino retirado lógicamente; historial conservado",
        user.name,
        user.organization_id,
        {"version": destination.version},
    )
    db.commit()
    return {"ok": True}


@router.post("/destinations/{destination_id}/test", response_model=DestinationTestResponse)
def test_destination(
    destination_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return test_saved_destination(
        db, owned_destination(db, destination_id, user, lock=True), user
    )


def _saved_sink(db: Session, destination_id: str, user: User):
    destination = owned_destination(db, destination_id, user)
    if not destination.enabled:
        raise DeliveryOperationError(
            409, "DESTINATION_DISABLED", "Habilita el destino para consultar metadata."
        )
    version = current_destination_version(db, destination)
    return sink_registry.create(settings_for(destination, version)), version


@router.get("/destinations/{destination_id}/schemas")
def destination_schemas(
    destination_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    sink, version = _saved_sink(db, destination_id, user)
    items = sink.schemas()
    return {"items": items, "total": len(items), "destination_version_id": version.id}


@router.get("/destinations/{destination_id}/tables")
def destination_tables(
    destination_id: str,
    schema_name: str = Query(min_length=1, max_length=128),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    sink, version = _saved_sink(db, destination_id, user)
    items = sink.tables(schema_name)
    return {"items": items, "total": len(items), "destination_version_id": version.id}


@router.get("/destinations/{destination_id}/table-metadata")
def destination_table_metadata(
    destination_id: str,
    schema_name: str = Query(min_length=1, max_length=128),
    table_name: str = Query(min_length=1, max_length=128),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    sink, version = _saved_sink(db, destination_id, user)
    return {
        **sink.table_metadata(schema_name, table_name),
        "destination_version_id": version.id,
    }


@router.post("/preview", response_model=DeliveryPreviewResponse)
def delivery_preview(
    body: DeliveryDraft,
    limit: int = Query(default=8, ge=1, le=20),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return preview_delivery(db, user.organization_id, body, limit=limit)


@router.post("/preflight", response_model=DeliveryPreflightResponse)
def delivery_preflight(
    body: DeliveryDraft,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return preflight_delivery(db, user.organization_id, body)


@router.get("/configurations")
def list_delivery_configurations(
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    items = db.scalars(
        select(Configuration)
        .where(
            Configuration.organization_id == user.organization_id,
            Configuration.module == "DELIVERY",
        )
        .order_by(Configuration.created_at.desc())
    ).all()
    return {"items": [delivery_config_dto(db, item) for item in items], "total": len(items)}


@router.post("/configurations", status_code=201)
def publish_delivery_configuration(
    body: DeliveryConfigurationBody,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    draft = DeliveryDraft.model_validate(
        body.model_dump(exclude={"name", "owner", "description"})
    )
    config = create_delivery_configuration(
        db,
        user,
        draft,
        name=body.name,
        owner=body.owner,
        description=body.description,
    )
    return delivery_config_dto(db, config)


@router.post("/configurations/{configuration_id}/versions", status_code=201)
def publish_delivery_configuration_version(
    configuration_id: str,
    body: DeliveryConfigurationVersionBody,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    previous = db.scalar(
        select(Configuration).where(
            Configuration.id == configuration_id,
            Configuration.organization_id == user.organization_id,
            Configuration.module == "DELIVERY",
        )
    )
    if previous is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró la configuración.")
    draft = DeliveryDraft.model_validate(body.model_dump(exclude={"description"}))
    config = create_delivery_configuration(
        db,
        user,
        draft,
        name=previous.name,
        owner=previous.owner,
        description=body.description if body.description is not None else previous.description,
        previous=previous,
    )
    return delivery_config_dto(db, config)


def _delivery_run(
    db: Session,
    user: User,
    body: DeliveryRunBody,
    request: Request,
) -> dict:
    route = request.url.path
    key = request.headers.get("Idempotency-Key")
    request_hash = hashlib.sha256(
        json.dumps(
            ["DELIVERY", body.configuration_id, body.dataset_version_id],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if key:
        if not key.strip() or len(key) > 128:
            raise DeliveryOperationError(
                422,
                "INVALID_IDEMPOTENCY_KEY",
                "La clave de idempotencia debe tener entre 1 y 128 caracteres.",
            )
        existing = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.organization_id == user.organization_id,
                IdempotencyKey.route == route,
                IdempotencyKey.key == key,
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise DeliveryOperationError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "Esta clave ya se utilizó con otros parámetros.",
                )
            run = db.scalar(
                select(Run).where(
                    Run.id == existing.run_id,
                    Run.organization_id == user.organization_id,
                )
            )
            if run is None:
                raise DeliveryOperationError(409, "IDEMPOTENCY_CONFLICT", "Run no disponible.")
            return run_dto(db, run)
    config = db.scalar(
        select(Configuration).where(
            Configuration.id == body.configuration_id,
            Configuration.organization_id == user.organization_id,
            Configuration.module == "DELIVERY",
        )
    )
    source = db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == body.dataset_version_id,
            DatasetVersion.organization_id == user.organization_id,
        )
    )
    if config is None or source is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró la configuración o versión.")
    run = enqueue_delivery(db, config, source, user)
    if key:
        db.add(
            IdempotencyKey(
                organization_id=user.organization_id,
                route=route,
                key=key,
                request_hash=request_hash,
                run_id=run.id,
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.organization_id == user.organization_id,
                IdempotencyKey.route == route,
                IdempotencyKey.key == key,
            )
        ) if key else None
        if not existing or existing.request_hash != request_hash:
            raise DeliveryOperationError(
                409, "IDEMPOTENCY_CONFLICT", "La solicitud entró en conflicto."
            ) from None
        run = db.scalar(
            select(Run).where(
                Run.id == existing.run_id,
                Run.organization_id == user.organization_id,
            )
        )
        if run is None:
            raise DeliveryOperationError(409, "IDEMPOTENCY_CONFLICT", "Run no disponible.")
    return run_dto(db, run)


@router.get("/runs")
def list_delivery_runs(
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    items = db.scalars(
        select(Run)
        .where(Run.organization_id == user.organization_id, Run.module == "DELIVERY")
        .order_by(Run.created_at.desc())
    ).all()
    return {"items": [run_dto(db, item) for item in items], "total": len(items)}


@router.post("/runs", status_code=202)
def create_delivery_run(
    body: DeliveryRunBody,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _delivery_run(db, user, body, request)


@router.get("/runs/{run_id}/attempts", response_model=DeliveryAttemptsResponse)
def delivery_attempts(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    run = db.scalar(
        select(Run).where(
            Run.id == run_id,
            Run.organization_id == user.organization_id,
            Run.module == "DELIVERY",
        )
    )
    if run is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró la entrega.")
    items = db.scalars(
        select(DeliveryAttempt)
        .where(
            DeliveryAttempt.run_id == run.id,
            DeliveryAttempt.organization_id == user.organization_id,
        )
        .order_by(DeliveryAttempt.attempt_number)
    ).all()
    return {"items": [attempt_dto(item) for item in items], "total": len(items)}


@router.get("/runs/{run_id}/receipt")
def delivery_receipt(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    run = db.scalar(
        select(Run).where(
            Run.id == run_id,
            Run.organization_id == user.organization_id,
            Run.module == "DELIVERY",
        )
    )
    if run is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró la entrega.")
    artifact = receipt_artifact(db, run)
    path = storage_provider.materialize(artifact)
    audit(
        db,
        "ARTIFACT_DOWNLOADED",
        "artifact",
        artifact.id,
        "Receipt de Delivery descargado",
        user.name,
        user.organization_id,
        {"kind": "DELIVERY_RECEIPT", "sha256": artifact.sha256},
        run_id=run.id,
    )
    db.commit()
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"trackvance_{run.id}_delivery_receipt.json",
    )


@router.post("/runs/{run_id}/repair-evidence", response_model=DeliveryRepairResponse)
def repair_evidence(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Reconstruct verified local evidence of COMMITTED; never replay a delivery."""
    return repair_delivery_evidence(db, user, run_id)


@router.get("/runs/{run_id}/reviews", response_model=DeliveryReviewsResponse)
def delivery_reviews(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Read structured external observations separately from historical outcomes."""
    run = owned_delivery_run(db, user, run_id)
    items = db.scalars(select(DeliveryOperationalReview).where(
        DeliveryOperationalReview.run_id == run.id,
        DeliveryOperationalReview.organization_id == user.organization_id,
    ).order_by(DeliveryOperationalReview.created_at, DeliveryOperationalReview.id)).all()
    return {"items": [review_dto(item) for item in items], "total": len(items)}


@router.post("/runs/{run_id}/reviews", status_code=201, response_model=DeliveryReviewResponse)
def review_delivery_outcome(
    run_id: str,
    body: DeliveryReviewBody,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Record a human observation; original UNKNOWN and Run/Job identity are unchanged."""
    return review_dto(record_delivery_review(db, user, run_id, body))
