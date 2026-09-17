import hashlib
import json
from collections import Counter
from datetime import UTC
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from . import __version__
from .artifactstore import (
    ArtifactIntegrityError,
    artifact_dto,
    artifact_store,
    link_artifact,
    storage_provider,
)
from .audit_context import Actor, actor_context, legacy_actor, request_id_context, sanitize_metadata
from .config import ORG_ID
from .dataset_readers import delimited_record_lines, read_dataset
from .db import iso, require_record, utcnow
from .jobqueue import JobQueue, job_queue
from .manifests import SCHEMA_VERSION, configuration_hash
from .models import (
    Artifact,
    ArtifactLink,
    AuditEvent,
    Configuration,
    Dataset,
    DatasetVersion,
    ExceptionCase,
    Finding,
    Job,
    Run,
    SentinelMetricHistory,
    User,
    uid,
)
from .processing import (
    ProcessingError,
    csv_record_lines,
    intake,
    profile_frame,
    reconcile,
    sentinel,
)


def resolve_actor(db: Session, actor: Actor | str, organization_id: str) -> tuple[Actor, bool]:
    if isinstance(actor, Actor):
        return actor, False
    context = actor_context.get()
    if context and (actor in {"Sistema", context.display_name}):
        return context, False
    users = db.scalars(select(User).where(User.organization_id == organization_id, User.name == actor)).all()
    if len(users) == 1:
        return Actor("USER", users[0].id, users[0].name), True
    if actor in {"Sistema", "Worker"}:
        return Actor("WORKER" if actor == "Worker" else "SYSTEM", f"trackvance:{actor.lower()}", actor), False
    return legacy_actor(actor, organization_id), True


def run_actor(run: Run) -> Actor:
    return Actor(run.initiated_by_type, run.initiated_by_id, run.initiated_by)


def audit(db: Session, event_type: str, subject_type: str, subject_id: str, message: str,
          actor="Sistema", organization_id=ORG_ID, metadata=None, run_id: str | None = None):
    identity, legacy = resolve_actor(db, actor, organization_id)
    metadata = sanitize_metadata(metadata)
    if legacy:
        metadata["legacy_identity"] = True
    db.add(AuditEvent(event_type=event_type, subject_type=subject_type, subject_id=subject_id,
                      message=message, actor=identity.display_name, actor_type=identity.type, actor_id=identity.id,
                      actor_legacy=legacy, request_id=request_id_context.get(),
                      run_id=run_id or (subject_id if subject_type == "run" else None),
                      organization_id=organization_id, metadata_json=metadata))


def version_dto(v: DatasetVersion, db: Session | None = None) -> dict:
    artifacts = []
    lineage = []
    if db is not None:
        identities = [identity for identity in (v.original_artifact_id, v.canonical_artifact_id) if identity]
        artifacts = [artifact_dto(item) for item in db.scalars(select(Artifact).where(Artifact.id.in_(identities), Artifact.organization_id == v.organization_id))]
        lineage = [{"relation": item.relation, "source_type": item.source_type, "source_id": item.source_id,
                    "target_type": item.target_type, "target_id": item.target_id} for item in db.scalars(
                        select(ArtifactLink).where(ArtifactLink.organization_id == v.organization_id,
                            or_(and_(ArtifactLink.source_type == "DATASET_VERSION", ArtifactLink.source_id == v.id),
                                and_(ArtifactLink.target_type == "DATASET_VERSION", ArtifactLink.target_id == v.id))))]
    return {"id": v.id, "dataset_id": v.dataset_id, "version": v.version, "filename": v.filename,
            "source_type": v.source_type, "sha256": v.sha256, "schema_hash": v.schema_hash,
            "size_bytes": v.size_bytes, "row_count": v.row_count, "column_count": v.column_count,
            "profile_status": v.profile_status, "created_at": iso(v.created_at), "schema": v.schema_json,
            "profile": v.profile, "ingestion_metadata": v.ingestion_metadata or {},
            "parent_version_id": v.parent_version_id,
            "original_artifact_id": v.original_artifact_id, "canonical_artifact_id": v.canonical_artifact_id,
            "source_run_id": v.source_run_id, "artifacts": artifacts, "lineage": lineage,
            "is_derived": v.source_type == "INTAKE_OUTPUT", "has_original_upload": bool(v.original_artifact_id)}


DATASET_ORIGINS = {
    "UPLOAD": ("MANUAL", "Manual"),
    "INTAKE_OUTPUT": ("DATA_INTAKE", "Data Intake"),
    "DEMO": ("DEMO", "Demo"),
    "GENERATED_DEMO": ("DEMO", "Demo"),
}


def dataset_origin(source_type: str | None) -> tuple[str, str]:
    """Return the stable origin code and its business-facing label.

    Dataset origin belongs to its latest immutable version rather than to the
    dataset container. Unknown future connector types remain visible through a
    readable fallback instead of being mislabeled as manual uploads.
    """
    normalized = (source_type or "").strip().upper()
    if not normalized:
        return "UNKNOWN", "Sin versiones"
    return DATASET_ORIGINS.get(normalized, (normalized, normalized.replace("_", " ").title()))


def dataset_dto(db: Session, d: Dataset) -> dict:
    versions = db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == d.id).order_by(DatasetVersion.version.desc())).all()
    latest = versions[0] if versions else None
    origin, origin_label = dataset_origin(latest.source_type if latest else None)
    return {"id": d.id, "name": d.name, "description": d.description, "domain": d.domain,
            "owner": d.owner, "criticality": d.criticality, "status": d.status,
            "created_at": iso(d.created_at), "version_count": len(versions),
            "row_count": latest.row_count if latest else 0, "column_count": latest.column_count if latest else 0,
            "latest_version_id": latest.id if latest else None,
            "origin": origin, "origin_label": origin_label,
            "origin_source_type": latest.source_type if latest else None,
            "updated_at": iso(latest.created_at if latest else d.created_at)}


def run_dto(db: Session, run: Run) -> dict:
    version = db.get(DatasetVersion, run.dataset_version_id)
    dataset = db.get(Dataset, version.dataset_id) if version else None
    job = db.scalar(select(Job).where(Job.run_id == run.id))
    return {"id": run.id, "run_id": run.id, "job_id": job.id if job else None,
            "module": run.module, "name": run.name, "status": run.status, "decision": run.decision,
            "config_id": run.config_id, "dataset_version_id": run.dataset_version_id,
            "target_version_id": run.target_version_id, "dataset_name": dataset.name if dataset else "",
            "created_at": iso(run.created_at), "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
            "progress_percent": run.progress_percent, "progress_stage": run.progress_stage,
            "metrics": run.metrics, "execution_plan": run.execution_plan, "error": run.error,
            "output_version_id": run.output_version_id, "initiated_by": run_actor(run).as_dict()}


def config_dto(db: Session, c: Configuration) -> dict:
    dataset, target = require_record(db, Dataset, c.dataset_id), db.get(Dataset, c.target_dataset_id) if c.target_dataset_id else None
    latest = db.scalar(select(Run).where(Run.config_id == c.id).order_by(Run.created_at.desc()))
    return {"id": c.id, "name": c.name, "module": c.module, "version": c.version,
            "dataset_id": c.dataset_id, "dataset_name": dataset.name, "target_dataset_id": c.target_dataset_id,
            "target_dataset_name": target.name if target else None, "owner": c.owner,
            "description": c.description, "status": c.status, "config": c.config,
            "previous_version_id": c.previous_version_id,
            "created_at": iso(c.created_at), "latest_run": run_dto(db, latest) if latest else None}


ADMINISTRATIVE_EXCEPTION_STATES = frozenset({"DISCARDED", "ACCEPTED", "NOT_APPLICABLE"})


def _validation_run_summary(run: Run | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "decision": run.decision,
        "created_at": iso(run.created_at),
        "finished_at": iso(run.finished_at),
    }


def _latest_validation_candidate(db: Session, case: ExceptionCase) -> Run | None:
    origin = db.get(Run, case.run_id)
    if origin is None:
        return None
    return db.scalar(
        select(Run)
        .where(
            Run.organization_id == case.organization_id,
            Run.config_id == case.configuration_id,
            Run.status == "SUCCESS",
            Run.id != origin.id,
            Run.created_at > origin.created_at,
        )
        .order_by(Run.created_at.desc(), Run.id.desc())
    )


def assess_exception_validation(
    db: Session,
    case: ExceptionCase,
    requested_run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate the newest successful later run without mutating history.

    The exact immutable configuration id is used deliberately. A newer version
    with relaxed rules is a different configuration and cannot prove that the
    original control now passes.
    """
    if case.state == "RESOLVED":
        if case.validation_run_id and (case.validation_evidence or {}).get("status") == "VALIDATED":
            evidence = case.validation_evidence or {}
            return {
                "status": "VALIDATED",
                "eligible": True,
                "validated": True,
                "can_resolve": False,
                "reason": evidence.get("reason", "La corrección fue validada técnicamente."),
                "candidate_run_id": case.validation_run_id,
                "validation_run_id": case.validation_run_id,
                "validated_at": iso(case.validated_at),
                "evidence": evidence,
            }
        return {
            "status": "LEGACY_UNVERIFIED",
            "eligible": False,
            "validated": False,
            "can_resolve": False,
            "reason": "Cierre histórico anterior a la validación técnica estructurada.",
            "candidate_run_id": None,
            "validation_run_id": None,
            "validated_at": None,
            "evidence": case.validation_evidence or {},
        }
    if case.state in ADMINISTRATIVE_EXCEPTION_STATES or case.state == "FALSE_POSITIVE":
        return {
            "status": "NOT_REQUESTED",
            "eligible": False,
            "validated": False,
            "can_resolve": False,
            "reason": "La excepción se cerró mediante una decisión administrativa.",
            "candidate_run_id": None,
            "validation_run_id": None,
            "validated_at": None,
            "evidence": case.validation_evidence or {},
        }

    candidate = _latest_validation_candidate(db, case)
    if candidate is None:
        return {
            "status": "NO_LATER_RUN",
            "eligible": False,
            "validated": False,
            "can_resolve": False,
            "reason": "Aún no existe una ejecución posterior exitosa del mismo control o configuración.",
            "candidate_run_id": None,
            "validation_run_id": None,
            "validated_at": None,
            "evidence": case.validation_evidence or {},
        }
    if requested_run_id and requested_run_id != candidate.id:
        return {
            "status": "INVALID_RUN",
            "eligible": False,
            "validated": False,
            "can_resolve": False,
            "reason": "La ejecución indicada no es la ejecución posterior exitosa más reciente del mismo control.",
            "candidate_run_id": candidate.id,
            "validation_run_id": None,
            "validated_at": None,
            "evidence": {},
        }

    origin_finding = db.get(Finding, case.finding_id) if case.finding_id else None
    validated = False
    criterion = ""
    if origin_finding is None:
        reason = "La excepción histórica no conserva un hallazgo técnico que pueda volver a evaluarse."
        criterion = "ORIGIN_FINDING_REQUIRED"
    elif case.module == "intake":
        repeated = db.scalar(
            select(Finding).where(
                Finding.run_id == candidate.id,
                Finding.fingerprint == origin_finding.fingerprint,
            )
        )
        validated = repeated is None
        criterion = "INTAKE_RULE_NO_LONGER_FAILS"
        reason = (
            "La regla que originó la excepción pasó en la ejecución posterior."
            if validated
            else "La misma regla continúa generando incumplimientos en la ejecución posterior."
        )
    elif case.module == "recon":
        repeated = db.scalar(
            select(Finding).where(
                Finding.run_id == candidate.id,
                Finding.fingerprint == origin_finding.fingerprint,
            )
        )
        validated = candidate.decision == "CONFORME" or repeated is None
        criterion = "RECON_FINDING_NO_LONGER_PRESENT"
        reason = (
            "La conciliación quedó conforme o el hallazgo asociado desapareció."
            if validated
            else "El hallazgo de conciliación continúa presente en la ejecución posterior."
        )
    elif case.module == "sentinel":
        validated = candidate.decision == "HEALTHY"
        criterion = "SENTINEL_MONITOR_HEALTHY"
        reason = (
            "El monitor volvió a un estado saludable."
            if validated
            else "El monitor todavía presenta alertas en la ejecución posterior."
        )
    else:
        reason = "El módulo de la excepción no tiene una política de validación técnica compatible."
        criterion = "UNSUPPORTED_MODULE"

    validation_status = "VALIDATED" if validated else "FAILED"
    stored_evidence = case.validation_evidence or {}
    same_persisted_check = (
        stored_evidence.get("status") == validation_status
        and stored_evidence.get("validation_run_id") == candidate.id
        and stored_evidence.get("criterion") == criterion
    )
    checked_at = (
        stored_evidence.get("checked_at")
        if same_persisted_check and stored_evidence.get("checked_at")
        else iso(utcnow())
    )
    evidence = {
        "schema_version": 1,
        "status": validation_status,
        "module": case.module,
        "criterion": criterion,
        "reason": reason,
        "configuration_id": case.configuration_id,
        "origin_run_id": case.run_id,
        "origin_finding_id": case.finding_id,
        "origin_finding_code": origin_finding.code if origin_finding else None,
        "origin_finding_fingerprint": origin_finding.fingerprint if origin_finding else None,
        "validation_run_id": candidate.id,
        "validation_run_decision": candidate.decision,
        "checked_at": checked_at,
    }
    return {
        "status": evidence["status"],
        "eligible": True,
        "validated": validated,
        "can_resolve": case.state == "PENDING_VALIDATION" and validated,
        "reason": reason,
        "candidate_run_id": candidate.id,
        "validation_run_id": candidate.id if validated else None,
        "validated_at": (
            iso(case.validated_at)
            if validated and same_persisted_check and case.validated_at
            else evidence["checked_at"] if validated else None
        ),
        "evidence": evidence,
    }


def exception_dto(db: Session, c: ExceptionCase) -> dict:
    config = db.get(Configuration, c.configuration_id)
    origin_run = db.get(Run, c.run_id)
    validation_run = db.get(Run, c.validation_run_id) if c.validation_run_id else None
    return {"id": c.id, "display_id": c.display_id, "finding_id": c.finding_id,
            "run_id": c.run_id, "origin_run_id": c.run_id,
            "configuration_id": c.configuration_id,
            "configuration_name": config.name if config else None,
            "configuration_version": config.version if config else None,
            "title": c.title, "module": c.module, "severity": c.severity, "state": c.state,
            "owner": c.owner, "root_cause": c.root_cause, "resolution": c.resolution,
            "administrative_reason": c.administrative_reason,
            "validation_run_id": c.validation_run_id, "validated_at": iso(c.validated_at),
            "validation_evidence": c.validation_evidence or {},
            "technical_validation": assess_exception_validation(db, c),
            "origin_run": _validation_run_summary(origin_run),
            "validation_run": _validation_run_summary(validation_run),
            "version": c.version, "created_at": iso(c.created_at),
            "updated_at": iso(c.updated_at), "events": c.events}


def finding_dto(db: Session, f: Finding) -> dict:
    case = db.scalar(select(ExceptionCase).where(ExceptionCase.finding_id == f.id))
    return {"id": f.id, "run_id": f.run_id, "title": f.title, "code": f.code,
            "severity": f.severity, "details": f.details, "created_at": iso(f.created_at), "exception_id": case.id if case else None}


def audit_dto(a: AuditEvent) -> dict:
    return {"id": a.id, "event_type": a.event_type, "actor": a.actor, "subject_type": a.subject_type,
            "subject_id": a.subject_id, "message": a.message, "created_at": iso(a.created_at), "metadata": a.metadata_json,
            "actor_type": a.actor_type, "actor_id": a.actor_id, "actor_legacy": a.actor_legacy,
            "request_id": a.request_id, "run_id": a.run_id}


def create_version(db: Session, dataset: Dataset, source: Path, filename: str,
                   actor="Sistema", source_type="UPLOAD", created_at=None, parent_version_id=None,
                   column_overrides: dict | None = None, source_run_id: str | None = None,
                   reader_options: dict | None = None) -> DatasetVersion:
    read_result = read_dataset(source, filename, reader_options)
    frame = read_result.frame
    schema, profile, schema_hash = profile_frame(
        frame,
        overrides=column_overrides,
        native_types=read_result.native_schema,
    )
    profile["row_numbering"] = read_result.row_numbering
    ingestion_metadata = {
        "reader": {"key": read_result.source_format, "version": 1},
        "source_format": read_result.source_format,
        "format_label": read_result.format_label,
        "reader_options": {
            key: value
            for key, value in {
                "sheet_name": read_result.selected_sheet,
                "delimiter": read_result.detected_delimiter,
            }.items()
            if value is not None
        },
        "row_numbering": read_result.row_numbering,
        "native_schema": read_result.native_schema,
        **read_result.metadata,
    }
    version_id = uid()
    original_artifact = None
    if source_type in {"UPLOAD", "GENERATED_DEMO"}:
        kind = "ORIGINAL_UPLOAD" if source_type == "UPLOAD" else "GENERATED_DEMO"
        original_artifact = storage_provider.put_file(
            db,
            source,
            kind,
            dataset.organization_id,
            filename,
            media_type=read_result.media_type,
        )
    canonical = storage_provider.temporary_path(".parquet")
    try:
        frame.write_parquet(canonical)
        canonical_artifact = storage_provider.put_file(
            db,
            canonical,
            "INTAKE_ACCEPTED" if source_type == "INTAKE_OUTPUT" else "CANONICAL_PARQUET",
            dataset.organization_id,
            "accepted.parquet" if source_type == "INTAKE_OUTPUT" else "canonical.parquet",
            media_type="application/vnd.apache.parquet",
        )
    finally:
        canonical.unlink(missing_ok=True)
    if original_artifact:
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "ARTIFACT", canonical_artifact.id, "ARTIFACT", original_artifact.id)
    source_artifact = original_artifact or canonical_artifact
    version_number = (db.scalar(select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset.id)) or 0) + 1
    v = DatasetVersion(id=version_id, organization_id=dataset.organization_id, dataset_id=dataset.id,
                       version=version_number, filename=filename, source_type=source_type,
                       sha256=source_artifact.sha256, schema_hash=schema_hash,
                       size_bytes=source_artifact.size_bytes, row_count=frame.height, column_count=frame.width,
                       original_path=original_artifact.path if original_artifact else "",
                       canonical_path=canonical_artifact.path, schema_json=schema, profile=profile,
                       ingestion_metadata=ingestion_metadata,
                       original_artifact_id=original_artifact.id if original_artifact and source_type == "UPLOAD" else None,
                       canonical_artifact_id=canonical_artifact.id, source_run_id=source_run_id,
                       created_at=created_at or utcnow(), parent_version_id=parent_version_id)
    db.add(v)
    db.flush()
    if parent_version_id:
        link_artifact(db, dataset.organization_id, "INTAKE_ACCEPTED_FROM", "DATASET_VERSION", v.id, "DATASET_VERSION", parent_version_id)
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "DATASET_VERSION", v.id, "DATASET_VERSION", parent_version_id)
    if source_run_id:
        link_artifact(db, dataset.organization_id, "RUN_OUTPUT", "RUN", source_run_id, "DATASET_VERSION", v.id)
        link_artifact(db, dataset.organization_id, "RUN_OUTPUT", "RUN", source_run_id, "ARTIFACT", canonical_artifact.id)
    event = "DATASET_DERIVED" if source_type == "INTAKE_OUTPUT" else "DATASET_UPLOADED"
    audit(db, event, "dataset_version", v.id, f"{filename}: {frame.height:,} registros", actor,
          dataset.organization_id, {"dataset_id": dataset.id, "sha256": v.sha256, "row_count": v.row_count,
                                    "source_type": source_type, "source_run_id": source_run_id,
                                    "format": read_result.source_format,
                                    "reader_key": read_result.source_format,
                                    "reader_version": 1,
                                    "sheet_name": read_result.selected_sheet,
                                    "delimiter": read_result.detected_delimiter}, run_id=source_run_id)
    return v


def enqueue(
    db: Session,
    config: Configuration,
    source: DatasetVersion,
    target: DatasetVersion | None,
    actor: str,
    *,
    queue: JobQueue | None = None,
) -> Run:
    if source.dataset_id != config.dataset_id:
        raise ProcessingError("La versión de origen no pertenece al dataset del contrato/control.")
    if config.module == "recon" and (target is None or target.dataset_id != config.target_dataset_id):
        raise ProcessingError("La versión de destino no pertenece al dataset del control.")
    from .config_semantics import effective_config
    from .planner import ExecutionPlanner, WorkloadInput

    identity, legacy = resolve_actor(db, actor, config.organization_id)
    effective = effective_config(config.module, config.config)
    plan = ExecutionPlanner().plan(config.module,
        [WorkloadInput(v.row_count, v.column_count, v.size_bytes) for v in [source, target] if v], effective)
    plan["config_hash"] = configuration_hash(effective)
    run = Run(id=uid(), organization_id=config.organization_id, module=config.module, name=config.name,
              config_id=config.id, dataset_version_id=source.id, target_version_id=target.id if target else None,
              initiated_by=identity.display_name, initiated_by_type=identity.type, initiated_by_id=identity.id,
              initiated_by_legacy=legacy, execution_plan=plan)
    if not plan["allowed"]:
        run.status, run.progress_stage = "FAILED_PRECONDITION", "No se cumplen las condiciones de ejecución"
        run.error, run.finished_at = plan["rejection_code"], utcnow()
    db.add(run)
    db.flush()
    (queue or job_queue).submit(db, run, executable=plan["allowed"])
    for version in [source, target]:
        if version:
            link_artifact(db, run.organization_id, "RUN_INPUT", "RUN", run.id, "DATASET_VERSION", version.id)
    audit(db, "RUN_QUEUED" if plan["allowed"] else "RUN_FAILED", "run", run.id,
          f"Ejecución programada: {run.name}" if plan["allowed"] else f"{run.name}: {run.error}",
          actor, run.organization_id, {"reason_code": plan["reason_code"]})
    db.flush()
    return run


def ensure_input(v: DatasetVersion, db: Session | None = None) -> pl.DataFrame:
    try:
        original_path = None
        if v.original_path:
            original_path = storage_provider.materialize_reference(
                v.original_path,
                expected_sha256=v.sha256,
                expected_size=v.size_bytes,
            )
        if db is not None and v.canonical_artifact_id:
            canonical = db.get(Artifact, v.canonical_artifact_id)
            if not canonical or canonical.organization_id != v.organization_id:
                raise ArtifactIntegrityError("No existe el artefacto canónico de esta versión.")
            return pl.read_parquet(storage_provider.materialize(canonical))
        if not v.original_path:
            canonical_path = storage_provider.materialize_reference(
                v.canonical_path,
                expected_sha256=v.sha256,
                expected_size=v.size_bytes,
            )
            return pl.read_parquet(canonical_path)
        reader_options = (v.ingestion_metadata or {}).get("reader_options", {})
        assert original_path is not None
        return read_dataset(original_path, v.filename, reader_options).frame
    except (OSError, ArtifactIntegrityError) as exc:
        raise ProcessingError(str(exc)) from exc


def input_record_numbers(version: DatasetVersion) -> tuple[list[int], str]:
    """Use only after ensure_input has verified the original bytes and canonical artifact."""
    metadata = version.ingestion_metadata or {}
    source_format = metadata.get("source_format")
    row_numbering = metadata.get("row_numbering")
    if version.original_path and source_format in {None, "CSV", "TXT"}:
        original_path = storage_provider.materialize_reference(
            version.original_path,
            expected_sha256=version.sha256,
            expected_size=version.size_bytes,
        )
        delimiter = metadata.get("reader_options", {}).get("delimiter")
        numbers = (
            delimited_record_lines(original_path, delimiter)
            if delimiter
            else csv_record_lines(original_path)
        )
        if len(numbers) != version.row_count:
            raise ProcessingError(
                "No coincide el mapa de líneas del archivo delimitado con el Parquet canónico."
            )
        return numbers, "PHYSICAL_LINE"
    numbering = row_numbering if row_numbering in {"PHYSICAL_LINE", "RECORD_NUMBER"} else "RECORD_NUMBER"
    return list(range(1, version.row_count + 1)), numbering


def create_exception(db: Session, finding: Finding, actor: str) -> ExceptionCase:
    existing = db.scalar(select(ExceptionCase).where(ExceptionCase.finding_id == finding.id))
    if existing:
        return existing
    run = require_record(db, Run, finding.run_id)
    identity, legacy = resolve_actor(db, actor, finding.organization_id)
    case = ExceptionCase(id=uid(), organization_id=finding.organization_id,
                         display_id="EXC-" + uid()[:8].upper(), finding_id=finding.id, run_id=run.id,
                         configuration_id=run.config_id,
                         title=finding.title, module=run.module, severity=finding.severity,
                         events=[{"timestamp": iso(utcnow()), "actor": identity.display_name,
                                  "actor_type": identity.type, "actor_id": identity.id, "actor_legacy": legacy, "from_state": None,
                                  "to_state": "OPEN", "comment": "Excepción creada a partir del hallazgo"}])
    db.add(case)
    db.flush()
    audit(db, "EXCEPTION_CREATED", "exception", case.id, case.title, actor, finding.organization_id, run_id=run.id)
    return case


def record_exception_validation(
    db: Session,
    case: ExceptionCase,
    assessment: dict[str, Any],
    actor: Actor | str,
    comment: str | None = None,
) -> None:
    """Persist one validation check while retaining earlier checks in the event timeline."""
    if assessment["status"] not in {"VALIDATED", "FAILED"}:
        raise ValueError("La evaluación no contiene una ejecución técnica válida.")
    current_evidence = case.validation_evidence or {}
    incoming_evidence = assessment["evidence"]
    if (
        current_evidence.get("status") == assessment["status"]
        and current_evidence.get("validation_run_id") == incoming_evidence.get("validation_run_id")
        and current_evidence.get("criterion") == incoming_evidence.get("criterion")
    ):
        return
    identity, legacy = resolve_actor(db, actor, case.organization_id)
    now = utcnow()
    evidence = dict(incoming_evidence)
    event = {
        "timestamp": iso(now),
        "actor": identity.display_name,
        "actor_type": identity.type,
        "actor_id": identity.id,
        "actor_legacy": legacy,
        "from_state": case.state,
        "to_state": case.state,
        "event_type": "TECHNICAL_VALIDATION",
        "validation_status": assessment["status"],
        "validation_run_id": evidence.get("validation_run_id"),
        "comment": comment or assessment["reason"],
        "evidence": evidence,
    }
    case.validation_evidence = evidence
    case.validation_run_id = evidence["validation_run_id"] if assessment["validated"] else None
    case.validated_at = now if assessment["validated"] else None
    case.updated_at = now
    case.version += 1
    case.events = [*(case.events or []), event]
    audit(
        db,
        "EXCEPTION_VALIDATION_CHECKED",
        "exception",
        case.id,
        f"{case.display_id}: {assessment['status']}",
        identity,
        case.organization_id,
        {
            "status": assessment["status"],
            "configuration_id": case.configuration_id,
            "origin_run_id": case.run_id,
            "validation_run_id": evidence.get("validation_run_id"),
            "criterion": evidence.get("criterion"),
        },
        run_id=evidence.get("validation_run_id"),
    )


def refresh_pending_exception_validations(db: Session, run: Run, actor: Actor | str) -> None:
    """Attach current evidence after a run, without administratively resolving cases.

    This is the seam for a future auto-resolution policy. Today it only updates
    technical validation and deliberately leaves PENDING_VALIDATION unchanged.
    """
    cases = db.scalars(
        select(ExceptionCase).where(
            ExceptionCase.organization_id == run.organization_id,
            ExceptionCase.configuration_id == run.config_id,
            ExceptionCase.state == "PENDING_VALIDATION",
        )
    ).all()
    for case in cases:
        assessment = assess_exception_validation(db, case, run.id)
        if assessment["status"] in {"VALIDATED", "FAILED"}:
            record_exception_validation(db, case, assessment, actor)


def execute_run(db: Session, run: Run, lease_owner: str | None = None, observed_at=None):
    """Shared worker/seed service. Configuration and dataset references are immutable."""
    from .config_semantics import effective_config
    from .planner import ExecutionPlanner, WorkloadInput

    if run.status in {"SUCCESS", "FAILED_PRECONDITION", "CANCELLED"}:
        return
    config = require_record(db, Configuration, run.config_id)
    effective = effective_config(config.module, config.config)
    source = require_record(db, DatasetVersion, run.dataset_version_id)
    target = require_record(db, DatasetVersion, run.target_version_id) if run.target_version_id else None
    current_plan = ExecutionPlanner().plan(config.module,
        [WorkloadInput(v.row_count, v.column_count, v.size_bytes) for v in [source, target] if v], effective)
    if not current_plan["allowed"]:
        raise ProcessingError(current_plan["rejection_code"])
    if run.cancel_requested:
        run.status, run.progress_stage, run.finished_at = "CANCELLED", "Cancelado", utcnow()
        return
    run.status, run.progress_stage = "RUNNING", "Procesando datos"
    run.started_at = run.started_at or observed_at or utcnow()
    db.commit()
    frame = ensure_input(source, db)
    source_numbers, source_numbering = input_record_numbers(source)
    accepted = None
    if run.module == "intake":
        rows, metrics, accepted = intake(frame, effective, observed_at=run.started_at, input_row_numbers=source_numbers)
        decision = metrics["decision"]
    elif run.module == "recon":
        target_version = require_record(db, DatasetVersion, run.target_version_id)
        target_frame = ensure_input(target_version, db)
        target_numbers, target_numbering = input_record_numbers(target_version)
        rows, metrics = reconcile(frame, target_frame, effective, source_row_numbers=source_numbers, target_row_numbers=target_numbers)
        metrics["target_row_numbering"] = target_numbering
        decision = "CONFORME" if metrics["matched"] == metrics["total_rows"] else "WITH_FINDINGS"
    else:
        baseline = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == source.dataset_id,
                             DatasetVersion.version < source.version).order_by(DatasetVersion.version.desc()))
        history = [{"metric_key": item.metric_key, "numeric_value": item.numeric_value, "method": item.method,
                    "metric_definition_version": item.metric_definition_version, "observed_at": iso(item.observed_at),
                    "run_id": item.run_id, "status": "SUCCESS"} for item in db.scalars(
                        select(SentinelMetricHistory).join(Run, SentinelMetricHistory.run_id == Run.id)
                        .join(Configuration, SentinelMetricHistory.monitor_id == Configuration.id)
                        .where(SentinelMetricHistory.organization_id == run.organization_id,
                               Configuration.dataset_id == config.dataset_id, Configuration.name == config.name,
                               Run.status == "SUCCESS", Run.id != run.id)
                        .order_by(SentinelMetricHistory.observed_at))]
        rows, metrics = sentinel(source.profile, source.schema_json, effective, source.created_at,
                                 baseline.profile if baseline else None, observed_at=run.started_at,
                                 frame=frame, previous_schema=baseline.schema_json if baseline else None, history=history)
        decision = "HEALTHY" if metrics["failed_checks"] == 0 else "ALERT"
    metrics["source_row_numbering"] = source_numbering
    db.refresh(run)
    if run.cancel_requested:
        run.status, run.progress_stage, run.finished_at = "CANCELLED", "Cancelado", utcnow()
        return
    _verify_lease(db, run, lease_owner)
    # Provider-generated artifact identities keep retries immutable without
    # exposing a local directory layout to the application service.
    results = storage_provider.temporary_path(".parquet")
    try:
        pl.DataFrame({"classification": [r.get("classification", "") for r in rows],
                      "payload": [json.dumps(r, ensure_ascii=False) for r in rows]},
                     schema={"classification": pl.String, "payload": pl.String}).write_parquet(results)
        result_artifact = storage_provider.put_file(
            db,
            results,
            {"intake": "INTAKE_ERRORS", "recon": "RECON_RESULTS", "sentinel": "SENTINEL_PROFILE"}[run.module],
            run.organization_id,
            "results.parquet",
            media_type="application/vnd.apache.parquet",
        )
    finally:
        results.unlink(missing_ok=True)
    link_artifact(db, run.organization_id, "RUN_OUTPUT", "RUN", run.id, "ARTIFACT", result_artifact.id)
    if accepted is not None and not run.output_version_id:
        output_dataset = db.scalar(select(Dataset).where(Dataset.organization_id == run.organization_id,
                                  Dataset.name == require_record(db, Dataset, source.dataset_id).name + " · Aprobados"))
        if not output_dataset:
            original_dataset = require_record(db, Dataset, source.dataset_id)
            output_dataset = Dataset(name=original_dataset.name + " · Aprobados", organization_id=run.organization_id,
                                     description="Filas conformes derivadas de Data Intake Gateway", domain=original_dataset.domain,
                                     owner=original_dataset.owner, criticality=original_dataset.criticality)
            db.add(output_dataset)
            db.flush()
        accepted_path = storage_provider.temporary_path(".parquet")
        try:
            accepted.write_parquet(accepted_path)
            output = create_version(db, output_dataset, accepted_path, "accepted.parquet",
                                    Actor("WORKER", lease_owner or "trackvance:worker", "Worker"), "INTAKE_OUTPUT",
                                    parent_version_id=source.id, source_run_id=run.id)
        finally:
            accepted_path.unlink(missing_ok=True)
        run.output_version_id = output.id
    if run.module == "intake":
        for rule in metrics["rules"]:
            if rule["failed_count"]:
                code = rule["code"] + ":" + rule["column"]
                fingerprint = hashlib.sha256(code.encode()).hexdigest()
                if not db.scalar(select(Finding).where(Finding.run_id == run.id, Finding.fingerprint == fingerprint)):
                    db.add(Finding(organization_id=run.organization_id, run_id=run.id, fingerprint=fingerprint, code=code, title=f"{rule['column']}: {rule['failed_count']} incumplimientos de {rule['code']}", severity="MEDIUM" if rule.get("severity") == "WARNING" else "HIGH", details=rule))
    else:
        issues = [r for r in rows if r.get("classification") not in ("MATCH", "PASS")]
        grouped = Counter(r.get("classification") for r in issues) if run.module == "recon" else None
        findings: list[dict[str, Any]] = [{"code": code, "title": f"{count} registros · {code}", "details": {"count": count, "classification": code}, "severity": "HIGH" if code == "VALUE_MISMATCH" else "MEDIUM"} for code, count in (grouped or {}).items()]
        if run.module == "sentinel":
            findings = [{"code": r["code"], "title": r["name"], "details": r, "severity": "HIGH"} for r in issues]
        for f in findings:
            fingerprint = hashlib.sha256(f["code"].encode()).hexdigest()
            if not db.scalar(select(Finding).where(Finding.run_id == run.id, Finding.fingerprint == fingerprint)):
                db.add(Finding(organization_id=run.organization_id, run_id=run.id, fingerprint=fingerprint, **f))
    run.metrics, run.decision, run.result_path = metrics, decision, result_artifact.path
    run.status, run.progress_percent, run.progress_stage, run.finished_at = "SUCCESS", 100, "Completado", observed_at or utcnow()
    if run.module == "sentinel":
        _record_metric_history(db, run, metrics)
    db.flush()
    refresh_pending_exception_validations(
        db,
        run,
        Actor("WORKER", lease_owner or "trackvance:worker", "Worker"),
    )
    result_artifacts = [artifact_dto(result_artifact)]
    if run.output_version_id:
        output = require_record(db, DatasetVersion, run.output_version_id)
        if output and output.canonical_artifact_id:
            result_artifacts.append(artifact_dto(require_record(db, Artifact, output.canonical_artifact_id)))
    manifest = {"schema_version": SCHEMA_VERSION, "run_id": run.id, "module": run.module,
                "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
                "initiated_by": run_actor(run).as_dict(), "initiated_by_legacy": run.initiated_by_legacy,
                "engine_version": __version__, "processing": run.execution_plan,
                "inputs": [{"dataset_version_id": v.id, "artifact_sha256": v.sha256, "schema_hash": v.schema_hash,
                            "dataset_id": v.dataset_id, "dataset_name": require_record(db, Dataset, v.dataset_id).name,
                            "version": v.version, "source_type": v.source_type, "original_artifact_id": v.original_artifact_id,
                            "ingestion_metadata": v.ingestion_metadata or {},
                            "row_numbering": source_numbering if v.id == source.id else metrics.get("target_row_numbering"),
                            "canonical_artifact_id": v.canonical_artifact_id,
                            "canonical_sha256": require_record(db, Artifact, v.canonical_artifact_id).sha256 if v.canonical_artifact_id else None}
                            for v in [source, target] if v],
                "configuration": {"id": config.id, "name": config.name, "version": config.version, "config": effective,
                "config_hash": configuration_hash(effective), "stored_config_hash": configuration_hash(config.config)},
                "metrics": metrics, "output_version_id": run.output_version_id,
                "result_artifacts": result_artifacts}
    temp_evidence = storage_provider.temporary_path(".json")
    try:
        temp_evidence.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        _verify_lease(db, run, lease_owner)
        evidence = storage_provider.put_file(
            db,
            temp_evidence,
            "RUN_MANIFEST",
            run.organization_id,
            "evidence.json",
            media_type="application/json",
        )
    finally:
        temp_evidence.unlink(missing_ok=True)
    run.evidence_path = evidence.path
    link_artifact(db, run.organization_id, "RUN_OUTPUT", "RUN", run.id, "ARTIFACT", evidence.id)
    audit(db, "RUN_COMPLETED", "run", run.id, f"{run.name}: {decision}",
          Actor("WORKER", lease_owner or "trackvance:worker", "Worker"), run.organization_id,
          {"module": run.module, "status": run.status, "decision": decision})


def result_rows(
    run: Run,
    classification: str | None = None,
    offset: int = 0,
    limit: int = 50,
    *,
    db: Session | None = None,
) -> dict:
    if not run.result_path:
        return {"items": [], "total": 0}
    if db is not None:
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.organization_id == run.organization_id,
                Artifact.path == run.result_path,
            )
        )
        if not artifact:
            raise ArtifactIntegrityError("No existe un artefacto registrado para el resultado.")
        result_path = storage_provider.materialize(artifact)
    else:
        # Compatibility for internal callers operating on historical local rows.
        result_path = storage_provider.materialize_reference(run.result_path)
    frame = pl.scan_parquet(result_path)
    if classification:
        frame = frame.filter(pl.col("classification") == classification)
    total = frame.select(pl.len()).collect().item()
    items = [json.loads(payload) for payload in frame.slice(offset, limit).collect()["payload"].to_list()]
    return {"items": items, "total": total}


def _verify_lease(db: Session, run: Run, lease_owner: str | None) -> None:
    if lease_owner is None:
        return
    job = db.scalar(select(Job).where(Job.run_id == run.id).execution_options(populate_existing=True))
    if (not job or job.lease_owner != lease_owner or job.status != "RUNNING"
            or not job.lease_until or job.lease_until.replace(tzinfo=UTC) <= utcnow()):
        raise ProcessingError("WORKER_LEASE_LOST: El worker ya no posee esta ejecución.")


def _record_metric_history(db: Session, run: Run, metrics: dict) -> None:
    """Exact metric definitions are independently versioned from configuration versions."""
    for record in metrics.get("metric_records", []):
        dimensions = record.get("dimensions", {})
        dimension_hash = configuration_hash(dimensions)
        previous = db.scalar(select(SentinelMetricHistory).where(
            SentinelMetricHistory.run_id == run.id,
            SentinelMetricHistory.metric_key == record["metric_key"],
            SentinelMetricHistory.dimension_hash == dimension_hash))
        if previous:
            continue
        db.add(SentinelMetricHistory(organization_id=run.organization_id,
            monitor_id=run.config_id, run_id=run.id, metric_key=record["metric_key"],
            dimensions=dimensions, dimension_hash=dimension_hash,
            numeric_value=record.get("numeric_value"), method=record["method"],
            metric_definition_version=record["metric_definition_version"],
            observed_at=run.finished_at or utcnow()))


def register_export(db: Session, run: Run, path: Path) -> Artifact:
    """Register a new export without altering a run or its original manifest."""
    artifact = storage_provider.put_file(db, path, "EXPORT_XLSX", run.organization_id, path.name)
    link_artifact(db, run.organization_id, "EXPORT_OF", "ARTIFACT", artifact.id, "RUN", run.id)
    return artifact


def backfill_artifacts(db: Session) -> dict[str, int]:
    """Register legacy bytes and additive links, never recompute historical data or evidence.

    Original SHA-256 values are verified before registration. Missing/corrupt files are
    left unregistered and reported in counts so existing history remains accessible.
    New runs still reject those inputs through ensure_input.
    """
    counts = {"versions": 0, "runs": 0, "unavailable": 0}
    for version in db.scalars(select(DatasetVersion)):
        if version.canonical_artifact_id:
            continue
        try:
            with db.begin_nested():
                original = None
                if version.original_path:
                    original = artifact_store.register_existing(db, Path(version.original_path),
                        "INTAKE_ACCEPTED_LEGACY" if version.source_type == "INTAKE_OUTPUT" else
                        "GENERATED_DEMO" if version.source_type == "GENERATED_DEMO" else "ORIGINAL_UPLOAD",
                        version.organization_id, version.filename, expected_sha256=version.sha256)
                canonical = artifact_store.register_existing(db, Path(version.canonical_path),
                    "INTAKE_ACCEPTED" if version.source_type == "INTAKE_OUTPUT" else "CANONICAL_PARQUET",
                    version.organization_id, "accepted.parquet" if version.source_type == "INTAKE_OUTPUT" else "canonical.parquet")
                version.canonical_artifact_id = canonical.id
                if original:
                    if version.source_type not in {"INTAKE_OUTPUT", "GENERATED_DEMO"}:
                        version.original_artifact_id = original.id
                    link_artifact(db, version.organization_id, "DERIVED_FROM", "ARTIFACT", canonical.id, "ARTIFACT", original.id)
                if version.parent_version_id:
                    link_artifact(db, version.organization_id, "INTAKE_ACCEPTED_FROM", "DATASET_VERSION", version.id,
                                  "DATASET_VERSION", version.parent_version_id)
                    link_artifact(db, version.organization_id, "DERIVED_FROM", "DATASET_VERSION", version.id,
                                  "DATASET_VERSION", version.parent_version_id)
                counts["versions"] += 1
        except (OSError, ArtifactIntegrityError):
            counts["unavailable"] += 1
    for run in db.scalars(select(Run)):
        for identity in [run.dataset_version_id, run.target_version_id]:
            if identity:
                link_artifact(db, run.organization_id, "RUN_INPUT", "RUN", run.id, "DATASET_VERSION", identity)
        if run.output_version_id:
            output = db.get(DatasetVersion, run.output_version_id)
            if output and output.organization_id == run.organization_id:
                if output.source_run_id is None:
                    output.source_run_id = run.id
                link_artifact(db, run.organization_id, "RUN_OUTPUT", "RUN", run.id, "DATASET_VERSION", output.id)
                if output.canonical_artifact_id:
                    link_artifact(db, run.organization_id, "RUN_OUTPUT", "RUN", run.id, "ARTIFACT", output.canonical_artifact_id)
        for path, kind in [(run.result_path, {"intake": "INTAKE_ERRORS", "recon": "RECON_RESULTS", "sentinel": "SENTINEL_PROFILE"}.get(run.module, "RUN_RESULTS")),
                           (run.evidence_path, "RUN_MANIFEST")]:
            if not path:
                continue
            try:
                with db.begin_nested():
                    artifact = artifact_store.register_existing(db, Path(path), kind, run.organization_id)
                    link_artifact(db, run.organization_id, "RUN_OUTPUT", "RUN", run.id, "ARTIFACT", artifact.id)
            except (OSError, ArtifactIntegrityError):
                counts["unavailable"] += 1
        counts["runs"] += 1
    return counts
