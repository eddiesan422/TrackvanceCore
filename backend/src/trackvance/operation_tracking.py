"""Read-only operational projection over native identities, never another case store.

Pagination uses a raw metadata cursor. Unauthorized resources are removed before
serialization; neither their names nor a global count leak through this endpoint.
"""

from datetime import UTC
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, literal, or_, select, union_all
from sqlalchemy.orm import Session

from .acquisition_models import AcquisitionRun
from .db import get_db, iso, utcnow
from .document_models import (
    DocumentBatch,
    DocumentBatchMember,
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
)
from .governance import authorize_dataset, authorize_run_content
from .models import Dataset, DatasetVersion, ExceptionCase, Run, User
from .operations_common import OperationError
from .permissions import effective_permissions

router = APIRouter(prefix="/api/v1/operations", tags=["Operación y seguimiento"])
CLOSED = frozenset({"RESOLVED", "DISCARDED", "ACCEPTED", "NOT_APPLICABLE", "FALSE_POSITIVE"})
Queue = Literal["all", "intervention", "running", "waiting", "finished"]


def current_user(request: Request) -> User:
    return request.state.user


def operational_queue(kind: str, status: str, *, decision: str | None = None, stage: str = "") -> str:
    if kind == "exception":
        return "finished" if status in CLOSED else "waiting" if status == "WAITING_EXTERNAL" else "running" if status == "INVESTIGATING" else "intervention"
    if kind == "document":
        return "finished" if status in {"PUBLISHED", "CANCELLED"} else "waiting" if status == "QUEUED" else "running" if status == "RUNNING" else "intervention"
    if kind == "document_batch":
        return "finished" if status in {"PUBLISHED", "EMPTY"} else "waiting" if status in {"OPEN", "WAITING"} else "intervention"
    if decision in {"UNKNOWN", "PARTIAL"} or status in {"UNKNOWN", "FAILED", "FAILED_PRECONDITION"}:
        return "intervention"
    if stage in {"RETRY_WAIT", "WAITING_RETRY"} or status in {"QUEUED", "WAITING", "RETRY_WAIT"}:
        return "waiting"
    return "running" if status == "RUNNING" else "finished"


def _queue_expression(kind, status, decision=None, stage=None):
    """Filter native state metadata before loading and authorizing each item.

    This mirrors operational_queue; authorization still happens before any item
    is serialized. A busy history must not make a waiting view traverse every
    completed run and its complete security ancestry.
    """
    if kind == "exception":
        return case((status.in_(CLOSED), "finished"), (status == "WAITING_EXTERNAL", "waiting"),
            (status == "INVESTIGATING", "running"), else_="intervention")
    if kind == "document":
        return case((status.in_({"PUBLISHED", "CANCELLED"}), "finished"), (status == "QUEUED", "waiting"),
            (status == "RUNNING", "running"), else_="intervention")
    if kind == "document_batch":
        return case((status.in_({"PUBLISHED", "EMPTY"}), "finished"),
            (status.in_({"OPEN", "WAITING"}), "waiting"), else_="intervention")
    decision = literal(None) if decision is None else decision
    stage = literal(None) if stage is None else stage
    return case((or_(decision.in_({"UNKNOWN", "PARTIAL"}), status.in_({"UNKNOWN", "FAILED", "FAILED_PRECONDITION"})), "intervention"),
        (or_(stage.in_({"RETRY_WAIT", "WAITING_RETRY"}), status.in_({"QUEUED", "WAITING", "RETRY_WAIT"})), "waiting"),
        (status == "RUNNING", "running"), else_="finished")


def _duration(record) -> float | None:
    if not record.started_at:
        return None
    return max(0, ((record.finished_at or utcnow()).replace(tzinfo=UTC) - record.started_at.replace(tzinfo=UTC)).total_seconds())


def _tracking(db, version: DatasetVersion | None, organization_id: str) -> str | None:
    """Follow only stored immutable relationships, with organization/cycle fences."""
    seen: set[str] = set()
    while version and version.organization_id == organization_id and version.id not in seen and len(seen) < 128:
        seen.add(version.id)
        metadata = version.ingestion_metadata or {}
        publication = metadata.get("document_publication") or {}
        if publication.get("batch_id"):
            return f"document-batch:{publication['batch_id']}"
        if isinstance(metadata.get("tracking_id"), str):
            return metadata["tracking_id"][:160]
        acquisition = db.scalar(select(AcquisitionRun.id).where(AcquisitionRun.organization_id == organization_id,
            AcquisitionRun.output_version_id == version.id))
        if acquisition:
            return f"acquisition:{acquisition}"
        if not version.parent_version_id:
            return f"version:{version.id}"
        version = db.get(DatasetVersion, version.parent_version_id)
    return None


def _stage(kind, record, title, href=None):
    return {"id": f"{kind}:{record.id}", "title": title, "state": getattr(record, "state", getattr(record, "status", None)),
            "created_at": iso(record.created_at), "href": href}


def _version_stage(db, user, version_id, grants):
    version = db.get(DatasetVersion, version_id) if version_id else None
    if version is None or version.organization_id != user.organization_id or "datasets:read" not in grants:
        return None
    try:
        authorize_dataset(db, user, version.dataset_id, "CONTENT")
    except OperationError:
        return None
    return {"id": f"version:{version.id}", "title": f"Versión {version.version} publicada", "state": "PUBLISHED",
            "created_at": iso(version.created_at), "href": f"/datasets/{version.dataset_id}?version={version.id}",
            "row_count": version.row_count, "sha256": version.sha256}


def _downstream_stages(db, user, version_id, grants):
    """Bounded native input/output traversal, including approved Intake outputs.

    The bound is reported to the viewer. A missing delivery record never becomes
    a fabricated pending or completed delivery stage.
    """
    pending: list[str] = [version_id]
    visited: set[str] = set()
    stages: list[dict[str, Any]] = []
    seen_runs: set[str] = set()
    while pending and len(seen_runs) < 20 and len(visited) < 20:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        runs = db.scalars(select(Run).where(Run.organization_id == user.organization_id,
            Run.dataset_version_id == current).order_by(Run.created_at).limit(21)).all()
        for run in runs:
            if run.id in seen_runs:
                continue
            if len(seen_runs) == 20:
                return stages, True
            if f"{run.module.lower()}:read" not in grants:
                continue
            try:
                authorize_run_content(db, user, run)
            except OperationError:
                continue
            seen_runs.add(run.id)
            stages.append(_stage("run", run, run.module, f"/runs/{run.id}"))
            if run.output_version_id:
                output = _version_stage(db, user, run.output_version_id, grants)
                if output:
                    stages.append(output)
                    pending.append(run.output_version_id)
    return stages, bool(pending)


def _matches_search(item, search):
    return search.casefold() in f"{item['title']} {item['tracking_id']} {item.get('diagnosis') or ''}".casefold()


def _item(db, user, kind, identity, grants, search=""):
    model = {"acquisition": AcquisitionRun, "run": Run, "exception": ExceptionCase,
             "document": DocumentOriginal, "document_batch": DocumentBatch}[kind]
    record = db.get(model, identity)
    if not record or record.organization_id != user.organization_id:
        return None
    stages: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    result = {"id": f"{kind}:{identity}", "kind": kind, "resource_id": identity,
              "created_at": iso(record.created_at), "stages": stages, "actions": actions}
    try:
        if kind in {"document", "document_batch"}:
            if "documents:read" not in grants:
                return None
            family = db.get(DocumentFamily, record.family_id)
            if family is None or family.organization_id != user.organization_id:
                return None
            href = f"/documents/families/{family.id}"
            actor_id = record.uploaded_by_id if kind == "document" else family.responsible_user_id
            actor = db.get(User, actor_id)
            result.update(title=f"Documento · {record.filename}" if kind == "document" else f"Lote · {family.name}",
                tracking_id=f"document:{identity}" if kind == "document" else f"document-batch:{identity}",
                state=record.status, queue=operational_queue(kind, record.status),
                owner=actor.name if actor and actor.organization_id == user.organization_id else "Responsable no disponible",
                assigned_user_id=family.responsible_user_id,
                impact="La extracción, la revisión humana, la publicación del dataset y su entrega tienen estados independientes.",
                next_action="Revisa el documento y la extracción, corrige mediante una revisión y confirma los campos requeridos antes de publicar." if kind == "document" else "Revisa todos los miembros y bloqueos del lote antes de sellar su publicación.")
            if kind == "document":
                href += f"?document={identity}"
                result.update(duration_seconds=_duration(record), error_code=record.error_code,
                    diagnosis=record.error_code or f"{family.name} · {record.progress}% de extracción")
                if not _matches_search(result, search):
                    return None
                stages.append(_stage(kind, record, "Documento recibido", href))
                extraction = db.get(DocumentExtraction, record.extraction_id) if record.extraction_id else None
                if extraction and extraction.organization_id == user.organization_id:
                    stages.append({"id": f"extraction:{extraction.id}", "title": f"Extracción · revisión {extraction.revision}",
                        "state": "REVIEWED" if extraction.reviewed else "EXTRACTED", "created_at": iso(extraction.created_at), "href": href})
                members = db.scalars(select(DocumentBatchMember).where(DocumentBatchMember.organization_id == user.organization_id,
                    DocumentBatchMember.document_id == record.id).order_by(DocumentBatchMember.created_at).limit(21)).all()
                result["more_stages"] = len(members) > 20
                for member in members[:20]:
                    batch = db.get(DocumentBatch, member.batch_id)
                    if batch and batch.organization_id == user.organization_id:
                        stages.append(_stage("document-batch", batch, f"Lote · {member.status}", f"/documents/families/{family.id}?batch={batch.id}"))
            else:
                href += f"?batch={identity}"
                result.update(diagnosis=record.blocked_reason or record.status, window_start=iso(record.window_start),
                    window_end=iso(record.window_end), wait_until=iso(record.wait_until))
                if not _matches_search(result, search):
                    return None
                stages.append(_stage("document-batch", record, "Lote documental", href))
                for version_id in (record.published_versions or {}).values():
                    published = _version_stage(db, user, version_id, grants)
                    if published:
                        stages.append(published)
                        actions.append({"title": published["title"], "href": published["href"]})
            actions.insert(0, {"title": "Revisar documento" if kind == "document" else "Revisar lote", "href": href})
        elif kind == "acquisition":
            if "datasets:read" not in grants:
                return None
            dataset = db.get(Dataset, record.dataset_id) if record.dataset_id else None
            pending = (record.source_snapshot or {}).get("pending_dataset") or {}
            result.update(title=f"Adquisición · {dataset.name if dataset else pending.get('name', record.filename)}",
                tracking_id=f"acquisition:{identity}", state=record.status,
                queue=operational_queue(kind, record.status, stage=record.stage),
                owner=record.initiated_by_name, assigned_user_id=record.initiated_by_id,
                duration_seconds=_duration(record), diagnosis=record.error_message or record.stage,
                error_code=record.error_code, attempts=record.attempts,
                impact="No existe una publicación parcial. Las versiones anteriores conservan su identidad." if record.status in {"FAILED", "CANCELLED"} else "La publicación depende de completar la lectura y los controles de esta adquisición.",
                next_action="Revisa el diagnóstico y corrige la conexión o el origen antes de iniciar otra adquisición." if record.status == "FAILED" else "Consulta el progreso y la versión publicada cuando esté disponible.")
            if not _matches_search(result, search):
                return None
            if dataset:
                authorize_dataset(db, user, dataset.id)
            stages.append(_stage(kind, record, "Adquisición", f"/operations?acquisition={identity}"))
            actions.append({"title": "Ver adquisición", "href": f"/operations?acquisition={identity}"})
            snapshot = record.source_snapshot or {}
            if snapshot.get("connection_id") and "connections:read" in grants:
                actions.append({"title": "Revisar conexión", "href": f"/connections/{snapshot['connection_id']}"})
            published = _version_stage(db, user, record.output_version_id, grants)
            if published:
                stages.append(published)
                actions.append({"title": "Abrir versión publicada", "href": published["href"]})
                linked, result["more_stages"] = _downstream_stages(db, user, record.output_version_id, grants)
                stages.extend(linked)
        else:
            run = record if kind == "run" else db.get(Run, record.run_id)
            if run is None or f"{run.module.lower()}:read" not in grants:
                return None
            version = db.get(DatasetVersion, run.dataset_version_id)
            result.update(tracking_id=_tracking(db, version, user.organization_id) or f"run:{run.id}",
                title=record.title if kind == "exception" else run.name,
                state=record.state if kind == "exception" else run.status,
                queue=operational_queue(kind, record.state if kind == "exception" else run.status,
                    decision=run.decision, stage=run.progress_stage),
                module=run.module, owner=record.owner if kind == "exception" else run.initiated_by,
                assigned_user_id=record.assigned_user_id if kind == "exception" else run.initiated_by_id,
                duration_seconds=_duration(run), diagnosis=record.root_cause if kind == "exception" else run.error or run.decision or run.progress_stage,
                impact="El cierre de una excepción y la confirmación de una entrega son etapas independientes.",
                next_action="Investiga el origen, documenta la corrección y valida la población afectada antes del cierre." if kind == "exception" else "Revisa el resultado y la evidencia de la ejecución.")
            if not _matches_search(result, search):
                return None
            authorize_run_content(db, user, run)
            initial = _version_stage(db, user, run.dataset_version_id, grants)
            if initial:
                stages.append(initial)
                actions.append({"title": "Ver datos afectados", "href": initial["href"]})
            stages.append(_stage("run", run, run.module, f"/runs/{run.id}"))
            actions.append({"title": "Revisar ejecución", "href": f"/runs/{run.id}"})
            if kind == "exception":
                stages.append(_stage(kind, record, "Excepción", f"/exceptions?id={identity}"))
                actions.insert(0, {"title": "Gestionar excepción", "href": f"/exceptions?id={identity}"})
                result.update(priority=record.priority, due_at=iso(record.due_at), revision=record.version,
                    closure_type="VERIFIED" if record.state == "RESOLVED" and record.validation_run_id else "ADMINISTRATIVE" if record.state in CLOSED else None)
            if run.decision == "UNKNOWN" or run.status == "UNKNOWN":
                result.update(next_action="Revisa la confirmación con el destino. Un POST incierto conserva UNKNOWN y no autoriza reenvío automático.",
                    impact="El destino podría haber aplicado la escritura; no se conoce su confirmación.")
            output = _version_stage(db, user, run.output_version_id, grants)
            if output:
                stages.append(output)
        return result
    except OperationError:
        return None


@router.get("")
def operations(queue: Queue = "all", search: str = Query(default="", max_length=200),
               offset: int = Query(default=0, ge=0), limit: int = Query(default=25, ge=1, le=100),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    grants = set(effective_permissions(db, user))
    # The exhaustive endpoint permission matrix is the main gate. Retain the
    # explicit gate for direct service callers and future mounting changes.
    if "exceptions:read" not in grants:
        raise OperationError(403, "FORBIDDEN", "Tu rol no permite consultar la operación.")
    models = (("acquisition", AcquisitionRun, _queue_expression("acquisition", AcquisitionRun.status, stage=AcquisitionRun.stage)),
        ("run", Run, _queue_expression("run", Run.status, Run.decision, Run.progress_stage)),
        ("exception", ExceptionCase, _queue_expression("exception", ExceptionCase.state)),
        ("document", DocumentOriginal, _queue_expression("document", DocumentOriginal.status)),
        ("document_batch", DocumentBatch, _queue_expression("document_batch", DocumentBatch.status)))
    sources = [select(literal(kind).label("kind"), model.id.label("identity"), model.created_at.label("created_at"), queue_expression.label("queue"))
               .where(model.organization_id == user.organization_id)
               for kind, model, queue_expression in models]
    native = union_all(*sources).subquery()
    metadata = select(native)
    if queue != "all":
        metadata = metadata.where(native.c.queue == queue)
    candidates = db.execute(metadata.order_by(native.c.created_at.desc(), native.c.kind, native.c.identity)
        .offset(offset).limit(1001)).all()
    items, consumed = [], 0
    for candidate in candidates[:1000]:
        consumed += 1
        item = _item(db, user, candidate.kind, candidate.identity, grants, search)
        if item is None or (queue != "all" and item["queue"] != queue):
            continue
        if queue == "intervention" and item.get("assigned_user_id") not in {None, user.id}:
            continue
        if not _matches_search(item, search):
            continue
        items.append(item)
        if len(items) == limit:
            break
    has_more = consumed < len(candidates)
    return {"items": items, "limit": limit, "offset": offset, "next_offset": offset + consumed if has_more else None,
            "has_more": has_more, "coverage": "AUTHORIZED_NATIVE_RECORDS", "scanned_records": consumed}
