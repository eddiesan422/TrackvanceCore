"""Joint metadata resolution, current authorization and report lifecycle."""
from __future__ import annotations

from datetime import UTC, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from .artifactstore import ArtifactIntegrityError
from .db import SessionLocal, iso, utcnow
from .governance import (
    authorize_dataset_content,
    contract_identity,
    dataset_eligibility,
    governance_snapshot,
    strict_approval,
)
from .models import Artifact, Configuration, Dataset, DatasetVersion, Job, Run, User, uid
from .operations_common import OperationError
from .permissions import effective_permissions
from .report_config import ReportLimits
from .report_models import ReportContext, ReportExecution, ReportRevision
from .report_query import compile_draft, digest
from .services import audit


def permission(db: Session, user: User, code: str) -> None:
    if code not in effective_permissions(db, user):
        raise OperationError(403, "FORBIDDEN", "Tu rol no permite esta acción de Reportes.")


def owned(db: Session, model, identity: str | None, user: User):
    if not identity or not isinstance(identity, str):
        raise OperationError(422, "REPORT_RESOURCE_ID", "Falta una identidad estable del recurso.")
    found = db.get(model, identity)
    if found is None or found.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el recurso de Reportes.")
    return found


def resolved_context(db: Session, identity: str, user: User, *, expired_ok: bool = False) -> ReportContext:
    context = owned(db, ReportContext, identity, user)
    if context.user_id != user.id:
        raise OperationError(403, "REPORT_CONTEXT_OWNER", "La selección pertenece a otro usuario.")
    expires = context.expires_at.replace(tzinfo=UTC) if context.expires_at.tzinfo is None else context.expires_at
    if not expired_ok and expires <= utcnow():
        raise OperationError(409, "REPORT_CONTEXT_EXPIRED", "La selección expiró; resuelve nuevamente las fuentes.")
    if digest(context.snapshot) != context.integrity_hash:
        raise OperationError(409, "REPORT_CONTEXT_INTEGRITY", "La selección congelada perdió integridad.")
    return context


def _source(db: Session, request: dict, user: User) -> tuple[dict, list[dict]]:
    if not isinstance(request, dict) or not isinstance(request.get("alias"), str):
        raise OperationError(422, "REPORT_SOURCE_INVALID", "Cada fuente requiere un alias y sus identidades.")
    dataset = owned(db, Dataset, request.get("input_dataset_id"), user)
    revisions = request.get("contract_revision_ids")
    if (not isinstance(revisions, list) or not 1 <= len(revisions) <= 100
            or any(not isinstance(identity, str) for identity in revisions) or len(set(revisions)) != len(revisions)):
        raise OperationError(422, "REPORT_CONTRACT_REVISIONS", "Admite explícitamente entre 1 y 100 revisiones distintas del contrato.")
    for identity in revisions:
        config = owned(db, Configuration, identity, user)
        if config.module != "intake" or config.dataset_id != dataset.id or contract_identity(db, config) != request.get("contract_id"):
            raise OperationError(422, "REPORT_CONTRACT_IDENTITY", "La revisión no pertenece al contrato y dataset seleccionados.")
    policy = request.get("policy", "LATEST_APPROVED")
    query = select(Run, DatasetVersion).join(DatasetVersion, Run.dataset_version_id == DatasetVersion.id).where(
        Run.organization_id == user.organization_id, Run.config_id.in_(revisions),
        DatasetVersion.dataset_id == dataset.id, Run.module == "intake")
    if policy == "SPECIFIC":
        version = owned(db, DatasetVersion, request.get("input_version_id"), user)
        if version.dataset_id != dataset.id:
            raise OperationError(422, "REPORT_INPUT_VERSION", "La entrada no pertenece al dataset seleccionado.")
        query = query.where(Run.dataset_version_id == version.id)
    elif policy != "LATEST_APPROVED":
        raise OperationError(422, "REPORT_VERSION_POLICY", "Selecciona versión específica o última entrada aprobada.")
    selected = None
    # Input ordinal dominates approval completion. Re-running an older input
    # never advances its data-version rank.
    with db.execute(query.order_by(DatasetVersion.version.desc(), Run.finished_at.desc(), Run.id.desc())
                    .execution_options(yield_per=32)) as candidates:
        for run, input_version in candidates:
            approval = strict_approval(db, run)
            if approval["approved"]:
                selected = (run, input_version, approval)
                break
    if selected is None:
        raise OperationError(422, "REPORT_NO_STRICT_APPROVAL", "No existe aprobación estricta verificable de la entrada bajo las revisiones admitidas.",
                             {"alias": request.get("alias")})
    run, input_version, approval = selected
    output = owned(db, DatasetVersion, run.output_version_id, user)
    eligibility = dataset_eligibility(db, user, output, "REPORT")
    # Eligibility belongs to the selected latest input. It cannot cause fallback
    # to an older, easier-to-read output.
    if not eligibility["eligible"]:
        raise OperationError(422, "REPORT_SOURCE_INELIGIBLE", "La fuente seleccionada no está habilitada para Reportes.",
                             {"alias": request.get("alias"), "input_version_id": input_version.id,
                              "output_version_id": output.id, "reasons": eligibility["reasons"]})
    artifact = owned(db, Artifact, output.canonical_artifact_id, user)
    latest = db.scalar(select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset.id)) or input_version.version
    warnings = []
    if latest > input_version.version:
        warnings.append({"code": "REPORT_NEWER_INPUT_PENDING", "alias": request["alias"],
                         "message": "Existe una entrada posterior pendiente o rechazada bajo las revisiones admitidas.", "latest_input_version": latest})
    return {"alias": request["alias"], "input_dataset_id": dataset.id, "input_version_id": input_version.id,
            "input_version": input_version.version, "contract_id": approval["contract_id"],
            "contract_revision_id": run.config_id, "approval_run_id": run.id,
            "approval_finished_at": iso(run.finished_at), "output_dataset_id": output.dataset_id,
            "output_version_id": output.id, "output_version": output.version, "schema": output.schema_json,
            "schema_hash": output.schema_hash, "canonical_artifact_id": artifact.id,
            "canonical_sha256": artifact.sha256, "canonical_size_bytes": (output.ingestion_metadata or {}).get("canonical_size_bytes", artifact.size_bytes),
            "row_count": output.row_count, "policy": policy, "governance": eligibility["governance"]}, warnings


def resolve(db: Session, user: User, draft: dict, revision_id: str | None = None) -> ReportContext:
    bind = db.get_bind()
    options = {"isolation_level": "REPEATABLE READ"} if bind.dialect.name == "postgresql" else {"isolation_level": "SERIALIZABLE"}
    # This new session selects every source at one MVCC snapshot, established
    # before permission/configuration/governance reads, regardless of an HTTP
    # dependency's already-active READ COMMITTED transaction.
    with Session(bind=bind.execution_options(**options), expire_on_commit=False) as joint:
        if bind.dialect.name == "sqlite":
            # sqlite3 legacy transaction mode does not BEGIN for SELECT; an
            # reserve the short metadata write too, avoiding a failed upgrade
            # from a read snapshot when a concurrent SQLite writer commits.
            joint.connection().exec_driver_sql("BEGIN IMMEDIATE")
        actor = joint.get(User, user.id)
        if actor is None:
            raise OperationError(403, "REPORT_ACTOR_REVOKED", "El usuario ya no está disponible.")
        return _resolve_joint(joint, actor, draft, revision_id)


def _resolve_joint(db: Session, user: User, draft: dict, revision_id: str | None) -> ReportContext:
    permission(db, user, "reports:read")
    sources_raw = draft.get("sources", [])
    if not isinstance(sources_raw, list) or not 1 <= len(sources_raw) <= 8:
        raise OperationError(422, "REPORT_SOURCE_LIMIT", "Se admiten entre 1 y 8 fuentes.")
    sources, warnings = [], []
    for request in sources_raw:
        source, notices = _source(db, request, user)
        sources.append(source)
        warnings.extend(notices)
    schemas = {source["alias"]: source["schema"] for source in sources}
    plan = compile_draft(draft, schemas)
    expected = draft.get("expected_schemas", {})
    for alias, columns in expected.items():
        current = {c["name"]: c.get("logical_type") for c in schemas.get(alias, [])}
        prior = {c["name"]: c.get("logical_type") for c in columns}
        for name in plan["used_columns"].get(alias, []):
            if name not in prior or current.get(name) != prior[name]:
                raise OperationError(422, "REPORT_SCHEMA_CHANGED", "Una columna utilizada falta o cambió de tipo; no se seleccionará una versión antigua.", {"alias": alias, "column": name})
    if revision_id:
        revision = owned(db, ReportRevision, revision_id, user)
        def structure(value):
            return {**value, "parameters": [{"name": p.get("name"), "type": p.get("type")}
                                            for p in value.get("parameters", [])]}
        if digest(structure(revision.draft)) != digest(structure(draft)):
            raise OperationError(409, "REPORT_REVISION_CHANGED", "La consulta no corresponde a la revisión indicada.")
    snapshot = {"draft": draft, "plan": plan, "sources": sources, "warnings": warnings,
                "resolved_at": iso(utcnow()), "organization_id": user.organization_id, "user_id": user.id}
    context = ReportContext(id=uid(), organization_id=user.organization_id, user_id=user.id,
                            revision_id=revision_id, expires_at=utcnow() + timedelta(minutes=15),
                            snapshot=snapshot, integrity_hash=digest(snapshot))
    db.add(context)
    db.flush()
    audit(db, "REPORT_RESOLVED", "report_context", context.id, "Fuentes de Reportes congeladas conjuntamente", user.name,
          user.organization_id, {"source_version_id": ",".join(s["output_version_id"] for s in sources), "sha256": context.integrity_hash})
    db.commit()
    return context


def revalidate(db: Session, context: ReportContext, user: User, code: str) -> None:
    current_user = db.get(User, user.id, populate_existing=True)
    if current_user is None or current_user.organization_id != context.organization_id:
        raise OperationError(403, "REPORT_ACTOR_REVOKED", "El usuario o su organización ya no están disponibles.")
    user = current_user
    permission(db, user, code)
    for source in context.snapshot["sources"]:
        output = owned(db, DatasetVersion, source["output_version_id"], user)
        authorize_dataset_content(db, user, output, "REPORT")
        eligible = dataset_eligibility(db, user, output, "REPORT")
        artifact = owned(db, Artifact, output.canonical_artifact_id, user)
        if not eligible["eligible"]:
            raise OperationError(403, "REPORT_SOURCE_REVOKED", "Una fuente congelada ya no está habilitada.", {"reasons": eligible["reasons"]})
        if (output.schema_hash != source["schema_hash"] or output.canonical_artifact_id != source["canonical_artifact_id"]
                or artifact.sha256 != source["canonical_sha256"] or output.source_run_id != source["approval_run_id"]):
            raise OperationError(409, "REPORT_SOURCE_CHANGED", "La fuente congelada ya no coincide con su evidencia.")


def execution_sources(db: Session, context: ReportContext) -> list[dict]:
    from .dataset_scans import version_paths
    result = []
    # Expensive complete artifact verification occurs after the short resolution
    # transaction and outside row/advisory locks.
    for source in context.snapshot["sources"]:
        from .governance import verify_strict_approval_artifacts
        approving_run = db.get(Run, source["approval_run_id"])
        if approving_run is None:
            raise OperationError(422, "REPORT_SOURCE_UNAVAILABLE", "La ejecución aprobatoria congelada no está disponible.")
        verify_strict_approval_artifacts(db, approving_run)
        version = db.get(DatasetVersion, source["output_version_id"])
        if version is None:
            raise OperationError(422, "REPORT_SOURCE_UNAVAILABLE", "La fuente congelada no está disponible.")
        try:
            paths = version_paths(db, version)
        except (ArtifactIntegrityError, OSError):
            raise OperationError(422, "REPORT_SOURCE_INTEGRITY", "No fue posible comprobar las partes y hashes de una fuente congelada.") from None
        result.append({"alias": source["alias"], "schema": source["schema"], "paths": [str(p) for p in paths],
                       "filter": context.snapshot["plan"]["source_filters"].get(source["alias"])})
    return result


def execution_dto(db: Session, execution: ReportExecution) -> dict:
    version = db.get(DatasetVersion, execution.output_version_id) if execution.output_version_id else None
    context = db.get(ReportContext, execution.context_id)
    return {"id": execution.id, "context_id": execution.context_id, "profile": execution.profile,
            "status": execution.status, "generation_status": execution.generation_status,
            "transmission_status": execution.transmission_status, "progress_stage": execution.progress_stage,
            "progress_percent": execution.progress_percent, "metrics": execution.metrics,
            "sources": context.snapshot["sources"] if context else [],
            "output_version_id": execution.output_version_id, "output_dataset_id": version.dataset_id if version else None,
            "error_code": execution.error_code, "error_message": execution.error_message,
            "started_at": iso(execution.started_at), "finished_at": iso(execution.finished_at), "created_at": iso(execution.created_at)}


def start_execution(db: Session, user: User, context: ReportContext, profile: str,
                    publication: dict | None = None, idempotency_key: str | None = None) -> ReportExecution:
    code = {"PREVIEW": "reports:preview", "DOWNLOAD": "reports:download", "DATASET": "reports:generate"}[profile]
    revalidate(db, context, user, code)
    request_hash = digest({"context_id": context.id, "publication": publication})
    if idempotency_key:
        found = db.scalar(select(ReportExecution).where(ReportExecution.organization_id == user.organization_id,
            ReportExecution.user_id == user.id, ReportExecution.idempotency_key == idempotency_key))
        if found:
            if found.request_hash != request_hash:
                raise OperationError(409, "IDEMPOTENCY_CONFLICT", "La clave ya corresponde a otra solicitud.")
            return found
    execution = ReportExecution(id=uid(), organization_id=user.organization_id, user_id=user.id,
        context_id=context.id, profile=profile, status="QUEUED", publication=publication or {},
        idempotency_key=idempotency_key, request_hash=request_hash,
        transmission_status="PENDING" if profile == "DOWNLOAD" else "NOT_APPLICABLE")
    db.add(execution)
    db.flush()
    if profile == "DATASET":
        db.add(Job(organization_id=user.organization_id, report_execution_id=execution.id,
                   lane="REPORT", status="QUEUED"))
    audit(db, "REPORT_EXECUTION_CREATED", "report_execution", execution.id, "Ejecución independiente creada", user.name,
          user.organization_id, {"kind": profile, "sha256": context.integrity_hash})
    db.commit()
    return execution


def admission(db: Session, execution: ReportExecution) -> None:
    """One transactional gate across API and durable report workers."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(8080080)"))
    else:
        # A harmless UPDATE obtains SQLite's database write reservation too.
        db.execute(update(ReportExecution).where(ReportExecution.id == execution.id).values(status=execution.status))
    for running in db.scalars(select(ReportExecution).where(ReportExecution.status == "RUNNING")):
        start = running.started_at.replace(tzinfo=UTC) if running.started_at and running.started_at.tzinfo is None else running.started_at
        if start and start + timedelta(seconds=ReportLimits.configured(running.profile).timeout_seconds + 10) < utcnow():
            running.status, running.error_code, running.finished_at = "INTERRUPTED", "REPORT_EXECUTOR_LOST", utcnow()
            running.transmission_status = "INTERRUPTED" if running.profile == "DOWNLOAD" else "NOT_APPLICABLE"
    db.flush()
    active = db.scalar(select(func.count()).select_from(ReportExecution).where(ReportExecution.status == "RUNNING", ReportExecution.id != execution.id)) or 0
    if active >= ReportLimits.configured().concurrency:
        raise OperationError(429, "REPORT_CONCURRENCY_LIMIT", "El cupo conjunto API/worker de Reportes está ocupado.")
    execution.status, execution.started_at = "RUNNING", utcnow()
    execution.progress_stage, execution.generation_status = "Verificando fuentes", "RUNNING"
    db.commit()


def check_execution(identity: str, permission_code: str) -> None:
    with SessionLocal() as db:
        execution = db.get(ReportExecution, identity)
        if not execution or execution.cancel_requested:
            raise OperationError(409, "REPORT_CANCELLED", "La ejecución fue cancelada.")
        if execution.status != "RUNNING":
            raise OperationError(409, "REPORT_EXECUTOR_LOST", "La ejecución ya no tiene un cupo activo.")
        start = execution.started_at.replace(tzinfo=UTC) if execution.started_at and execution.started_at.tzinfo is None else execution.started_at
        if start and start + timedelta(seconds=ReportLimits.configured(execution.profile).timeout_seconds) <= utcnow():
            raise OperationError(422, "REPORT_TIMEOUT", "La ejecución excedió el tiempo global autorizado.")
        user = db.get(User, execution.user_id)
        if not user:
            raise OperationError(403, "REPORT_ACTOR_REVOKED", "El usuario ya no está disponible.")
        context = resolved_context(db, execution.context_id, user, expired_ok=True)
        revalidate(db, context, user, permission_code)


def terminal(identity: str, *, status: str, error: OperationError | None = None,
             metrics: dict | None = None, generation: str | None = None, transmission: str | None = None) -> None:
    with SessionLocal() as db:
        execution = db.get(ReportExecution, identity)
        if not execution:
            return
        # Transport interruption is a separate fact and never downgrades a
        # stronger persisted generation failure/revocation/cancellation cause.
        if status == "INTERRUPTED" and execution.status in {"FAILED", "CANCELLED"}:
            if transmission:
                execution.transmission_status = transmission
            db.commit()
            return
        execution.status, execution.finished_at = status, utcnow()
        if metrics is not None:
            execution.metrics = metrics
        if generation:
            execution.generation_status = generation
        if transmission:
            execution.transmission_status = transmission
        execution.progress_stage = "Completado" if status == "SUCCESS" else "Interrumpido" if status == "INTERRUPTED" else "Falló" if status == "FAILED" else "Cancelado"
        execution.progress_percent = 100 if status == "SUCCESS" else execution.progress_percent
        if error:
            execution.error_code, execution.error_message = error.code, error.message
        audit(db, "REPORT_EXECUTION_FINISHED", "report_execution", execution.id, execution.progress_stage, "Worker", execution.organization_id,
              {"status": status, "kind": execution.profile, "row_count": (metrics or {}).get("rows"), "error_code": error.code if error else None})
        db.commit()


def list_sources(db: Session, user: User, search: str, offset: int, limit: int,
                 revision_offset: int = 0, version_offset: int = 0, contract_id: str | None = None) -> dict:
    # Reuse the catalog's SQL security closure before count/pagination. Metadata
    # pages never instantiate all organizations' contracts or histories.
    from .governance_api import dataset_query
    authorized = dataset_query(user, search).with_only_columns(Dataset.id).order_by(None)
    roots = select(Configuration).where(Configuration.organization_id == user.organization_id,
        Configuration.module == "intake", Configuration.previous_version_id.is_(None),
        Configuration.dataset_id.in_(authorized))
    if contract_id:
        roots = roots.where(Configuration.id == contract_id)
    total = db.scalar(select(func.count()).select_from(roots.subquery())) or 0
    items = []
    for root in db.scalars(roots.order_by(Configuration.name, Configuration.id).offset(offset).limit(limit)):
        identity = root.id
        chain = select(Configuration.id.label("id")).where(Configuration.id == identity).cte("report_contract_chain", recursive=True)
        chain = chain.union(select(Configuration.id).join(chain, Configuration.previous_version_id == chain.c.id).where(
            Configuration.organization_id == user.organization_id, Configuration.dataset_id == root.dataset_id,
            Configuration.module == "intake"))
        revision_total = db.scalar(select(func.count()).select_from(chain)) or 0
        revisions = list(db.scalars(select(Configuration).where(Configuration.id.in_(select(chain.c.id)))
            .order_by(Configuration.version.desc(), Configuration.id).offset(revision_offset).limit(100)))
        latest_config = db.scalar(select(Configuration).where(Configuration.id.in_(select(chain.c.id)))
            .order_by(Configuration.version.desc(), Configuration.id).limit(1)) or root
        dataset = db.get(Dataset, latest_config.dataset_id)
        assert dataset is not None
        candidates = db.execute(select(Run, DatasetVersion).join(DatasetVersion, Run.dataset_version_id == DatasetVersion.id).where(
            Run.config_id.in_([r.id for r in revisions]), Run.organization_id == user.organization_id,
            Run.module == "intake", Run.status == "SUCCESS").order_by(DatasetVersion.version.desc(), Run.finished_at.desc(), Run.id.desc())
            .execution_options(yield_per=32))
        selection = None
        for run, version in candidates:
            if strict_approval(db, run)["approved"]:
                selection = run, version
                break
        candidates.close()
        output = db.get(DatasetVersion, selection[0].output_version_id) if selection and selection[0].output_version_id else None
        eligibility = dataset_eligibility(db, user, output) if output else {"eligible": False, "reasons": [{"code": "NO_STRICT_APPROVAL", "message": "No existe una salida estrictamente aprobada."}]}
        try:
            from .governance import authorize_dataset
            authorize_dataset(db, user, dataset.id, "METADATA")
        except OperationError:
            continue
        version_query = select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)
        version_total = db.scalar(select(func.count()).select_from(version_query.subquery())) or 0
        versions = list(db.scalars(version_query.order_by(DatasetVersion.version.desc()).offset(version_offset).limit(100)))
        items.append({"input_dataset_id": dataset.id, "name": dataset.name, "contract_id": identity,
            "contract_name": latest_config.name, "contract_revision_ids": [r.id for r in revisions],
            "contract_revisions": [{"id": r.id, "version": r.version} for r in revisions],
            "contract_revision_total": revision_total, "contract_revision_offset": revision_offset, "contract_revision_limit": 100,
            "versions": [{"id": v.id, "version": v.version} for v in versions],
            "version_total": version_total, "version_offset": version_offset, "version_limit": 100,
            "input_version_id": selection[1].id if selection else None, "input_version": selection[1].version if selection else None,
            "output_version_id": output.id if output else None, "output_version": output.version if output else None,
            "schema": output.schema_json if output else [], "eligibility": eligibility,
            "governance": governance_snapshot(db, dataset)})
    return {"items": items, "total": total, "offset": offset, "limit": limit}
