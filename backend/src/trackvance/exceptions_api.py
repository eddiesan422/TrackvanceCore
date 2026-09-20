"""Operational case management around immutable technical findings and runs."""

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .artifactstore import artifact_dto, link_artifact, storage_provider
from .audit_context import Actor
from .db import get_db, iso, utcnow
from .models import Artifact, ExceptionAttachment, ExceptionCase, Finding, User
from .operations_common import OperationError, save_case
from .permissions import permissions_for
from .services import (
    assess_exception_validation,
    audit,
    exception_dto,
    finding_dto,
    record_exception_validation,
)

router = APIRouter(prefix="/api/v1/exceptions", tags=["Excepciones"])
ADMINISTRATIVE = frozenset({"DISCARDED", "ACCEPTED", "NOT_APPLICABLE"})
CLOSED = frozenset({"RESOLVED", "FALSE_POSITIVE", *ADMINISTRATIVE})
OPEN_STATES = frozenset({"OPEN", "ASSIGNED", "INVESTIGATING", "PENDING_VALIDATION", "REOPENED", "WAITING_EXTERNAL"})
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACHMENTS = {".txt", ".csv", ".json", ".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".parquet"}


def current_user(request: Request) -> User:
    return request.state.user


def owned_case(db: Session, case_id: str, user: User) -> ExceptionCase:
    case = db.get(ExceptionCase, case_id)
    if not case or case.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró la excepción.")
    return case


def assert_version(case: ExceptionCase, version: int) -> None:
    if case.version != version:
        raise OperationError(409, "VERSION_CONFLICT", "Otra persona modificó esta excepción. Actualiza los datos.")


def actor_of(user: User) -> Actor:
    return Actor("USER", user.id, user.name)


def case_event(user: User, case: ExceptionCase, kind: str, comment: str, **extra) -> dict:
    return {"timestamp": iso(utcnow()), "actor": user.name, "actor_type": "USER", "actor_id": user.id,
            "from_state": case.state, "to_state": case.state, "event_type": kind, "comment": comment, **extra}


class CaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CasePatch(CaseInput):
    version: int = Field(ge=1)
    state: Literal["OPEN", "ASSIGNED", "INVESTIGATING", "PENDING_VALIDATION", "RESOLVED", "DISCARDED", "ACCEPTED", "NOT_APPLICABLE", "REOPENED"] | None = None
    owner: str | None = Field(default=None, min_length=1, max_length=120)
    assigned_user_id: str | None = Field(default=None, min_length=1, max_length=64)
    priority: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = None
    sla_hours: int | None = Field(default=None, ge=1, le=8760)
    due_at: datetime | None = None
    auto_resolve_enabled: bool | None = None
    root_cause: str | None = Field(default=None, max_length=10000)
    resolution: str | None = Field(default=None, max_length=10000)
    administrative_reason: str | None = Field(default=None, max_length=10000)
    comment: str | None = Field(default=None, max_length=10000)

    @field_validator("due_at")
    @classmethod
    def utc_date(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("La fecha objetivo debe incluir zona horaria.")
        return value.astimezone(UTC) if value else None


class ValidationBody(CaseInput):
    version: int = Field(ge=1)
    validation_run_id: str | None = Field(default=None, min_length=1, max_length=64)
    comment: str | None = Field(default=None, max_length=10000)


class CommentBody(CaseInput):
    version: int = Field(ge=1)
    comment: str = Field(min_length=1, max_length=10000)


@router.get("")
def exceptions(state: str | None = None, module: str | None = None, severity: str | None = None,
               priority: str | None = None, assigned_user_id: str | None = None, overdue: bool | None = None,
               search: str = "", offset: int = 0, limit: int = 500,
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    if offset < 0 or limit < 1 or limit > 1000 or len(search) > 200:
        raise OperationError(422, "INVALID_FILTER", "Los filtros o límites no son válidos.")
    query = select(ExceptionCase).where(ExceptionCase.organization_id == user.organization_id)
    for field, value in (("state", state), ("module", module), ("severity", severity),
                         ("priority", priority), ("assigned_user_id", assigned_user_id)):
        if value:
            query = query.where(getattr(ExceptionCase, field) == value)
    if search:
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(*(column.ilike(pattern, escape="\\") for column in
                                 (ExceptionCase.title, ExceptionCase.display_id, ExceptionCase.owner))))
    if overdue is not None:
        condition = ExceptionCase.state.in_(OPEN_STATES) & ExceptionCase.due_at.is_not(None) & (ExceptionCase.due_at < utcnow())
        query = query.where(condition if overdue else ~condition)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(ExceptionCase.updated_at.desc(), ExceptionCase.id).offset(offset).limit(limit))
    return {"items": [exception_dto(db, case) for case in rows], "total": total}


@router.get("/assignees")
def assignees(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = [{"id": candidate.id, "name": candidate.name, "role": candidate.role} for candidate in
            db.scalars(select(User).where(User.organization_id == user.organization_id, User.active.is_(True)).order_by(User.name))
            if "exceptions:write" in permissions_for(candidate.role)]
    return {"items": rows, "total": len(rows)}


@router.get("/{case_id}")
def detail(case_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = owned_case(db, case_id, user)
    finding = db.get(Finding, case.finding_id) if case.finding_id else None
    return {**exception_dto(db, case), "finding": finding_dto(db, finding) if finding else None}


@router.post("/{case_id}/validate")
def validate(case_id: str, body: ValidationBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = owned_case(db, case_id, user)
    assert_version(case, body.version)
    if case.state != "PENDING_VALIDATION":
        raise OperationError(422, "VALIDATION_STATE_REQUIRED", "Pasa la excepción a Pendiente de validación antes de verificar la corrección.")
    assessment = assess_exception_validation(db, case, body.validation_run_id)
    if assessment["status"] == "INVALID_RUN":
        raise OperationError(409, "VALIDATION_RUN_OUTDATED", assessment["reason"], {"latest_run_id": assessment["candidate_run_id"]})
    if assessment["status"] == "NO_LATER_RUN":
        raise OperationError(409, "VALIDATION_RUN_REQUIRED", assessment["reason"])
    record_exception_validation(db, case, assessment, actor_of(user), body.comment)
    db.commit()
    return exception_dto(db, case)


@router.patch("/{case_id}")
def update_case(case_id: str, body: CasePatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = owned_case(db, case_id, user)
    assert_version(case, body.version)
    values = body.model_dump(exclude={"version", "comment"}, exclude_unset=True)
    changes = {key: value for key, value in values.items() if value is not None or key in {"assigned_user_id", "sla_hours", "due_at"}}
    state = changes.get("state", case.state)
    permissions = permissions_for(user.role)
    if ((state != case.state and state in CLOSED) or
        ("auto_resolve_enabled" in changes and changes["auto_resolve_enabled"] != case.auto_resolve_enabled)) and "exceptions:close" not in permissions:
        raise OperationError(403, "FORBIDDEN", "No tienes permiso para cerrar excepciones o cambiar su resolución automática.")
    transitions = {
        "OPEN": {"ASSIGNED", "INVESTIGATING", *ADMINISTRATIVE},
        "ASSIGNED": {"INVESTIGATING", *ADMINISTRATIVE},
        "INVESTIGATING": {"ASSIGNED", "PENDING_VALIDATION", *ADMINISTRATIVE},
        "PENDING_VALIDATION": {"INVESTIGATING", "RESOLVED", *ADMINISTRATIVE},
        "REOPENED": {"ASSIGNED", "INVESTIGATING", *ADMINISTRATIVE},
        "WAITING_EXTERNAL": {"INVESTIGATING", "PENDING_VALIDATION", *ADMINISTRATIVE},
        **{value: {"OPEN", "REOPENED"} for value in CLOSED},
    }
    if state != case.state and state not in transitions.get(case.state, set()):
        raise OperationError(422, "INVALID_TRANSITION", "La transición de estado no está permitida.")
    if case.state in CLOSED and state == case.state and changes:
        raise OperationError(422, "CASE_CLOSED", "Reabre el caso antes de modificar su gestión.")
    if "assigned_user_id" in changes:
        candidate = db.get(User, changes["assigned_user_id"]) if changes["assigned_user_id"] else None
        if changes["assigned_user_id"] and (not candidate or candidate.organization_id != user.organization_id
                                             or (changes["assigned_user_id"] != case.assigned_user_id and
                                                 (not candidate.active or "exceptions:write" not in permissions_for(candidate.role)))):
            raise OperationError(422, "INVALID_ASSIGNEE", "Selecciona un responsable activo de esta organización con permiso de gestión.")
        if changes["assigned_user_id"] != case.assigned_user_id:
            changes["owner"] = candidate.name if candidate else "Sin asignar"
    elif case.assigned_user_id and "owner" in changes:
        changes.pop("owner")  # Stable assignment controls the display label; legacy owner remains writable otherwise.
    if state == "ASSIGNED" and not changes.get("assigned_user_id", case.assigned_user_id):
        raise OperationError(422, "ASSIGNEE_REQUIRED", "Selecciona un responsable para asignar la excepción.")
    now = utcnow()
    reopened = case.state in CLOSED and state in {"OPEN", "REOPENED"}
    if reopened:
        if not body.comment:
            raise OperationError(422, "REOPEN_REASON_REQUIRED", "Registra por qué se reabre la excepción.")
        changes["reopened_at"] = now
    if "sla_hours" in changes and "due_at" not in changes:
        base = now if reopened else case.reopened_at or case.created_at
        changes["due_at"] = base + timedelta(hours=changes["sla_hours"]) if changes["sla_hours"] else None
    elif reopened and case.sla_hours and "due_at" not in changes:
        changes["due_at"] = now + timedelta(hours=case.sla_hours)
    assessment = None
    if state == "RESOLVED" and state != case.state:
        if not changes.get("root_cause", case.root_cause).strip() or not changes.get("resolution", case.resolution).strip():
            raise OperationError(422, "RESOLUTION_REQUIRED", "Para resolver, registra causa raíz y resolución.")
        assessment = assess_exception_validation(db, case)
        if not assessment["can_resolve"]:
            raise OperationError(422, "TECHNICAL_VALIDATION_REQUIRED", assessment["reason"], assessment)
        changes.update(validation_run_id=assessment["validation_run_id"], validated_at=now,
                       validation_evidence=assessment["evidence"])
    if state in ADMINISTRATIVE and state != case.state:
        if not changes.get("administrative_reason", "").strip():
            raise OperationError(422, "ADMINISTRATIVE_REASON_REQUIRED", "Registra el motivo del cierre administrativo.")
        changes.update(validation_run_id=None, validated_at=None, validation_evidence={})
    if state != case.state and state in OPEN_STATES:
        changes.update(validation_run_id=None, validated_at=None, validation_evidence={})
    event = case_event(user, case, "REOPENED" if reopened else "STATE_CHANGE" if state != case.state else "CASE_UPDATED",
                       body.comment or "Excepción actualizada", to_state=state,
                       validation_run_id=assessment["validation_run_id"] if assessment else None,
                       administrative_reason=changes.get("administrative_reason") if state in ADMINISTRATIVE else None,
                       changes={key: value for key, value in changes.items() if key in
                                {"assigned_user_id", "priority", "sla_hours", "auto_resolve_enabled"}})
    old_state = case.state
    changes.update(updated_at=now, events=[*(case.events or []), event])
    save_case(db, case, body.version, changes)
    audit(db, "EXCEPTION_REOPENED" if reopened else "EXCEPTION_UPDATED", "exception", case.id,
          f"{case.display_id}: {state}", actor_of(user), user.organization_id,
          {"from_state": old_state, "to_state": state, "configuration_id": case.configuration_id,
           "origin_run_id": case.run_id, "validation_run_id": assessment["validation_run_id"] if assessment else None,
           "assigned_user_id": case.assigned_user_id, "priority": case.priority, "sla_hours": case.sla_hours,
           "due_at": iso(case.due_at), "auto_resolve_enabled": case.auto_resolve_enabled,
           "closure_type": "TECHNICAL" if state == "RESOLVED" else "ADMINISTRATIVE" if state in ADMINISTRATIVE else None}, run_id=case.run_id)
    db.commit()
    return exception_dto(db, case)


@router.post("/{case_id}/comments")
def comment(case_id: str, body: CommentBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = owned_case(db, case_id, user)
    assert_version(case, body.version)
    save_case(db, case, body.version, {"updated_at": utcnow(), "events": [*(case.events or []),
                                      case_event(user, case, "COMMENT_ADDED", body.comment)]})
    audit(db, "EXCEPTION_COMMENT_ADDED", "exception", case.id, "Comentario agregado al caso", actor_of(user), user.organization_id, run_id=case.run_id)
    db.commit()
    return exception_dto(db, case)


@router.post("/{case_id}/attachments", status_code=201)
async def upload_attachment(case_id: str, version: int = Form(...), description: str = Form(""),
                            file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = owned_case(db, case_id, user)
    assert_version(case, version)
    if len(description) > 500:
        raise OperationError(422, "INVALID_ATTACHMENT", "La descripción permite hasta 500 caracteres.")
    # Treat the supplied filename only as a label; never as a local storage path.
    filename = (file.filename or "archivo").replace("\\", "/").split("/")[-1]
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).lstrip(".").strip()[:180]
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_ATTACHMENTS:
        raise OperationError(422, "UNSUPPORTED_ATTACHMENT", "Formato no admitido. Usa PDF, imagen, TXT, CSV, JSON, XLSX o Parquet.")
    temporary = storage_provider.temporary_path(extension)
    try:
        size = 0
        with temporary.open("xb") as outgoing:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise OperationError(413, "ATTACHMENT_TOO_LARGE", "El adjunto supera 10 MiB.")
                outgoing.write(chunk)
        if not size:
            raise OperationError(422, "EMPTY_ATTACHMENT", "El adjunto está vacío.")
        artifact = storage_provider.put_file(db, temporary, "EXCEPTION_ATTACHMENT", user.organization_id, filename)
        attachment = ExceptionAttachment(exception_id=case.id, organization_id=user.organization_id,
                                        artifact_id=artifact.id, uploaded_by_id=user.id, description=description.strip())
        db.add(attachment)
        db.flush()
        link_artifact(db, user.organization_id, "EXCEPTION_EVIDENCE", "EXCEPTION", case.id, "ARTIFACT", artifact.id)
        save_case(db, case, version, {"updated_at": utcnow(), "events": [*(case.events or []),
            case_event(user, case, "ATTACHMENT_ADDED", description.strip() or "Evidencia adjunta",
                       artifact_id=artifact.id, name=artifact.name, sha256=artifact.sha256)]})
        audit(db, "EXCEPTION_ATTACHMENT_ADDED", "exception", case.id, "Evidencia adjunta al caso", actor_of(user),
              user.organization_id, artifact_dto(artifact), run_id=case.run_id)
        db.commit()
        return exception_dto(db, case)
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()


@router.get("/{case_id}/attachments/{attachment_id}/download")
def attachment_download(case_id: str, attachment_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if "artifacts:download" not in permissions_for(user.role):
        raise OperationError(403, "FORBIDDEN", "Tu rol no permite descargar evidencia.")
    case = owned_case(db, case_id, user)
    attachment = db.get(ExceptionAttachment, attachment_id)
    if not attachment or attachment.organization_id != user.organization_id or attachment.exception_id != case.id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el adjunto.")
    artifact = db.get(Artifact, attachment.artifact_id)
    if not artifact or artifact.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el adjunto.")
    path = storage_provider.materialize(artifact)
    audit(db, "EXCEPTION_ATTACHMENT_DOWNLOADED", "exception", case.id, "Evidencia descargada", actor_of(user),
          user.organization_id, artifact_dto(artifact), run_id=case.run_id)
    db.commit()
    return FileResponse(path, filename=artifact.name, media_type=artifact.media_type,
                        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
