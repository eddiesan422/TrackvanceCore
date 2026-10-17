"""Bounded, authorized metadata traversal; stored relationship directions stay intact."""

from collections import deque
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .db import get_db, iso
from .governance import authorize_dataset, authorize_dataset_content, authorize_run_content
from .models import Artifact, ArtifactLink, Configuration, Dataset, DatasetVersion, Run, User
from .operations_common import OperationError
from .permissions import effective_permissions

router = APIRouter(prefix="/api/v1/catalog", tags=["Linaje gráfico"])
REVERSE = frozenset({"DERIVED_FROM", "INTAKE_ACCEPTED_FROM", "SOURCE_SNAPSHOT", "REFRESH_OF",
                     "RUN_INPUT", "RUN_REFERENCE", "USES_CONFIGURATION", "REPORT_SOURCE",
                     "REPORT_APPROVAL", "REPORT_REVISION", "EXPORT_OF", "EVIDENCE_OF",
                     "COMPARISON_INPUT", "COMPARISON_EVIDENCE"})


def _artifact_is_authorized(db, user, artifact, grants):
    """Prove domain ownership independently of whichever graph edge reached it."""
    identities, pending = {artifact.id}, [artifact.id]
    while pending:
        identity = pending.pop()
        parents = db.scalars(select(ArtifactLink.source_id).where(
            ArtifactLink.organization_id == user.organization_id, ArtifactLink.relation == "DATASET_PART",
            ArtifactLink.source_type == "ARTIFACT", ArtifactLink.target_type == "ARTIFACT",
            ArtifactLink.target_id == identity).limit(129)).all()
        for parent in parents:
            if parent not in identities:
                if len(identities) >= 128:
                    return False
                identities.add(parent)
                pending.append(parent)
    versions = db.scalars(select(DatasetVersion).where(or_(
        DatasetVersion.canonical_artifact_id.in_(identities), DatasetVersion.original_artifact_id.in_(identities)))
        .limit(129)).all()
    if len(versions) > 128:
        return False
    if versions and "artifacts:download" not in grants:
        return False
    for version in versions:
        authorize_dataset_content(db, user, version)
    relationships = db.scalars(select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
        or_(and_(ArtifactLink.source_type == "RUN", ArtifactLink.target_type == "ARTIFACT",
                 ArtifactLink.target_id.in_(identities), ArtifactLink.relation.in_(["RUN_OUTPUT", "REST_BATCH_PAYLOAD"])),
            and_(ArtifactLink.source_type == "ARTIFACT", ArtifactLink.target_type == "RUN",
                 ArtifactLink.source_id.in_(identities), ArtifactLink.relation.in_(["EXPORT_OF", "EVIDENCE_OF"]))))
        .limit(129)).all()
    if len(relationships) > 128:
        return False
    if relationships and "artifacts:download" not in grants:
        return False
    for link in relationships:
        run_id = link.source_id if link.source_type == "RUN" else link.target_id
        run = db.get(Run, run_id)
        if run is None or run.organization_id != user.organization_id or f"{run.module.lower()}:read" not in grants:
            return False
        authorize_run_content(db, user, run)
    from .comparison_api import authorize_comparison
    from .comparison_models import VersionComparison
    from .document_models import DocumentOriginal

    originals = db.scalars(select(DocumentOriginal).where(DocumentOriginal.organization_id == user.organization_id,
        DocumentOriginal.artifact_id.in_(identities)).limit(129)).all()
    document_links = db.scalars(select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
        ArtifactLink.source_type.in_(["DOCUMENT", "DOCUMENT_EXTRACTION"]),
        ArtifactLink.target_type == "ARTIFACT", ArtifactLink.target_id.in_(identities)).limit(129)).all()
    comparisons = db.scalars(select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
        ArtifactLink.source_type == "ARTIFACT", ArtifactLink.source_id.in_(identities),
        ArtifactLink.target_type == "VERSION_COMPARISON", ArtifactLink.relation == "COMPARISON_EVIDENCE").limit(129)).all()
    if max(len(originals), len(document_links), len(comparisons)) > 128:
        return False
    if (originals or document_links) and not {"documents:read", "documents:download"} <= grants:
        return False
    for link in document_links:
        if _node(db, user, link.source_type, link.source_id, grants) is None:
            return False
    for link in comparisons:
        if "artifacts:download" not in grants:
            return False
        comparison = db.get(VersionComparison, link.target_id)
        if comparison is None:
            return False
        authorize_comparison(db, comparison, user)
    return bool(versions or relationships or originals or document_links or comparisons)


def _node(db, user, kind, identity, grants):
    from .acquisition_models import AcquisitionRun
    from .comparison_models import VersionComparison
    from .document_models import (
        DocumentBatch,
        DocumentExtraction,
        DocumentFamily,
        DocumentOriginal,
        DocumentTemplate,
    )
    from .models import (
        DeliveryAttempt,
        DeliveryDestination,
        DeliveryDestinationVersion,
        DeliveryTargetPolicy,
        ExternalConnection,
        ExternalConnectionVersion,
    )
    from .report_models import ReportDefinition, ReportExecution, ReportRevision

    models = {"DATASET_VERSION": DatasetVersion, "RUN": Run, "CONFIGURATION": Configuration,
              "ARTIFACT": Artifact, "ACQUISITION": AcquisitionRun,
              "CONNECTION_VERSION": ExternalConnectionVersion,
              "DELIVERY_DESTINATION_VERSION": DeliveryDestinationVersion,
              "DELIVERY_TARGET_POLICY": DeliveryTargetPolicy, "DELIVERY_ATTEMPT": DeliveryAttempt,
              "REPORT_DEFINITION": ReportDefinition, "REPORT_REVISION": ReportRevision,
              "REPORT_EXECUTION": ReportExecution, "DOCUMENT": DocumentOriginal,
              "DOCUMENT_EXTRACTION": DocumentExtraction, "DOCUMENT_BATCH": DocumentBatch,
              "DOCUMENT_FAMILY": DocumentFamily, "DOCUMENT_TEMPLATE": DocumentTemplate,
              "VERSION_COMPARISON": VersionComparison}
    model = models.get(kind)
    record = db.get(model, identity) if model else None
    if record is None or record.organization_id != user.organization_id:
        return None
    result = {"id": f"{kind}:{identity}", "resource_id": identity, "type": kind,
              "label": identity, "href": None, "created_at": iso(record.created_at),
              "status": None, "decision": None, "technical": kind == "ARTIFACT"}
    try:
        if kind == "DATASET_VERSION":
            asset = db.get(Dataset, record.dataset_id)
            if not asset or asset.organization_id != user.organization_id:
                return None
            authorize_dataset(db, user, asset.id, "METADATA")
            result.update(label=f"{asset.name} · v{record.version}",
                href=f"/datasets/{asset.id}?version={identity}", dataset_id=asset.id,
                version=record.version, sha256=record.sha256, row_count=record.row_count,
                schema_hash=record.schema_hash, status=record.profile_status)
        elif kind in {"RUN", "CONFIGURATION"}:
            if f"{record.module.lower()}:read" not in grants:
                return None
            if kind == "RUN":
                authorize_run_content(db, user, record)
                result.update(label=record.name, href=f"/runs/{identity}", status=record.status,
                    decision=record.decision, module=record.module,
                    role="MONITORING" if record.module == "sentinel" else "EXECUTION")
            else:
                if record.dataset_id:
                    authorize_dataset(db, user, record.dataset_id, "METADATA")
                if record.target_dataset_id:
                    authorize_dataset(db, user, record.target_dataset_id, "METADATA")
                href = "/delivery" if record.module == "DELIVERY" else f"/{record.module.lower()}?contract_id={identity}"
                if record.module == "intake" and record.status == "DELETED":
                    href += "&include_deleted=true"
                result.update(label=f"{record.name} · r{record.version}",
                    href=href, module=record.module,
                    status=record.status)
        elif kind == "ACQUISITION":
            if "datasets:read" not in grants:
                return None
            if record.dataset_id:
                authorize_dataset(db, user, record.dataset_id, "METADATA")
            result.update(label="Adquisición", status=record.status,
                href=f"/operations?acquisition={identity}")
        elif kind.startswith("DOCUMENT"):
            if "documents:read" not in grants:
                return None
            original = db.get(DocumentOriginal, record.document_id) if kind == "DOCUMENT_EXTRACTION" else record if kind == "DOCUMENT" else None
            if kind == "DOCUMENT_EXTRACTION" and original is None:
                return None
            family_id = original.family_id if original else record.id if kind == "DOCUMENT_FAMILY" else record.family_id
            family = db.get(DocumentFamily, family_id)
            if not family or family.organization_id != user.organization_id or (original and original.organization_id != user.organization_id):
                return None
            href = f"/documents/families/{family.id}"
            if kind in {"DOCUMENT", "DOCUMENT_EXTRACTION"}:
                if original is None:
                    return None
                href += f"?document={original.id}"
                result.update(label=original.filename if kind == "DOCUMENT" else f"Extracción · r{record.revision}",
                    href=href, status=record.status if kind == "DOCUMENT" else "REVIEWED" if record.reviewed else "EXTRACTED",
                    sha256=record.sha256 if kind == "DOCUMENT" else record.payload_hash,
                    revision=record.revision if kind == "DOCUMENT_EXTRACTION" else None, role="DOCUMENT_METADATA")
            elif kind == "DOCUMENT_BATCH":
                result.update(label=f"{family.name} · lote", href=f"{href}?batch={identity}", status=record.status,
                    sha256=record.manifest_hash, role="DOCUMENT_PUBLICATION")
            else:
                result.update(label=family.name if kind == "DOCUMENT_FAMILY" else f"{family.name} · plantilla r{record.revision}", href=href,
                    role="DOCUMENT_CONFIGURATION")
        elif kind == "VERSION_COMPARISON":
            from .comparison_api import authorize_comparison
            if "datasets:read" not in grants:
                return None
            versions = authorize_comparison(db, record, user)
            result.update(label=f"Comparación · v{versions[0].version} → v{versions[1].version}",
                href=f"/datasets/{versions[0].dataset_id}/versions/compare?report={identity}", status=record.status,
                role="COMPARISON_EVIDENCE")
        elif kind == "CONNECTION_VERSION":
            if "connections:read" not in grants:
                return None
            connection = db.get(ExternalConnection, record.connection_id)
            if not connection or connection.organization_id != user.organization_id:
                return None
            result.update(label=f"{connection.name} · r{record.version}", href="/connections",
                          source_type=connection.source_type, sha256=record.config_hash)
        elif kind in {"DELIVERY_DESTINATION_VERSION", "DELIVERY_TARGET_POLICY", "DELIVERY_ATTEMPT"}:
            if "delivery:read" not in grants:
                return None
            if kind == "DELIVERY_ATTEMPT":
                run = db.get(Run, record.run_id)
                if not run or f"{run.module.lower()}:read" not in grants:
                    return None
                authorize_run_content(db, user, run)
                result.update(label=f"Intento {record.attempt_number}", status=record.status,
                              href=f"/runs/{run.id}")
            else:
                if "destinations:read" not in grants:
                    return None
                destination = db.get(DeliveryDestination, record.destination_id)
                if not destination or destination.organization_id != user.organization_id:
                    return None
                result.update(label=destination.name + (f" · r{record.version}" if kind.endswith("VERSION") else " · política"),
                              href="/delivery/destinations", sink_type=destination.sink_type)
        elif kind.startswith("REPORT_"):
            if "reports:read" not in grants:
                return None
            if kind == "REPORT_EXECUTION":
                if record.user_id != user.id:
                    return None
                result.update(label="Ejecución de Reportes", href=f"/reports/executions/{identity}",
                              status=record.status)
            else:
                definition = record if kind == "REPORT_DEFINITION" else db.get(ReportDefinition, record.definition_id)
                if not definition or definition.organization_id != user.organization_id:
                    return None
                result.update(label=definition.name + (f" · r{record.version}" if kind == "REPORT_REVISION" else ""),
                              href=f"/reports/definitions/{definition.id}")
        elif kind == "ARTIFACT":
            if not ({"artifacts:download", "documents:download"} & grants) or not _artifact_is_authorized(db, user, record, grants):
                return None
            # Paths, locators and file content never enter graph metadata.
            result.update(label=record.name, sha256=record.sha256, artifact_kind=record.kind,
                          size_bytes=record.size_bytes)
        return result
    except OperationError:
        return None


def _edge(link, nodes):
    source, target = f"{link.source_type}:{link.source_id}", f"{link.target_type}:{link.target_id}"
    if link.relation in REVERSE:
        source, target = target, source
    relation = link.relation
    state = "RECORDED"
    if relation == "DELIVERED_TO":
        run = nodes.get(f"RUN:{link.source_id}", {})
        state = "CONFIRMED" if run.get("status") == "SUCCESS" and run.get("decision") == "COMMITTED" else "UNCONFIRMED"
        relation = "ENTREGA_CONFIRMADA" if state == "CONFIRMED" else "DESTINO_PREVISTO"
    if link.relation in {"RUN_INPUT", "RUN_REFERENCE"} and nodes.get(target, {}).get("module") == "sentinel":
        relation = "MONITOREA"
    return {"id": link.id, "source": source, "target": target, "relation": relation,
            "stored_relation": link.relation, "stored_source": f"{link.source_type}:{link.source_id}",
            "stored_target": f"{link.target_type}:{link.target_id}", "state": state}


@router.get("/datasets/{dataset_id}/lineage-graph")
def lineage_graph(dataset_id: str, request: Request, version_id: str | None = None,
                  max_depth: int = Query(2, ge=1, le=8), max_nodes: int = Query(100, ge=10, le=500),
                  projection: Literal["technical", "business"] = "technical",
                  cursor: str | None = Query(None, max_length=32768),
                  evidence: bool = False,
                  db: Session = Depends(get_db)):
    user: User = request.state.user
    asset = db.get(Dataset, dataset_id)
    if not asset or asset.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el dataset.")
    authorize_dataset(db, user, dataset_id, "METADATA")
    versions = select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id,
                                           DatasetVersion.organization_id == user.organization_id)
    version = db.scalar(versions.where(DatasetVersion.id == version_id)) if version_id else db.scalar(
        versions.order_by(DatasetVersion.version.desc()).limit(1))
    if not version:
        if version_id:
            raise OperationError(404, "NOT_FOUND", "No se encontró la versión del dataset.")
        empty: dict[str, object] = {"nodes": [], "edges": [], "root_id": None, "version_id": None,
                 "complete": True, "pending_branches": [], "limits": {"max_depth": max_depth, "max_nodes": max_nodes}}
        if projection == "business":
            from .lineage_business import MAX_PATH, PAGE_SIZE
            empty.update(projection="business", anchor_id=None, branches=[],
                         metadata_relationships_scanned=0,
                         limits={"max_depth": max_depth, "max_nodes": max_nodes,
                                 "adjacency_page_size": PAGE_SIZE, "max_path": MAX_PATH})
        return empty
    grants = set(effective_permissions(db, user))
    root = _node(db, user, "DATASET_VERSION", version.id, grants)
    if root is None:
        raise OperationError(403, "PERMISSION_DENIED", "Tu rol no permite consultar esta versión.")
    if projection == "business":
        from .lineage_business import business_graph
        return business_graph(db, request, root, grants, max_depth, max_nodes, cursor, evidence)
    if cursor or evidence:
        raise OperationError(422, "LINEAGE_CURSOR_INVALID", "La continuación requiere la vista de negocio.")
    nodes, links, pending = {root["id"]: root}, {}, set()
    queue = deque([(root["id"], 0)])
    visited = set()
    budget = max_nodes * 8
    while queue and budget > 0:
        key, depth = queue.popleft()
        if key in visited:
            continue
        visited.add(key)
        kind, identity = key.split(":", 1)
        adjacency = db.scalars(select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
            or_(and_(ArtifactLink.source_type == kind, ArtifactLink.source_id == identity),
                and_(ArtifactLink.target_type == kind, ArtifactLink.target_id == identity)))
            .order_by(ArtifactLink.created_at, ArtifactLink.id).limit(min(budget, max_nodes * 2) + 1)).all()
        if len(adjacency) > min(budget, max_nodes * 2):
            pending.add(key)
            adjacency = adjacency[:min(budget, max_nodes * 2)]
        budget -= len(adjacency)
        for link in adjacency:
            other_kind, other_id = ((link.target_type, link.target_id) if
                (link.source_type, link.source_id) == (kind, identity) else (link.source_type, link.source_id))
            other_key = f"{other_kind}:{other_id}"
            other = nodes.get(other_key) or _node(db, user, other_kind, other_id, grants)
            if other is None:
                continue
            if other_key not in nodes and (depth >= max_depth or len(nodes) >= max_nodes):
                pending.add(key)
                continue
            nodes[other_key] = other
            links[link.id] = link
            if other_key not in visited:
                queue.append((other_key, depth + 1))
    pending.update(key for key, _ in queue if key not in visited)
    for key, node in nodes.items():
        node["has_more"] = key in pending
    return {"nodes": list(nodes.values()), "edges": [_edge(link, nodes) for link in links.values()],
            "root_id": root["id"], "version_id": version.id, "complete": not pending,
            "pending_branches": sorted(pending), "limits": {"max_depth": max_depth, "max_nodes": max_nodes},
            "semantics": "METADATA_ONLY_NO_ROW_OR_COLUMN_CORRESPONDENCE"}
