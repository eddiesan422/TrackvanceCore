"""Read-only operational projection over native identities, never another case store.

Pagination uses a raw metadata cursor. Unauthorized resources are removed before
serialization; neither their names nor a global count leak through this endpoint.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import DateTime, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from .acquisition_models import AcquisitionRun
from .acquisition_schedule_models import (
    AcquisitionOccurrence,
    AcquisitionSourceConfiguration,
    AcquisitionSourceRevision,
)
from .db import get_db, iso, utcnow
from .document_models import (
    DocumentBatch,
    DocumentBatchMember,
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
)
from .governance import authorize_dataset, authorize_dataset_content, authorize_run_content
from .models import ArtifactLink, Dataset, DatasetVersion, ExceptionCase, Run, User
from .operation_processes import process_identity, process_roots, readable_root_key
from .operations_common import OperationError
from .permissions import can_read_operations, effective_permissions

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
    if decision in {"UNKNOWN", "PARTIAL", "REJECTED", "ALERT", "WITH_FINDINGS"} or status in {"UNKNOWN", "FAILED", "FAILED_PRECONDITION", "BLOCKED"}:
        return "intervention"
    if stage in {"RETRY_WAIT", "WAITING_RETRY"} or status in {"QUEUED", "ENQUEUED", "WAITING", "RETRY_WAIT"}:
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
    return case((or_(decision.in_({"UNKNOWN", "PARTIAL", "REJECTED", "ALERT", "WITH_FINDINGS"}), status.in_({"UNKNOWN", "FAILED", "FAILED_PRECONDITION", "BLOCKED"})), "intervention"),
        (or_(stage.in_({"RETRY_WAIT", "WAITING_RETRY"}), status.in_({"QUEUED", "ENQUEUED", "WAITING", "RETRY_WAIT"})), "waiting"),
        (status == "RUNNING", "running"), else_="finished")


def _duration(record) -> float | None:
    if not record.started_at:
        return None
    return max(0, ((record.finished_at or utcnow()).replace(tzinfo=UTC) - record.started_at.replace(tzinfo=UTC)).total_seconds())


def _tracking(db, version: DatasetVersion | None, organization_id: str) -> str | None:
    return process_identity(db, organization_id, "version", version.id) if version else None


def _stage(kind, record, title, href=None):
    plan = getattr(record, "execution_plan", None) or {}
    origin = (getattr(record, "source_snapshot", None) or {}).get("origin") or next((
        plan[name].get("origin", "AUTOMATIC" if name == "sentinel_automatic" else "SCHEDULED")
        for name in ("automation", "intake_schedule", "schedule", "sentinel_automatic") if plan.get(name)),
        "SCHEDULED" if kind == "occurrence" else "MANUAL")
    return {"id": f"{kind}:{record.id}", "title": title, "state": getattr(record, "state", getattr(record, "status", None)),
            "created_at": iso(record.created_at), "href": href,
            "decision": getattr(record, "decision", None), "attempts": getattr(record, "attempts", None),
            "module": getattr(record, "module", "acquisition" if kind in {"acquisition", "occurrence"} else "documents"),
            "origin": "AUTOMATIC" if origin == "DATASET_VERSION_PUBLISHED" else origin}


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


def _downstream_stages(db, user, version_id, grants, process=None):
    """Bounded native input/output traversal, including approved Intake outputs.

    The bound is reported to the viewer. A missing delivery record never becomes
    a fabricated pending or completed delivery stage.
    """
    process = process or process_identity(db, user.organization_id, "version", version_id)
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
            Run.dataset_version_id == current,
            or_(Run.execution_plan["sentinel_automatic"]["event_id"].as_string().is_not(None),
                Run.execution_plan["intake_schedule"]["origin"].as_string() == "EVENT",
                Run.execution_plan["automation"]["origin"].as_string() == "CHAINED"))
            .order_by(Run.created_at).limit(21)).all()
        for run in runs:
            if run.id in seen_runs:
                continue
            if len(seen_runs) == 20:
                return stages, True
            if f"{run.module.lower()}:read" not in grants:
                continue
            if process_identity(db, user.organization_id, "run", run.id) != process:
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
    stages = " ".join(f"{stage.get('title', '')} {stage['id']}" for stage in item.get("stages", []))
    return search.casefold() in f"{item['title']} {item['tracking_id']} {item.get('diagnosis') or ''} {stages}".casefold()


def _item(db, user, kind, identity, grants, search=""):
    model = {"acquisition": AcquisitionRun, "run": Run, "exception": ExceptionCase, "occurrence": AcquisitionOccurrence,
             "acquisition_schedule": AcquisitionSourceConfiguration,
             "document": DocumentOriginal, "document_batch": DocumentBatch}[kind]
    record = db.get(model, identity)
    if not record or record.organization_id != user.organization_id:
        return None
    stages: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    result = {"id": f"{kind}:{identity}", "kind": kind, "resource_id": identity,
              "created_at": iso(record.created_at), "stages": stages, "actions": actions}
    try:
        if kind == "acquisition_schedule":
            diagnostic = getattr(record, "dispatch_diagnostic", None)
            if "datasets:read" not in grants or not diagnostic:
                return None
            authorize_dataset(db, user, record.dataset_id)
            dataset = db.get(Dataset, record.dataset_id)
            result.update(title=f"Programación degradada · {dataset.name}", tracking_id=f"acquisition_schedule:{identity}",
                state="BLOCKED", queue="intervention", module="acquisition", origin="SCHEDULED",
                created_at=diagnostic.get("recorded_at") or iso(record.updated_at),
                diagnosis=diagnostic.get("message") or "La programación no pudo registrar este corte.",
                error_code=diagnostic.get("code"), planned_at=diagnostic.get("planned_at"),
                impact="No se registró una ocurrencia ni una consulta para este corte; no hay nueva publicación acreditada.",
                impact_level="CONFIRMED", dependency_impact="NOT_CALCULATED", functional_result="NOT_DISPATCHED",
                next_action="Revisa la configuración y el diagnóstico del despacho; el siguiente corte requiere una programación válida.")
            actions.append({"title": "Revisar programación", "href": f"/datasets/{record.dataset_id}"})
        elif kind == "occurrence":
            if "datasets:read" not in grants:
                return None
            config = db.get(AcquisitionSourceConfiguration, record.configuration_id)
            revision = db.get(AcquisitionSourceRevision, record.revision_id)
            if not config or not revision or config.organization_id != user.organization_id or revision.organization_id != user.organization_id:
                return None
            authorize_dataset(db, user, config.dataset_id)
            dataset = db.get(Dataset, config.dataset_id)
            actor = db.get(User, (revision.settings or {}).get("responsible_user_id"))
            result.update(title=f"Corte programado · {dataset.name}", tracking_id=f"occurrence:{identity}",
                state=record.status, queue=operational_queue(kind, record.status), module="acquisition", origin="SCHEDULED",
                owner=actor.name if actor and actor.organization_id == user.organization_id else "Responsable no disponible",
                assigned_user_id=actor.id if actor and actor.organization_id == user.organization_id else None,
                planned_at=iso(record.planned_at), error_code=record.reason_code,
                diagnosis=getattr(record, "reason_message", None) or "Consulta el motivo registrado en el detalle del corte.",
                impact="Este corte no ejecutó una consulta ni publicó una versión nueva." if not record.acquisition_id else "El corte registró un trabajo de adquisición; consulta su resultado.",
                impact_level="CONFIRMED", dependency_impact="NOT_CALCULATED",
                functional_result="NOT_EXECUTED" if not record.acquisition_id else "DISPATCHED",
                next_action="Revisa el motivo, la disponibilidad y los permisos del responsable antes del próximo corte.")
            stages.append(_stage(kind, record, "Corte programado", f"/datasets/{config.dataset_id}?occurrence={identity}"))
            actions.append({"title": "Revisar programación", "href": f"/datasets/{config.dataset_id}?occurrence={identity}"})
            if record.acquisition_id:
                acquisition = _item(db, user, "acquisition", record.acquisition_id, grants)
                if acquisition:
                    stages.extend(acquisition["stages"])
                    actions.extend(acquisition["actions"])
                    for field in ("queue", "state", "diagnosis", "impact", "impact_level", "functional_result", "next_action", "attempts"):
                        if field in acquisition:
                            result[field] = acquisition[field]
        elif kind in {"document", "document_batch"}:
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
                if record.is_example:
                    return None
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
                counts = dict(db.execute(select(DocumentBatchMember.status, func.count()).where(
                    DocumentBatchMember.organization_id == user.organization_id, DocumentBatchMember.batch_id == identity)
                    .group_by(DocumentBatchMember.status)).all())
                pending = sum(count for state, count in counts.items() if state not in {"READY", "PUBLISHED", "EXCLUDED"})
                policy = (record.settings or {}).get("close_policy") or (record.settings or {}).get("policy")
                result.update(impact=f"{pending} documentos pendientes en este lote. " + (
                    "La política estricta impide publicarlo hasta resolver sus pendientes." if policy in {"STRICT", "strict"}
                    else "Consulta la política de cierre y el diferimiento registrado antes de publicar."),
                    impact_level="CONFIRMED", pending_documents=pending)
                if not _matches_search(result, search):
                    return None
                stages.append(_stage("document-batch", record, "Lote documental", href))
                members = db.scalars(select(DocumentBatchMember).where(DocumentBatchMember.organization_id == user.organization_id,
                    DocumentBatchMember.batch_id == identity).order_by(DocumentBatchMember.created_at).limit(21)).all()
                result["more_stages"] = len(members) > 20
                for member in members[:20]:
                    document = _item(db, user, "document", member.document_id, grants)
                    if document:
                        stages.extend(stage for stage in document["stages"] if stage["id"].startswith(("document:", "extraction:")))
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
            latest = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == record.dataset_id,
                DatasetVersion.organization_id == user.organization_id, DatasetVersion.profile_status == "READY")
                .order_by(DatasetVersion.version.desc()).limit(1)) if dataset else None
            if record.status in {"FAILED", "CANCELLED"}:
                result.update(impact="Esta adquisición no publicó una versión nueva. " + (
                    f"La versión válida v{latest.version} permanece disponible." if latest else "No se ha registrado una versión válida disponible."),
                    impact_level="CONFIRMED", functional_result="NOT_PUBLISHED")
            else:
                result.update(impact_level="CONFIRMED" if record.output_version_id else "NOT_CALCULATED",
                    functional_result="PUBLISHED" if record.output_version_id else "NO_CHANGES" if record.status == "SUCCESS" else "PENDING")
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
            if run is None or f"{'delivery' if run.module == 'DELIVERY_PREFLIGHT' else run.module.lower()}:read" not in grants:
                return None
            if kind == "exception" and "exceptions:read" not in grants:
                return None
            result.update(tracking_id=f"run:{run.id}",
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
            result.update(functional_result=run.decision or "NOT_CALCULATED", impact_level="NOT_CALCULATED")
            if run.module == "intake" and run.decision == "REJECTED":
                metrics = run.metrics or {}
                count = metrics.get("error_rows")
                result.update(impact=(f"La validación rechazó la entrada y registró {count} filas con errores. "
                    if type(count) is int else "La validación rechazó la entrada. ") +
                    "Esta ejecución no habilita la continuación automática a Delivery que exige aprobación estricta.",
                    impact_level="CONFIRMED", next_action="Revisa los hallazgos y corrige el origen o la regla; ejecuta una nueva validación.")
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
                    impact="La confirmación es insuficiente. El reenvío automático permanece bloqueado; el destino podría haber aplicado la escritura.",
                    impact_level="CONFIRMED", dependency_impact="POTENTIAL")
            output = _version_stage(db, user, run.output_version_id, grants)
            if output:
                stages.append(output)
        result.setdefault("module", "documents" if kind in {"document", "document_batch"} else "acquisition")
        if "origin" not in result:
            plan = (getattr(record, "execution_plan", None) or {})
            result["origin"] = (getattr(record, "source_snapshot", None) or {}).get("origin") or next((plan[name].get("origin", "AUTOMATIC" if name == "sentinel_automatic" else "SCHEDULED") for name in ("automation", "intake_schedule", "schedule", "sentinel_automatic") if plan.get(name)), "MANUAL")
            if result["origin"] == "DATASET_VERSION_PUBLISHED":
                result["origin"] = "AUTOMATIC"
        result.setdefault("functional_result", "PUBLISHED" if result["state"] == "PUBLISHED" else "PENDING")
        result.setdefault("impact_level", "NOT_CALCULATED")
        result["technical_state"] = result["state"]
        result["intervention_required"] = result["queue"] == "intervention"
        return result
    except OperationError:
        return None


def process_item(db, user, process, grants):
    """Authorize the anchor before allowing its identity into a grouped view."""
    kind, _, identity = process.partition(":")
    if kind not in {"acquisition", "acquisition_schedule", "occurrence", "run", "document", "document_batch", "exception"}:
        return None
    return _item(db, user, kind, identity, grants)


def group_item(db, user, item, grants):
    process = process_identity(db, user.organization_id,
        "run" if item["kind"] == "exception" else item["kind"],
        db.get(ExceptionCase, item["resource_id"]).run_id if item["kind"] == "exception" else item["resource_id"])
    anchor = process_item(db, user, process, grants)
    if anchor is None:
        process, anchor = item["id"], item
    anchor["process_id"] = process
    anchor["tracking_id"] = process
    assigned = {anchor.get("assigned_user_id")}
    # Only recorded direct automatic continuations join a producer. Scheduled
    # and manual consumers of the same input are separate processes.
    versions = [stage["id"].split(":", 1)[1] for stage in anchor["stages"] if stage["id"].startswith("version:")]
    for version in versions[:20]:
        linked, partial = _downstream_stages(db, user, version, grants, process)
        anchor["stages"].extend(linked)
        anchor["more_stages"] = anchor.get("more_stages", False) or partial
    if "exceptions:read" in grants:
        run_ids = [stage["id"].split(":", 1)[1] for stage in anchor["stages"] if stage["id"].startswith("run:")]
        cases = db.scalars(select(ExceptionCase).where(ExceptionCase.organization_id == user.organization_id,
            ExceptionCase.run_id.in_(run_ids)).order_by(ExceptionCase.created_at).limit(21)).all() if run_ids else []
        anchor["more_stages"] = anchor.get("more_stages", False) or len(cases) > 20
        for case_record in cases[:20]:
            case_item = _item(db, user, "exception", case_record.id, grants)
            if case_item:
                assigned.add(case_item.get("assigned_user_id"))
                anchor["stages"].extend({**stage, "closure_type": case_item.get("closure_type")}
                    for stage in case_item["stages"] if stage["id"].startswith("exception:"))
                anchor["actions"].extend(action for action in case_item["actions"] if action["href"].startswith("/exceptions"))
    anchor["stages"] = list({stage["id"]: stage for stage in anchor["stages"]}.values())
    needs_attention = any(operational_queue("exception" if stage["id"].startswith("exception:") else "run",
        stage.get("state"), decision=stage.get("decision")) == "intervention" for stage in anchor["stages"])
    if needs_attention:
        anchor["queue"], anchor["intervention_required"] = "intervention", True
        for stage in anchor["stages"]:
            if stage["id"].startswith("run:") and operational_queue("run", stage.get("state"), decision=stage.get("decision")) == "intervention":
                child = _item(db, user, "run", stage["id"].split(":", 1)[1], grants)
                if child:
                    for field in ("impact", "impact_level", "functional_result", "diagnosis", "next_action"):
                        anchor[field] = child[field]
    anchor["stage_count"] = len(anchor["stages"])
    anchor["modules"] = sorted({anchor["module"], *(stage["module"] for stage in anchor["stages"] if stage.get("module"))})
    anchor["origins"] = sorted({anchor["origin"], *(stage["origin"] for stage in anchor["stages"] if stage.get("origin"))})
    anchor["technical_states"] = sorted({anchor["technical_state"], *(stage["state"] for stage in anchor["stages"] if stage.get("state"))})
    anchor["functional_results"] = sorted({anchor["functional_result"], *(stage["decision"] for stage in anchor["stages"] if stage.get("decision"))})
    anchor["queues"] = sorted({anchor["queue"], *(operational_queue(stage["id"].split(":", 1)[0].replace("-", "_"),
        stage.get("state"), decision=stage.get("decision")) for stage in anchor["stages"]
        if not stage["id"].startswith(("version:", "extraction:")))})
    anchor["assigned_user_ids"] = sorted(identity for identity in assigned if identity)
    return anchor


def _filter_matches(item, module, origin, status, result):
    return all(not value or value in item.get(collection, [item.get(field)])
        for field, collection, value in (("module", "modules", module), ("origin", "origins", origin),
            ("technical_state", "technical_states", status), ("functional_result", "functional_results", result)))


def _queue_matches(item, queue):
    return queue == "all" or queue in item.get("queues", [item["queue"]])


def _within_dates(item, from_date, until_date):
    def aware(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    instant = aware(datetime.fromisoformat(item["created_at"]))
    return (not from_date or instant >= aware(from_date)) and (not until_date or instant <= aware(until_date))


@router.get("")
def operations(queue: Queue = "all", search: str = Query(default="", max_length=200),
               offset: int = Query(default=0, ge=0), limit: int = Query(default=25, ge=1, le=100),
               grouped: bool = False, module: str | None = None, origin: str | None = None,
               status: str | None = None, result: str | None = None,
               from_date: datetime | None = None, until_date: datetime | None = None, process: str | None = None,
               dataset_id: str | None = None, version_id: str | None = None,
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    grants = set(effective_permissions(db, user))
    # The exhaustive endpoint permission matrix is the main gate. Retain the
    # explicit gate for direct service callers and future mounting changes.
    if not can_read_operations(grants):
        raise OperationError(403, "FORBIDDEN", "Tu rol no permite consultar la operación.")
    related_scope = bool(dataset_id or version_id)
    empty = {"items": [], "limit": limit, "offset": offset, "next_offset": None,
        "has_more": False, "coverage": "AUTHORIZED_NATIVE_RECORDS", "scanned_records": 0}
    if related_scope:
        try:
            if version_id:
                selected = db.get(DatasetVersion, version_id)
                if not selected or selected.organization_id != user.organization_id or dataset_id and selected.dataset_id != dataset_id:
                    return empty
                authorize_dataset_content(db, user, selected)
                dataset_id = selected.dataset_id
            elif dataset_id:
                authorize_dataset(db, user, dataset_id)
        except OperationError:
            return empty
    if process and not related_scope:
        item = process_item(db, user, process, grants)
        if item and grouped:
            item = group_item(db, user, item, grants)
        if item and (not _queue_matches(item, queue) or not _matches_search(item, search)
                or not _filter_matches(item, module, origin, status, result)
                or not _within_dates(item, from_date, until_date)
                or queue == "intervention" and item.get("assigned_user_id") not in {None, user.id} and user.id not in item.get("assigned_user_ids", [])):
            item = None
        return {"items": [item] if item else [], "limit": limit, "offset": offset, "next_offset": None,
            "has_more": False, "coverage": "AUTHORIZED_NATIVE_RECORDS", "scanned_records": int(item is not None)}
    models = (("acquisition", AcquisitionRun, _queue_expression("acquisition", AcquisitionRun.status, stage=AcquisitionRun.stage)),
        ("run", Run, _queue_expression("run", Run.status, Run.decision, Run.progress_stage)),
        ("exception", ExceptionCase, _queue_expression("exception", ExceptionCase.state)),
        ("document", DocumentOriginal, _queue_expression("document", DocumentOriginal.status)),
        ("document_batch", DocumentBatch, _queue_expression("document_batch", DocumentBatch.status)),
        ("occurrence", AcquisitionOccurrence, _queue_expression("occurrence", AcquisitionOccurrence.status)),
        ("acquisition_schedule", AcquisitionSourceConfiguration, literal("intervention")))
    diagnostic_time = AcquisitionSourceConfiguration.dispatch_diagnostic["recorded_at"].as_string()
    schedule_time = (cast(diagnostic_time, DateTime(timezone=True)) if db.get_bind().dialect.name == "postgresql"
        else func.datetime(diagnostic_time, type_=DateTime(timezone=True)))
    modules = [value for value in ("intake", "recon", "sentinel", "DELIVERY") if f"{value.lower()}:read" in grants]
    if "delivery:read" in grants:
        modules.append("DELIVERY_PREFLIGHT")
    selected_versions = select(DatasetVersion.id).where(DatasetVersion.organization_id == user.organization_id,
        DatasetVersion.id == version_id if version_id else DatasetVersion.dataset_id == dataset_id)
    related_runs = select(Run.id).where(Run.organization_id == user.organization_id,
        or_(Run.dataset_version_id.in_(selected_versions), Run.target_version_id.in_(selected_versions),
            Run.output_version_id.in_(selected_versions), Run.id.in_(select(ArtifactLink.source_id).where(
                ArtifactLink.organization_id == user.organization_id, ArtifactLink.source_type == "RUN",
                ArtifactLink.target_type == "DATASET_VERSION", ArtifactLink.relation == "RUN_REFERENCE",
                ArtifactLink.target_id.in_(selected_versions)))))
    related_acquisitions = select(AcquisitionRun.id).where(AcquisitionRun.organization_id == user.organization_id,
        AcquisitionRun.output_version_id.in_(selected_versions))
    related_batches = select(DatasetVersion.ingestion_metadata["document_publication"]["batch_id"].as_string()).where(
        DatasetVersion.organization_id == user.organization_id,
        DatasetVersion.id == version_id if version_id else DatasetVersion.dataset_id == dataset_id)
    scope_conditions = {"run": Run.id.in_(related_runs), "exception": ExceptionCase.run_id.in_(related_runs),
        "acquisition": AcquisitionRun.id.in_(related_acquisitions) if version_id else AcquisitionRun.dataset_id == dataset_id,
        "occurrence": AcquisitionOccurrence.acquisition_id.in_(related_acquisitions) if version_id else
            AcquisitionOccurrence.configuration_id.in_(select(AcquisitionSourceConfiguration.id).where(AcquisitionSourceConfiguration.dataset_id == dataset_id)),
        "acquisition_schedule": literal(False) if version_id else AcquisitionSourceConfiguration.dataset_id == dataset_id,
        "document_batch": DocumentBatch.id.in_(related_batches),
        "document": DocumentOriginal.id.in_(select(DocumentBatchMember.document_id).where(DocumentBatchMember.batch_id.in_(related_batches)))}
    sources = []
    for kind, model, queue_expression in models:
        if kind == "exception" and "exceptions:read" not in grants or kind in {"document", "document_batch"} and "documents:read" not in grants or kind in {"acquisition", "occurrence", "acquisition_schedule"} and "datasets:read" not in grants or kind == "run" and not modules:
            continue
        source = select(literal(kind).label("kind"), model.id.label("identity"),
            (func.coalesce(schedule_time, AcquisitionSourceConfiguration.updated_at) if kind == "acquisition_schedule" else model.created_at).label("created_at"), queue_expression.label("queue"))
        source = source.where(model.organization_id == user.organization_id)
        if kind == "run":
            source = source.where(Run.module.in_(modules))
        elif kind == "exception":
            source = source.where(ExceptionCase.run_id.in_(select(Run.id).where(Run.organization_id == user.organization_id, Run.module.in_(modules))))
        elif kind == "document":
            source = source.where(DocumentOriginal.is_example.is_(False))
        elif kind == "acquisition_schedule":
            source = source.where(AcquisitionSourceConfiguration.dispatch_diagnostic["code"].as_string().is_not(None))
        if related_scope:
            source = source.where(scope_conditions[kind])
        sources.append(source)
    if not sources:
        return empty
    native = union_all(*sources).subquery()
    metadata = select(native)
    # Legacy raw views retain their SQL prefilter. Grouped views must consider
    # a failed child even when the producer itself completed successfully.
    if queue != "all" and not grouped:
        metadata = metadata.where(native.c.queue == queue)
    if from_date:
        metadata = metadata.where(native.c.created_at >= from_date)
    if until_date:
        metadata = metadata.where(native.c.created_at <= until_date)
    ordering = native
    if grouped and related_scope:
        scoped = metadata.subquery("operation_related_scope")
        roots = process_roots(select(scoped.c.kind, scoped.c.identity), user.organization_id).subquery("operation_related_roots")
        process_key = readable_root_key(roots, grants, user.organization_id)
        ranked = select(scoped, process_key.label("process_key"), func.row_number().over(
            partition_by=process_key, order_by=(scoped.c.created_at.desc(), scoped.c.kind, scoped.c.identity)).label("group_rank"))
        ranked = ranked.join(roots, (roots.c.kind == scoped.c.kind) & (roots.c.identity == scoped.c.identity))
        if process:
            ranked = ranked.where((process_key == process) | (scoped.c.kind + ":" + scoped.c.identity == process))
        anchors = ranked.subquery("operation_related_anchors")
        metadata = select(anchors).where(anchors.c.group_rank == 1)
        ordering = anchors
    candidates = db.execute(metadata.order_by(ordering.c.created_at.desc(), ordering.c.kind, ordering.c.identity)
        .offset(offset).limit(1001)).all()
    items, consumed, seen = [], 0, set()
    for candidate in candidates[:1000]:
        consumed += 1
        item = _item(db, user, candidate.kind, candidate.identity, grants, "" if grouped else search)
        if item is None:
            continue
        if grouped:
            item = group_item(db, user, item, grants)
            # Only anchors emit a group. This remains stable across raw cursor
            # pages; aliases are represented as stages of their native anchor.
            if not related_scope and candidate.kind + ":" + candidate.identity != item["process_id"]:
                continue
        if not _queue_matches(item, queue):
            continue
        if not _filter_matches(item, module, origin, status, result):
            continue
        if process and process not in {item.get("process_id"), item["id"], item["tracking_id"]}:
            continue
        key = item.get("process_id", item["id"])
        if key in seen:
            continue
        seen.add(key)
        if queue == "intervention" and item.get("assigned_user_id") not in {None, user.id} and user.id not in item.get("assigned_user_ids", []):
            continue
        if not _matches_search(item, search):
            continue
        items.append(item)
        if len(items) == limit:
            break
    has_more = consumed < len(candidates)
    return {"items": items, "limit": limit, "offset": offset, "next_offset": offset + consumed if has_more else None,
            "has_more": has_more, "coverage": "AUTHORIZED_NATIVE_RECORDS", "scanned_records": consumed}
