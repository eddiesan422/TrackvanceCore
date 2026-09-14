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
    file_hash,
    link_artifact,
)
from .audit_context import Actor, actor_context, legacy_actor, request_id_context, sanitize_metadata
from .config import ORG_ID, STORAGE_DIR
from .db import iso, require_record, utcnow
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
    read_csv,
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
            "profile": v.profile, "parent_version_id": v.parent_version_id,
            "original_artifact_id": v.original_artifact_id, "canonical_artifact_id": v.canonical_artifact_id,
            "source_run_id": v.source_run_id, "artifacts": artifacts, "lineage": lineage,
            "is_derived": v.source_type == "INTAKE_OUTPUT", "has_original_upload": bool(v.original_artifact_id)}


def dataset_dto(db: Session, d: Dataset) -> dict:
    versions = db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == d.id).order_by(DatasetVersion.version.desc())).all()
    latest = versions[0] if versions else None
    return {"id": d.id, "name": d.name, "description": d.description, "domain": d.domain,
            "owner": d.owner, "criticality": d.criticality, "status": d.status,
            "created_at": iso(d.created_at), "version_count": len(versions),
            "row_count": latest.row_count if latest else 0, "column_count": latest.column_count if latest else 0,
            "latest_version_id": latest.id if latest else None, "updated_at": iso(latest.created_at if latest else d.created_at)}


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


def exception_dto(c: ExceptionCase) -> dict:
    return {"id": c.id, "display_id": c.display_id, "finding_id": c.finding_id, "run_id": c.run_id,
            "title": c.title, "module": c.module, "severity": c.severity, "state": c.state,
            "owner": c.owner, "root_cause": c.root_cause, "resolution": c.resolution,
            "version": c.version, "created_at": iso(c.created_at), "updated_at": iso(c.updated_at), "events": c.events}


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
                   column_overrides: dict | None = None, source_run_id: str | None = None) -> DatasetVersion:
    frame = pl.read_parquet(source) if source.suffix.lower() == ".parquet" else read_csv(source)
    schema, profile, schema_hash = profile_frame(frame, overrides=column_overrides)
    profile["row_numbering"] = "RECORD_NUMBER" if source.suffix.lower() == ".parquet" else "PHYSICAL_LINE"
    version_id = uid()
    folder = STORAGE_DIR / "artifacts" / version_id
    folder.mkdir(parents=True, exist_ok=False)
    original_artifact = None
    if source_type in {"UPLOAD", "GENERATED_DEMO"}:
        kind = "ORIGINAL_UPLOAD" if source_type == "UPLOAD" else "GENERATED_DEMO"
        original_artifact = artifact_store.put_file(db, source, kind, dataset.organization_id, filename)
    canonical = folder / "canonical.parquet"
    frame.write_parquet(canonical)
    canonical_artifact = artifact_store.register_existing(db, canonical, "INTAKE_ACCEPTED" if source_type == "INTAKE_OUTPUT" else "CANONICAL_PARQUET", dataset.organization_id,
                                                         "accepted.parquet" if source_type == "INTAKE_OUTPUT" else "canonical.parquet")
    if original_artifact:
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "ARTIFACT", canonical_artifact.id, "ARTIFACT", original_artifact.id)
    source_artifact = original_artifact or canonical_artifact
    version_number = (db.scalar(select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset.id)) or 0) + 1
    v = DatasetVersion(id=version_id, organization_id=dataset.organization_id, dataset_id=dataset.id,
                       version=version_number, filename=filename, source_type=source_type,
                       sha256=source_artifact.sha256, schema_hash=schema_hash,
                       size_bytes=source_artifact.size_bytes, row_count=frame.height, column_count=frame.width,
                       original_path=original_artifact.path if original_artifact else "", canonical_path=str(canonical), schema_json=schema, profile=profile,
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
                                    "source_type": source_type, "source_run_id": source_run_id}, run_id=source_run_id)
    return v


def enqueue(db: Session, config: Configuration, source: DatasetVersion, target: DatasetVersion | None, actor: str) -> Run:
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
    db.add(Job(organization_id=run.organization_id, run_id=run.id, status="QUEUED" if plan["allowed"] else "FAILED"))
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
        if v.original_path:
            path = artifact_store.checked_path(v.original_path)
            if file_hash(path) != v.sha256:
                raise ArtifactIntegrityError("La integridad del archivo de entrada no coincide con su SHA-256.")
        if db is not None and v.canonical_artifact_id:
            canonical = db.get(Artifact, v.canonical_artifact_id)
            if not canonical or canonical.organization_id != v.organization_id:
                raise ArtifactIntegrityError("No existe el artefacto canónico de esta versión.")
            return pl.read_parquet(artifact_store.verify(canonical))
        if not v.original_path:
            path = artifact_store.checked_path(v.canonical_path)
            if file_hash(path) != v.sha256:
                raise ArtifactIntegrityError("La integridad del artefacto derivado no coincide con su SHA-256.")
            return pl.read_parquet(path)
        return read_csv(Path(v.original_path))
    except (OSError, ArtifactIntegrityError) as exc:
        raise ProcessingError(str(exc)) from exc


def input_record_numbers(version: DatasetVersion) -> tuple[list[int], str]:
    """Use only after ensure_input has verified the original bytes and canonical artifact."""
    if version.original_path:
        numbers = csv_record_lines(Path(version.original_path))
        if len(numbers) != version.row_count:
            raise ProcessingError("No coincide el mapa de líneas del CSV con el Parquet canónico.")
        return numbers, "PHYSICAL_LINE"
    return list(range(1, version.row_count + 1)), "RECORD_NUMBER"


def create_exception(db: Session, finding: Finding, actor: str) -> ExceptionCase:
    existing = db.scalar(select(ExceptionCase).where(ExceptionCase.finding_id == finding.id))
    if existing:
        return existing
    run = require_record(db, Run, finding.run_id)
    identity, legacy = resolve_actor(db, actor, finding.organization_id)
    case = ExceptionCase(id=uid(), organization_id=finding.organization_id,
                         display_id="EXC-" + uid()[:8].upper(), finding_id=finding.id, run_id=run.id,
                         title=finding.title, module=run.module, severity=finding.severity,
                         events=[{"timestamp": iso(utcnow()), "actor": identity.display_name,
                                  "actor_type": identity.type, "actor_id": identity.id, "actor_legacy": legacy, "from_state": None,
                                  "to_state": "OPEN", "comment": "Excepción creada a partir del hallazgo"}])
    db.add(case)
    db.flush()
    audit(db, "EXCEPTION_CREATED", "exception", case.id, case.title, actor, finding.organization_id, run_id=run.id)
    return case


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
    # Attempt-specific paths leave any interrupted attempt unreferenced. No historical bytes are replaced.
    folder = STORAGE_DIR / "runs" / run.id / ("attempt-" + uid())
    folder.mkdir(parents=True, exist_ok=False)
    results = folder / "results.parquet"
    pl.DataFrame({"classification": [r.get("classification", "") for r in rows],
                  "payload": [json.dumps(r, ensure_ascii=False) for r in rows]},
                 schema={"classification": pl.String, "payload": pl.String}).write_parquet(folder / "results.tmp.parquet")
    artifact_store.promote(folder / "results.tmp.parquet", results)
    result_artifact = artifact_store.register_existing(db, results,
        {"intake": "INTAKE_ERRORS", "recon": "RECON_RESULTS", "sentinel": "SENTINEL_PROFILE"}[run.module], run.organization_id)
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
        accepted_path = folder / "accepted.parquet"
        accepted.write_parquet(accepted_path)
        output = create_version(db, output_dataset, accepted_path, "accepted.parquet",
                                Actor("WORKER", lease_owner or "trackvance:worker", "Worker"), "INTAKE_OUTPUT",
                                parent_version_id=source.id, source_run_id=run.id)
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
    run.metrics, run.decision, run.result_path = metrics, decision, str(results)
    run.status, run.progress_percent, run.progress_stage, run.finished_at = "SUCCESS", 100, "Completado", observed_at or utcnow()
    if run.module == "sentinel":
        _record_metric_history(db, run, metrics)
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
                            "row_numbering": source_numbering if v.id == source.id else metrics.get("target_row_numbering"),
                            "canonical_artifact_id": v.canonical_artifact_id,
                            "canonical_sha256": require_record(db, Artifact, v.canonical_artifact_id).sha256 if v.canonical_artifact_id else None}
                            for v in [source, target] if v],
                "configuration": {"id": config.id, "name": config.name, "version": config.version, "config": effective,
                "config_hash": configuration_hash(effective), "stored_config_hash": configuration_hash(config.config)},
                "metrics": metrics, "output_version_id": run.output_version_id,
                "result_artifacts": result_artifacts}
    temp_evidence = folder / "evidence.tmp.json"
    temp_evidence.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _verify_lease(db, run, lease_owner)
    artifact_store.promote(temp_evidence, folder / "evidence.json")
    run.evidence_path = str(folder / "evidence.json")
    evidence = artifact_store.register_existing(db, Path(run.evidence_path), "RUN_MANIFEST", run.organization_id)
    link_artifact(db, run.organization_id, "RUN_OUTPUT", "RUN", run.id, "ARTIFACT", evidence.id)
    audit(db, "RUN_COMPLETED", "run", run.id, f"{run.name}: {decision}",
          Actor("WORKER", lease_owner or "trackvance:worker", "Worker"), run.organization_id,
          {"module": run.module, "status": run.status, "decision": decision})


def result_rows(run: Run, classification: str | None = None, offset=0, limit=50) -> dict:
    if not run.result_path:
        return {"items": [], "total": 0}
    frame = pl.scan_parquet(run.result_path)
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
    artifact = artifact_store.put_file(db, path, "EXPORT_XLSX", run.organization_id, path.name)
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
