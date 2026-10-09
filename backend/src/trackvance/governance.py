"""One catalog/Reports evaluator; metadata selection never scans canonical files."""

import copy
import json
from collections import Counter
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, aliased

from .db import iso
from .governance_models import (
    DataDomain,
    DatasetBlock,
    DatasetSecurityDependency,
    GovernanceHistory,
    GovernancePerson,
    MacroDomain,
    StrictApproval,
)
from .manifests import configuration_hash
from .models import Artifact, ArtifactLink, Configuration, Dataset, DatasetVersion, Run, User
from .operations_common import OperationError
from .permissions import effective_permissions

MAX_ANCESTORS = 128
STRICT_APPROVAL_CRITERION_VERSION = 2
STRICT_APPROVAL_CRITERION = "ALL_ROWS_EFFECTIVELY_VALIDATED_V2"


def contract_identity(db: Session, config: Configuration) -> str:
    visited: set[str] = set()
    current = config
    while current.previous_version_id:
        if current.id in visited or len(visited) >= MAX_ANCESTORS:
            raise OperationError(422, "CONTRACT_LINEAGE_INVALID", "La historia del contrato contiene un ciclo o excede el límite.")
        visited.add(current.id)
        previous = db.get(Configuration, current.previous_version_id)
        if previous is None or previous.organization_id != config.organization_id or previous.dataset_id != config.dataset_id:
            raise OperationError(422, "CONTRACT_LINEAGE_INVALID", "La historia del contrato está incompleta.")
        current = previous
    return current.id


def validate_classification(db: Session, organization_id: str, macro_domain_id: str | None,
                            domain_id: str | None, *, require_active: bool = True) -> None:
    macro = db.get(MacroDomain, macro_domain_id) if macro_domain_id else None
    domain = db.get(DataDomain, domain_id) if domain_id else None
    if macro_domain_id and (not macro or macro.organization_id != organization_id or require_active and not macro.active):
        raise OperationError(422, "MACRODOMAIN_INVALID", "El macrodominio no está disponible para esta organización.")
    if domain_id and (not domain or domain.organization_id != organization_id or domain.macro_domain_id != macro_domain_id
                      or require_active and not domain.active):
        raise OperationError(422, "DOMAIN_INCOMPATIBLE", "Selecciona un dominio activo del macrodominio elegido o deja la clasificación incompleta.")


def verified_intake_parent(db: Session, dataset: Dataset) -> str | None:
    if dataset.intake_input_dataset_id:
        return dataset.intake_input_dataset_id
    parent = aliased(DatasetVersion)
    total = db.scalar(select(func.count()).select_from(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)) or 0
    if not total:
        return None
    count, parents, identity = db.execute(select(func.count(), func.count(func.distinct(parent.dataset_id)),
        func.min(parent.dataset_id)).select_from(DatasetVersion).join(parent, DatasetVersion.parent_version_id == parent.id)
        .join(Run, DatasetVersion.source_run_id == Run.id).join(Configuration, Run.config_id == Configuration.id)
        .where(DatasetVersion.dataset_id == dataset.id, DatasetVersion.source_type == "INTAKE_OUTPUT",
            DatasetVersion.organization_id == dataset.organization_id, parent.organization_id == dataset.organization_id,
            Run.organization_id == dataset.organization_id, Run.module == "intake", Run.output_version_id == DatasetVersion.id,
            Run.dataset_version_id == parent.id, Configuration.organization_id == dataset.organization_id,
            Configuration.dataset_id == parent.dataset_id)).one()
    return identity if count == total and parents == 1 else None


def governing_dataset(db: Session, dataset: Dataset) -> Dataset:
    current = dataset
    visited: set[str] = set()
    parent_id = verified_intake_parent(db, current)
    while parent_id:
        if current.id in visited or len(visited) >= MAX_ANCESTORS:
            raise OperationError(422, "GOVERNANCE_LINEAGE_INVALID", "La herencia de gobierno no es verificable.")
        visited.add(current.id)
        parent = db.get(Dataset, parent_id)
        if parent is None or parent.organization_id != dataset.organization_id:
            raise OperationError(422, "GOVERNANCE_LINEAGE_INVALID", "La entrada de gobierno no está disponible.")
        current = parent
        parent_id = verified_intake_parent(db, current)
    return current


def governance_snapshot(db: Session, dataset: Dataset) -> dict:
    current = governing_dataset(db, dataset)
    macro = db.get(MacroDomain, current.macro_domain_id) if current.macro_domain_id else None
    domain = db.get(DataDomain, current.domain_id) if current.domain_id else None
    complete = bool(macro and domain and macro.active and domain.active and domain.macro_domain_id == macro.id
                    and macro.organization_id == dataset.organization_id and domain.organization_id == dataset.organization_id)
    identities: dict[str, Any] = {}
    for field in ("business_owner_id", "steward_id", "technical_custodian_id"):
        role = field.removesuffix("_id")
        person_id = getattr(current, f"{role}_person_id")
        person = db.get(GovernancePerson, person_id) if person_id else None
        user = db.get(User, getattr(current, field)) if getattr(current, field) else None
        if person and person.organization_id == dataset.organization_id:
            identities[role] = {"id": person.id, "name": person.name, "active": person.active,
                                "identity_kind": "PERSON", "user_id": person.user_id, "version": person.version}
        else:
            identities[role] = {"id": user.id, "name": user.name, "active": user.active and not user.deleted,
                                "identity_kind": "LEGACY_USER", "user_id": user.id} if user else None
        identities[f"{role}_person_id"] = person_id
    return {"schema_version": 2, "dataset_id": dataset.id, "source_dataset_id": current.id, "inherited": current.id != dataset.id,
            "version": current.governance_version, "macro_domain_id": current.macro_domain_id, "domain_id": current.domain_id,
            "macro_domain": {"id": macro.id, "name": macro.name, "active": macro.active, "version": macro.version} if macro else None,
            "domain": {"id": domain.id, "name": domain.name, "active": domain.active, "version": domain.version} if domain else None,
            "classification_complete": complete, "description": current.description, "legacy_area": current.domain,
            "legacy_owner": current.owner, "criticality": current.criticality,
            "information_classification": current.information_classification, **identities,
            "business_owner_id": current.business_owner_id, "steward_id": current.steward_id,
            "technical_custodian_id": current.technical_custodian_id}


def dataset_ancestors(db: Session, dataset_id: str, organization_id: str) -> list[Dataset]:
    """Iterative, bounded graph walk; cycles fail closed and duplicate paths collapse."""
    result: dict[str, Dataset] = {}
    stack: list[tuple[str, frozenset[str]]] = [(dataset_id, frozenset())]
    while stack:
        identity, path = stack.pop()
        if identity in path:
            raise OperationError(403, "SECURITY_LINEAGE_INVALID", "La procedencia de acceso contiene un ciclo.")
        if identity in result:
            continue
        if len(result) >= MAX_ANCESTORS:
            raise OperationError(403, "SECURITY_LINEAGE_LIMIT", "La procedencia excede el límite de 128 activos.")
        dataset = db.get(Dataset, identity, populate_existing=True)
        if dataset is None or dataset.organization_id != organization_id:
            raise OperationError(403, "SOURCE_ACCESS_DENIED", "No tienes acceso a todas las fuentes del activo.")
        result[identity] = dataset
        parents = list(db.scalars(select(DatasetSecurityDependency.source_dataset_id).where(
            DatasetSecurityDependency.dataset_id == identity, DatasetSecurityDependency.organization_id == organization_id,
            DatasetSecurityDependency.active.is_(True))))
        # Intake descendants inherit the dependency graph of their input, including
        # historical descendants whose immutable parent-version relationship exists.
        intake_parent = verified_intake_parent(db, dataset)
        if intake_parent:
            parents.append(intake_parent)
        for parent in parents:
            stack.append((parent, path | {identity}))
    return list(result.values())


def authorize_dataset(db: Session, user: User, dataset_id: str, purpose: str = "CONTENT") -> None:
    ancestors = dataset_ancestors(db, dataset_id, user.organization_id)
    identities = [dataset.id for dataset in ancestors]
    if "datasets:read" not in effective_permissions(db, user):
        report_ancestor = db.scalar(select(DatasetVersion.id).where(DatasetVersion.dataset_id.in_(identities),
            DatasetVersion.source_type.in_(["REPORTS", "REPORT_OUTPUT", "REPORT"])).limit(1))
        # Historical module-specific read grants retain their previous semantics
        # for assets unrelated to Reportes. A new report-derived population never
        # obtains a content shortcut through module results/exception endpoints.
        if purpose.upper() != "MODULE" or report_ancestor:
            raise OperationError(403, "SOURCE_ACCESS_DENIED", "Tu rol no permite consultar las fuentes del activo.")
    scopes = [] if purpose.upper() == "METADATA" else ["CONTENT", "REPORT"] if purpose.upper() in {"REPORT", "REPORTS"} else ["CONTENT"]
    block = db.scalar(select(DatasetBlock).where(DatasetBlock.organization_id == user.organization_id,
        DatasetBlock.dataset_id.in_(identities), DatasetBlock.active.is_(True), DatasetBlock.scope.in_(scopes)))
    if block:
        raise OperationError(403, "DATASET_BLOCKED", "Una restricción vigente impide el uso solicitado.",
                             {"block_id": block.id, "scope": block.scope, "reason": block.reason})
    if purpose.upper() != "METADATA" and len(ancestors) > 1 and any(dataset.status != "ACTIVE" for dataset in ancestors):
        raise OperationError(403, "SOURCE_UNAVAILABLE", "Una fuente del activo derivado está inactiva.")


def authorize_run_content(db: Session, user: User, run: Run) -> None:
    if run.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró la ejecución.")
    references = db.scalars(select(ArtifactLink.target_id).where(ArtifactLink.organization_id == run.organization_id,
        ArtifactLink.source_type == "RUN", ArtifactLink.source_id == run.id,
        ArtifactLink.relation == "RUN_REFERENCE", ArtifactLink.target_type == "DATASET_VERSION"))
    for identity in (run.dataset_version_id, run.target_version_id, *references):
        if identity:
            version = db.get(DatasetVersion, identity)
            if version is None:
                raise OperationError(403, "SOURCE_ACCESS_DENIED", "La procedencia de la ejecución está incompleta.")
            authorize_dataset_content(db, user, version, "MODULE")


def authorize_dataset_content(db: Session, user: User, version: DatasetVersion, purpose: str = "CONTENT") -> None:
    if version.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró la versión solicitada.")
    authorize_dataset(db, user, version.dataset_id, purpose)
    # Immutable parent links also protect Intake descendants created before the
    # additive identity backfill; renaming or revalidating never severs ancestry.
    seen: set[str] = set()
    current = version
    while current.parent_version_id:
        if current.id in seen or len(seen) >= MAX_ANCESTORS:
            raise OperationError(403, "SECURITY_LINEAGE_INVALID", "La procedencia de la versión no es verificable.")
        seen.add(current.id)
        parent = db.get(DatasetVersion, current.parent_version_id)
        if parent is None or parent.organization_id != user.organization_id:
            raise OperationError(403, "SOURCE_ACCESS_DENIED", "La procedencia de la versión está incompleta.")
        authorize_dataset(db, user, parent.dataset_id, purpose)
        current = parent


def add_security_dependency(db: Session, child_dataset_id: str, source_dataset_id: str, actor: Any = None) -> None:
    child, source = db.get(Dataset, child_dataset_id), db.get(Dataset, source_dataset_id)
    if not child or not source or child.organization_id != source.organization_id:
        raise OperationError(422, "SECURITY_SOURCE_INVALID", "Las fuentes deben pertenecer a la misma organización.")
    if child.id == source.id or child.id in {item.id for item in dataset_ancestors(db, source.id, child.organization_id)}:
        raise OperationError(422, "SECURITY_LINEAGE_CYCLE", "Esta relación crearía un ciclo de procedencia.")
    existing = db.scalar(select(DatasetSecurityDependency).where(DatasetSecurityDependency.dataset_id == child.id,
        DatasetSecurityDependency.source_dataset_id == source.id))
    if existing:
        if not existing.active:
            existing.active, existing.version = True, existing.version + 1
            from .services import audit
            audit(db, "SECURITY_DEPENDENCY_REESTABLISHED", "dataset_security_dependency", existing.id,
                "Una nueva derivación volvió a consumir esta fuente.", actor or "Worker", child.organization_id,
                {"dataset_id": child.id, "source_dataset_id": source.id, "version": existing.version})
            db.flush()
        return
    db.add(DatasetSecurityDependency(organization_id=child.organization_id, dataset_id=child.id, source_dataset_id=source.id))
    db.flush()


def strict_approval(db: Session, run: Run) -> dict:
    """Conservative metadata proof. Hash/byte verification belongs to the worker.

    Missing historical accounting is never synthesized from a percentage or from
    the sum of evaluations of overlapping rules.
    """
    reasons: list[dict] = []
    def reject(code: str, message: str) -> None:
        reasons.append({"code": code, "message": message})
    source = db.get(DatasetVersion, run.dataset_version_id)
    output = db.get(DatasetVersion, run.output_version_id) if run.output_version_id else None
    config = db.get(Configuration, run.config_id)
    metrics = run.metrics or {}
    if run.module.lower() != "intake" or run.status != "SUCCESS" or run.finished_at is None or run.cancel_requested:
        reject("EXECUTION_NOT_COMPLETE", "La ejecución Intake no terminó correctamente.")
    if run.decision != "APPROVED" or metrics.get("decision") != "APPROVED":
        reject("DECISION_NOT_APPROVED", "La decisión no es APPROVED sin advertencias.")
    if not source or not output or not config or any(item.organization_id != run.organization_id for item in (source, output, config) if item):
        reject("EVIDENCE_IDENTITY_MISSING", "Faltan las relaciones de entrada, contrato, ejecución y salida.")
    elif config.dataset_id != source.dataset_id or config.module.lower() != "intake" or output.parent_version_id != source.id or output.source_run_id != run.id or output.source_type != "INTAKE_OUTPUT":
        reject("EVIDENCE_IDENTITY_MISMATCH", "La salida no acredita esta entrada y revisión del contrato.")
    counts = ("total_rows", "processed_rows", "valid_rows", "output_rows", "error_rows", "warning_rows", "discarded_rows", "validation_coverage_rows")
    valid_counts = all(type(metrics.get(key)) is int and metrics[key] >= 0 for key in counts)
    if not valid_counts:
        reject("ACCOUNTING_INSUFFICIENT", "La evidencia histórica carece de contabilidad íntegra; requiere nueva validación.")
    else:
        total = metrics["total_rows"]
        if total <= 0 or source and source.row_count <= 0:
            reject("EMPTY_INPUT", "La entrada vacía no acredita aprobación estricta.")
        if any(metrics[key] for key in ("error_rows", "warning_rows", "discarded_rows")):
            reject("QUALITY_FINDINGS", "Existen errores, advertencias o descartes.")
        if any(metrics[key] != total for key in ("processed_rows", "valid_rows", "output_rows")) or source and source.row_count != total or output and output.row_count != total:
            reject("ROW_COUNTS_INCONSISTENT", "Los conteos de entrada, procesamiento y salida no coinciden.")
        if metrics["validation_coverage_rows"] == 0:
            reject("NO_EFFECTIVE_VALIDATION", "Ninguna fila recibió una validación efectiva.")
        if metrics["validation_coverage_rows"] != total:
            reject("VALIDATION_COVERAGE_INCOMPLETE", "La aprobación estricta requiere que cada fila reciba al menos una validación efectiva; las condiciones no aplicables y exclusiones IGNORE no aportan cobertura.")
    rules = metrics.get("rules")
    try:
        from .processing import configured_rules
        configured = configured_rules(config.config) if config else []
    except (ValueError, KeyError):
        configured = []
    if not configured or not isinstance(rules, list) or not rules:
        reject("NO_VALIDATION_RULES", "El contrato debe habilitar controles de validación; las transformaciones no bastan.")
    elif Counter((rule.rule_id, rule.code, tuple(rule.parameters.get("columns", [rule.column] if rule.column else [])), rule.severity)
            if rule.type != "column_compare" else (rule.rule_id, rule.code, (*(rule.parameters.get("columns", [rule.column] if rule.column else [])), rule.parameters["other_column"]), rule.severity)
            for rule in configured) != Counter((item.get("rule_id"), item.get("code"), tuple(item.get("columns", [])), item.get("severity")) for item in rules):
        reject("RULE_EVIDENCE_MISMATCH", "La evidencia no corresponde a todos los controles habilitados.")
    else:
        total = metrics.get("total_rows")
        evaluated = 0
        for rule in rules:
            if not all(type(rule.get(key)) is int and rule[key] >= 0 for key in ("evaluated_count", "skipped_count", "failed_count")):
                reject("RULE_ACCOUNTING_INSUFFICIENT", "Faltan conteos de evaluación y condiciones no aplicables.")
                break
            if rule["failed_count"] or rule["evaluated_count"] + rule["skipped_count"] != total or rule.get("status") != "PASS":
                reject("RULE_EVALUATION_FAILED", "Los controles presentan fallos o contabilidad incoherente.")
                break
            evaluated += rule["evaluated_count"]
        if evaluated == 0:
            reject("NO_EFFECTIVE_VALIDATION", "Ningún control de validación se evaluó positivamente.")
    artifact = db.get(Artifact, output.canonical_artifact_id) if output and output.canonical_artifact_id else None
    evidence = db.scalar(select(Artifact).where(Artifact.path == run.evidence_path, Artifact.organization_id == run.organization_id)) if run.evidence_path else None
    if not artifact or not evidence or artifact.organization_id != run.organization_id or not artifact.sha256 or not evidence.sha256:
        reject("ARTIFACT_EVIDENCE_MISSING", "Faltan los artefactos registrados de salida y evidencia.")
    elif output:
        required = {("RUN_INPUT", "RUN", run.id, "DATASET_VERSION", run.dataset_version_id),
                    ("RUN_OUTPUT", "RUN", run.id, "DATASET_VERSION", output.id),
                    ("RUN_OUTPUT", "RUN", run.id, "ARTIFACT", evidence.id)}
        actual = {(link.relation, link.source_type, link.source_id, link.target_type, link.target_id) for link in db.scalars(
            select(ArtifactLink).where(ArtifactLink.organization_id == run.organization_id, ArtifactLink.source_id == run.id))}
        if not required <= actual:
            reject("LINEAGE_EVIDENCE_MISSING", "El linaje registrado no confirma todos los hechos de aprobación.")
    identity = None
    if config:
        try:
            identity = contract_identity(db, config)
        except OperationError:
            reject("CONTRACT_LINEAGE_INVALID", "La identidad del contrato no se puede comprobar.")
    return {"approved": not reasons, "reasons": reasons, "run_id": run.id,
            "criterion_version": STRICT_APPROVAL_CRITERION_VERSION, "criterion": STRICT_APPROVAL_CRITERION,
            "input_version_id": source.id if source else None, "output_version_id": output.id if output else None,
            "contract_id": identity, "contract_revision_id": config.id if config else None,
            "finished_at": iso(run.finished_at), "verified_bytes": False}


def index_strict_approval(db: Session, run: Run) -> None:
    result = strict_approval(db, run)
    if not result["approved"] or db.scalar(select(StrictApproval.id).where(StrictApproval.run_id == run.id)):
        return
    source = db.get(DatasetVersion, run.dataset_version_id)
    assert source is not None
    dataset = db.get(Dataset, source.dataset_id)
    assert dataset is not None
    snapshot = (run.execution_plan or {}).get("governance_snapshot")
    # Missing legacy snapshots stay explicitly unknown; never attribute current
    # classification to an earlier run.
    db.add(StrictApproval(organization_id=run.organization_id, run_id=run.id, input_version_id=source.id,
        output_version_id=result["output_version_id"], contract_id=result["contract_id"],
        criterion_version=STRICT_APPROVAL_CRITERION_VERSION,
        contract_revision_id=run.config_id, evidence_hash=configuration_hash({"run_id": run.id, "metrics": run.metrics,
            "evidence_path": run.evidence_path}), governance_snapshot=snapshot or {"historical_governance": "UNKNOWN"}))


def verify_strict_approval_artifacts(db: Session, run: Run) -> dict:
    """Worker-only complete integrity check using committed, detached metadata.

    A separate short read transaction prevents file hashing and footer reads from
    retaining coordinator/selection locks. No dispatch or tree endpoint calls it.
    """
    from .artifactstore import ArtifactIntegrityError, storage_provider
    from .config_semantics import effective_config
    with Session(bind=db.get_bind(), expire_on_commit=False) as metadata:
        committed = metadata.get(Run, run.id)
        if committed is None:
            raise OperationError(412, "APPROVAL_NOT_COMMITTED", "La aprobación debe estar confirmada antes de leerla.")
        assessment = strict_approval(metadata, committed)
        if not assessment["approved"]:
            raise OperationError(412, "STRICT_APPROVAL_REQUIRED", "La aprobación estricta ya no es verificable.", assessment["reasons"])
        source = metadata.get(DatasetVersion, committed.dataset_version_id)
        output = metadata.get(DatasetVersion, committed.output_version_id)
        config = metadata.get(Configuration, committed.config_id)
        assert source and output and config
        evidence = metadata.scalar(select(Artifact).where(Artifact.path == committed.evidence_path,
            Artifact.organization_id == committed.organization_id))
        assert evidence is not None
        canonical = [metadata.get(Artifact, version.canonical_artifact_id) for version in (source, output)]
        original = metadata.get(Artifact, source.original_artifact_id) if source.original_artifact_id else None
        expected = {"run_id": committed.id, "module": committed.module, "input_id": source.id, "output_id": output.id,
            "input_sha256": source.sha256, "input_schema_hash": source.schema_hash,
            "output_schema_hash": output.schema_hash, "output_rows": output.row_count,
            "config_id": config.id, "config_version": config.version,
            "stored_config_hash": configuration_hash(config.config),
            "effective_config_hash": configuration_hash(effective_config(config.module, config.config)),
            "metrics_hash": configuration_hash(copy.deepcopy(committed.metrics)),
            "input_canonical_hash": canonical[0].sha256 if canonical[0] else None,
            "output_canonical_hash": canonical[1].sha256 if canonical[1] else None,
            "output_artifact_id": canonical[1].id if canonical[1] else None}
        versions = [(version.id, version.row_count, version.column_count, copy.deepcopy(version.schema_json)) for version in (source, output)]
        metadata.commit()
    try:
        manifest = json.loads(storage_provider.materialize(evidence).read_text(encoding="utf-8"))
        configuration = manifest.get("configuration", {})
        input_proof: dict = next((item for item in manifest.get("inputs", []) if item.get("dataset_version_id") == expected["input_id"]), {})
        output_proof: dict = next((item for item in manifest.get("result_artifacts", []) if item.get("artifact_id") == expected["output_artifact_id"]), {})
        output_identity: dict = next((item for item in manifest.get("outputs", []) if item.get("dataset_version_id") == expected["output_id"]), {})
        valid = (manifest.get("run_id") == expected["run_id"] and manifest.get("module") == expected["module"]
            and manifest.get("output_version_id") == expected["output_id"] and configuration.get("id") == expected["config_id"]
            and configuration.get("version") == expected["config_version"]
            and configuration.get("stored_config_hash") == expected["stored_config_hash"]
            and configuration.get("config_hash") == expected["effective_config_hash"]
            and configuration_hash(manifest.get("metrics", {})) == expected["metrics_hash"]
            and input_proof.get("artifact_sha256") == expected["input_sha256"]
            and input_proof.get("schema_hash") == expected["input_schema_hash"]
            and input_proof.get("canonical_sha256") == expected["input_canonical_hash"]
            and output_proof.get("sha256") == expected["output_canonical_hash"]
            and output_identity.get("schema_hash") == expected["output_schema_hash"]
            and output_identity.get("row_count") == expected["output_rows"]
            and output_identity.get("canonical_sha256") == expected["output_canonical_hash"])
        if not valid:
            raise ArtifactIntegrityError("La evidencia no coincide con las identidades y hashes congelados.")
        if original:
            storage_provider.materialize(original)
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
        parts = 0
        for artifact, (_, rows, columns, schema) in zip(canonical, versions, strict=True):
            if artifact is None:
                raise ArtifactIntegrityError("Falta un artefacto canónico registrado.")
            total = 0
            paths = storage_provider.dataset_paths(artifact)
            parts += len(paths)
            for path in paths:
                with pq.ParquetFile(path, memory_map=False, pre_buffer=False) as parquet:
                    total += parquet.metadata.num_rows
                    # Internal row numbering is not a logical business column.
                    names = [name for name in parquet.schema_arrow.names if not name.startswith("__tv_")]
                    if len(names) != columns or names != [column["name"] for column in schema]:
                        raise ArtifactIntegrityError("El esquema físico no coincide con las columnas publicadas.")
            if total != rows:
                raise ArtifactIntegrityError("La población física no coincide con el conteo completo de la versión.")
    except (OSError, ValueError, KeyError, ArtifactIntegrityError) as exc:
        raise OperationError(412, "APPROVAL_INTEGRITY_FAILED", "La salida o evidencia de aprobación no superó la comprobación íntegra.") from exc
    return {**assessment, "verified_bytes": True, "parts_verified": parts, "evidence_sha256": evidence.sha256}


def dataset_eligibility(db: Session, user: User, version: DatasetVersion, purpose: str = "REPORT") -> dict:
    dataset = db.get(Dataset, version.dataset_id)
    if not dataset or dataset.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el dataset.")
    governance = governance_snapshot(db, dataset)
    run = db.get(Run, version.source_run_id) if version.source_run_id else None
    approval: dict = strict_approval(db, run) if run else {"approved": False, "reasons": [{"code": "NO_INTAKE_APPROVAL", "message": "Pendiente de validación de calidad en Data Intake."}]}
    related_output = None
    if not run and "intake:read" in effective_permissions(db, user):
        # A validated input remains an input. Navigate through exact registered
        # relationships without transferring its output's approval to it.
        candidates = select(Run).where(Run.organization_id == user.organization_id,
            Run.dataset_version_id == version.id, Run.module == "intake", Run.output_version_id.is_not(None))
        with db.scalars(candidates.order_by(Run.finished_at.desc(), Run.id.desc())
                        .execution_options(yield_per=32)) as runs:
            for candidate in runs:
                if not strict_approval(db, candidate)["approved"]:
                    continue
                output = db.get(DatasetVersion, candidate.output_version_id)
                if not output:
                    continue
                try:
                    authorize_dataset_content(db, user, output, purpose)
                except OperationError:
                    continue
                related_output = {"output_dataset_id": output.dataset_id, "output_version_id": output.id,
                    "approval_run_id": candidate.id, "input_dataset_id": dataset.id, "input_version_id": version.id}
                approval = {**approval, "reasons": [{"code": "INPUT_HAS_APPROVED_OUTPUT",
                    "message": "Esta versión de entrada tiene una salida Intake aprobada. Reportes consume esa salida exacta; la entrada conserva su estado propio."}]}
                break
    reasons = list(approval["reasons"])
    if not governance["classification_complete"]:
        reasons.append({"code": "CLASSIFICATION_INCOMPLETE", "message": "Completa macrodominio y dominio activos para Reportes."})
    artifact = db.get(Artifact, version.canonical_artifact_id) if version.canonical_artifact_id else None
    available = bool(dataset.status == "ACTIVE" and artifact and artifact.organization_id == user.organization_id and artifact.sha256 and version.profile_status == "READY")
    if not available:
        reasons.append({"code": "SOURCE_UNAVAILABLE", "message": "La versión no tiene un artefacto canónico disponible."})
    try:
        authorize_dataset_content(db, user, version, purpose)
        if purpose.upper() in {"REPORT", "REPORTS"} and "intake:read" not in effective_permissions(db, user):
            raise OperationError(403, "SOURCE_ACCESS_DENIED", "Tu rol no permite consultar la aprobación de Intake.")
    except OperationError as exc:
        reasons.append({"code": exc.code, "message": exc.message})
    return {"eligible": not reasons, "classification_complete": governance["classification_complete"],
            "strict_approval": approval, "availability": {"available": available, "verified_bytes": False},
            "reasons": reasons, "governance": governance, "related_approved_output": related_output}


def update_governance(db: Session, dataset: Dataset, user: User, expected_version: int, changes: dict) -> None:
    from .governance_people import resolve_governance_assignments
    from .services import audit
    if verified_intake_parent(db, dataset):
        raise OperationError(409, "GOVERNANCE_INHERITED", "Edita el gobierno en el dataset de entrada; esta salida lo hereda.")
    macro = changes.get("macro_domain_id", dataset.macro_domain_id)
    domain = changes.get("domain_id", dataset.domain_id)
    classification_changed = (macro, domain) != (dataset.macro_domain_id, dataset.domain_id)
    validate_classification(db, user.organization_id, macro, domain, require_active=classification_changed)
    changes = resolve_governance_assignments(db, user.organization_id, changes, dataset)
    result = db.execute(update(Dataset).where(Dataset.id == dataset.id, Dataset.organization_id == user.organization_id,
        Dataset.governance_version == expected_version).values(**changes, governance_version=expected_version + 1)
        .execution_options(synchronize_session=False))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "VERSION_CONFLICT", "El gobierno cambió. Actualiza la ficha antes de guardar.")
    db.refresh(dataset)
    snapshot = governance_snapshot(db, dataset)
    db.add(GovernanceHistory(organization_id=user.organization_id, dataset_id=dataset.id, version=dataset.governance_version,
        actor_id=user.id, snapshot=snapshot, reason="Edición de gobierno"))
    audit(db, "GOVERNANCE_UPDATED", "dataset", dataset.id, "Gobierno actualizado", user.name, user.organization_id,
          {"governance_version": dataset.governance_version})
