"""Business-first metadata projection and session-scoped, directed continuations.

Artifact links are excluded in SQL before the traversal budget is spent. Technical
evidence is counted as relationships, never as downloadable/authorized artifacts;
individual artifacts are authorized again when their bounded evidence page opens.
"""

import base64
import hashlib
import hmac
import json
import time
from collections import deque

from sqlalchemy import and_, case, func, literal, or_, select, union_all

from .governance import authorize_dataset_content
from .lineage_api import REVERSE, _edge, _node
from .models import ArtifactLink, DatasetVersion
from .operations_common import OperationError

PAGE_SIZE = 80
MAX_PATH = 64
CURSOR_SECONDS = 1800
RELATION_LABELS = {
    "DERIVED_FROM": "Derivada de", "INTAKE_ACCEPTED_FROM": "Datos aceptados",
    "SOURCE_SNAPSHOT": "Fuente de la versión", "REFRESH_OF": "Actualiza",
    "RUN_INPUT": "Entrada", "RUN_REFERENCE": "Referencia", "DELIVERY_INPUT": "Entrada de entrega",
    "RUN_OUTPUT": "Salida", "ACQUISITION_OUTPUT": "Publica", "USES_CONFIGURATION": "Usa contrato",
    "REPORT_SOURCE": "Entrada de Reportes", "REPORT_OUTPUT": "Salida de Reportes",
    "REPORT_APPROVAL": "Aprobación de entrada", "REPORT_REVISION": "Definición del reporte",
    "DELIVERED_TO": "Destino de entrega", "AUDITED_TARGET": "Política del destino",
    "MONITOREA": "Monitorea", "ENTREGA_CONFIRMADA": "Entrega confirmada",
    "DESTINO_PREVISTO": "Destino previsto", "ENTREGA_PARCIAL": "Entrega parcial",
    "ENTREGA_INCIERTA": "Confirmación incierta",
    "DOCUMENT_ORIGINAL": "Original documental", "DOCUMENT_EXTRACTION": "Extracción y revisión",
    "DOCUMENT_BATCH_MEMBER": "Pertenece al lote", "EXTRACTION_BATCH": "Revisión publicada en lote",
    "DOCUMENT_PUBLICATION": "Publicación del lote", "COMPARISON_INPUT": "Versión comparada",
    "COMPARISON_EVIDENCE": "Informe de comparación",
}
EVIDENCE_LABELS = {
    "REST_BATCH_PAYLOAD": "Payloads de lotes REST", "DATASET_PART": "Partes Parquet",
    "RUN_OUTPUT": "Archivos de resultado", "VERSION_ARTIFACT": "Archivos de la versión",
    "REPORT_OUTPUT": "Archivos del reporte", "EXPORT_OF": "Exportaciones",
    "EVIDENCE_OF": "Evidencias", "DELIVERY_RECEIPT": "Comprobantes de entrega",
    "CANONICAL_REFERENCE": "Archivo canónico de la versión", "ORIGINAL_REFERENCE": "Archivo original de la versión",
    "DOCUMENT_ORIGINAL": "Originales documentales", "DOCUMENT_EXTRACTION_RESULT": "Resultados de extracción",
    "COMPARISON_EVIDENCE": "Informes de comparación",
}


def _invalid():
    return OperationError(422, "LINEAGE_CURSOR_INVALID",
                          "La continuación venció o no corresponde a esta versión y sesión. Actualiza el linaje.")


def _encode(request, root, path, direction="both", after="", technical=False):
    body = json.dumps({"v": 1, "root": root, "user": request.state.user.id,
                       "path": path, "direction": direction, "after": after,
                       "technical": technical, "expires": int(time.time()) + CURSOR_SECONDS},
                      separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(request.state.session.token_hash.encode(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode()


def _decode(db, request, root, grants, cursor, technical):
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        body, signature = raw[:-32], raw[-32:]
        expected = hmac.new(request.state.session.token_hash.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        value = json.loads(body)
        path = value["path"]
        if (value["v"] != 1 or value["root"] != root or value["user"] != request.state.user.id
                or value["technical"] is not technical or value["expires"] < time.time()
                or not isinstance(path, list) or not 1 <= len(path) <= MAX_PATH
                or path[0] != root or value["direction"] not in {"both", "upstream", "downstream"}
                or not isinstance(value["after"], str) or len(value["after"]) > 110):
            raise ValueError
        previous = None
        for key in path:
            if not isinstance(key, str) or len(key) > 110:
                raise ValueError
            kind, identity = key.split(":", 1)
            if kind == "ARTIFACT" or _node(db, request.state.user, kind, identity, grants) is None:
                raise ValueError
            if previous is not None:
                prev_kind, prev_id = previous.split(":", 1)
                exists = db.scalar(select(ArtifactLink.id).where(
                    ArtifactLink.organization_id == request.state.user.organization_id,
                    or_(and_(ArtifactLink.source_type == prev_kind, ArtifactLink.source_id == prev_id,
                             ArtifactLink.target_type == kind, ArtifactLink.target_id == identity),
                        and_(ArtifactLink.target_type == prev_kind, ArtifactLink.target_id == prev_id,
                             ArtifactLink.source_type == kind, ArtifactLink.source_id == identity))).limit(1))
                if not exists:
                    raise ValueError
            previous = key
        return value
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise _invalid() from None


def _adjacency(user, key, direction="both", technical=False):
    kind, identity = key.split(":", 1)
    source = and_(ArtifactLink.source_type == kind, ArtifactLink.source_id == identity)
    target = and_(ArtifactLink.target_type == kind, ArtifactLink.target_id == identity)
    condition = or_(source, target)
    if direction != "both":
        forward, reverse = (source, target) if direction == "downstream" else (target, source)
        condition = or_(and_(forward, ArtifactLink.relation.not_in(REVERSE)),
                        and_(reverse, ArtifactLink.relation.in_(REVERSE)))
    artifact = or_(ArtifactLink.source_type == "ARTIFACT", ArtifactLink.target_type == "ARTIFACT")
    return select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
                                     condition, artifact if technical else ~artifact)


def _technical_groups(db, user, key, grants):
    documentary = key.startswith(("DOCUMENT:", "DOCUMENT_EXTRACTION:")) and "documents:download" in grants
    if "artifacts:download" not in grants and not documentary:
        return []
    if key.startswith("DATASET_VERSION:"):
        try:
            authorize_dataset_content(db, user, db.get(DatasetVersion, key.split(":", 1)[1]))
        except OperationError:
            return []
    rows = _technical_query(db, user, key).subquery()
    query = select(rows.c.relation, func.count()).group_by(rows.c.relation)
    # These are counts of recorded links, not a promise of download authorization.
    return [{"relation": relation, "label": EVIDENCE_LABELS.get(relation, "Evidencia técnica"),
             "relationship_count": count} for relation, count in db.execute(query)]


def _technical_query(db, user, key):
    """Recorded artifact references plus one level of actual DATASET_PART links.

    The subquery stays in SQL even for 1,000 payloads. DatasetVersion foreign keys
    are references, not fabricated ArtifactLink records, and are labelled as such.
    """
    direct = _adjacency(user, key, technical=True).with_only_columns(
        ArtifactLink.id.label("id"), ArtifactLink.relation.label("relation"),
        case((ArtifactLink.source_type == "ARTIFACT", ArtifactLink.source_id),
             else_=ArtifactLink.target_id).label("artifact_id"))
    references = [direct]
    if key.startswith("DATASET_VERSION:"):
        version = db.get(DatasetVersion, key.split(":", 1)[1])
        for relation, identity in (("CANONICAL_REFERENCE", version.canonical_artifact_id),
                                   ("ORIGINAL_REFERENCE", version.original_artifact_id)):
            if identity:
                references.append(select(literal(f"{relation}:{version.id}").label("id"),
                                         literal(relation).label("relation"),
                                         literal(identity).label("artifact_id")))
    owner_references = union_all(*references).subquery() if len(references) > 1 else direct.subquery()
    parts = select(ArtifactLink.id.label("id"), ArtifactLink.relation.label("relation"),
                   ArtifactLink.target_id.label("artifact_id")).where(
        ArtifactLink.organization_id == user.organization_id,
        ArtifactLink.source_type == "ARTIFACT", ArtifactLink.target_type == "ARTIFACT",
        ArtifactLink.relation == "DATASET_PART",
        ArtifactLink.source_id.in_(select(owner_references.c.artifact_id)))
    return union_all(select(owner_references), parts)


def _business_edges(links, nodes):
    grouped = {}
    for link in links.values():
        edge = _edge(link, nodes)
        if link.relation == "DELIVERED_TO":
            run = nodes.get(edge["source"], {})
            if run.get("decision") == "PARTIAL":
                edge.update(state="PARTIAL", relation="ENTREGA_PARCIAL")
            elif run.get("status") == "UNKNOWN" or run.get("decision") == "UNKNOWN":
                edge.update(state="UNKNOWN", relation="ENTREGA_INCIERTA")
        # Only the same source, target and semantic state are visually grouped.
        key = (edge["source"], edge["target"], edge["state"], edge["relation"] == "MONITOREA")
        if key not in grouped:
            grouped[key] = {**edge, "evidence_ids": [], "stored_relations": [], "relation_labels": []}
        item = grouped[key]
        item["evidence_ids"].append(link.id)
        if link.relation not in item["stored_relations"]:
            item["stored_relations"].append(link.relation)
        title = RELATION_LABELS.get(edge["relation"], "Relación registrada")
        if title not in item["relation_labels"]:
            item["relation_labels"].append(title)
    for item in grouped.values():
        item["evidence_count"] = len(item["evidence_ids"])
        item["label"] = " · ".join(item["relation_labels"])
    return list(grouped.values())


def _evidence_page(db, request, root, grants, value):
    user, path = request.state.user, value["path"]
    owner = path[-1]
    query = _technical_query(db, user, owner).subquery()
    rows = db.execute(select(query).where(query.c.id > value["after"])
                      .order_by(query.c.id).limit(PAGE_SIZE + 1)).all()
    visible = []
    for link in rows[:PAGE_SIZE]:
        artifact = _node(db, user, "ARTIFACT", link.artifact_id, grants)
        if artifact is not None:
            visible.append({"relationship_id": link.id, "relation": link.relation,
                            "label": EVIDENCE_LABELS.get(link.relation, "Evidencia técnica"),
                            "artifact": artifact})
    return {"projection": "evidence", "root_id": root["id"], "version_id": root["resource_id"],
            "owner_id": owner, "items": visible, "complete": len(rows) <= PAGE_SIZE,
            "next_cursor": _encode(request, root["id"], path, after=rows[PAGE_SIZE - 1].id,
                                   technical=True) if len(rows) > PAGE_SIZE else None,
            "scanned_relationships": min(len(rows), PAGE_SIZE), "limit": PAGE_SIZE,
            "semantics": "METADATA_ONLY_NO_ROW_OR_COLUMN_CORRESPONDENCE"}


def business_graph(db, request, root, grants, max_depth, max_nodes, cursor, evidence):
    user = request.state.user
    value = _decode(db, request, root["id"], grants, cursor, evidence) if cursor else None
    if evidence:
        if value is None:
            raise _invalid()
        return _evidence_page(db, request, root, grants, value)
    path = value["path"] if value else [root["id"]]
    anchor = path[-1]
    anchor_kind, anchor_id = anchor.split(":", 1)
    anchor_node = _node(db, user, anchor_kind, anchor_id, grants)
    nodes, links, paths = {anchor: anchor_node}, {}, {anchor: path}
    queue = deque([(anchor, 0, value["after"] if value else "",
                    value["direction"] if value else "both")])
    visited, pending = set(), {}
    scanned, budget = 0, max_nodes * 8
    while queue and budget > 0:
        key, depth, after, direction = queue.popleft()
        if key in visited:
            continue
        visited.add(key)
        size = min(PAGE_SIZE, budget)
        rows = db.scalars(_adjacency(user, key, direction).where(ArtifactLink.id > after)
                          .order_by(ArtifactLink.id).limit(size + 1)).all()
        budget -= min(len(rows), size)
        scanned += min(len(rows), size)
        last = after
        for link in rows[:size]:
            kind, identity = key.split(":", 1)
            other_kind, other_id = ((link.target_type, link.target_id) if
                (link.source_type, link.source_id) == (kind, identity)
                else (link.source_type, link.source_id))
            other_key = f"{other_kind}:{other_id}"
            other = nodes.get(other_key) or _node(db, user, other_kind, other_id, grants)
            if other is None:
                last = link.id
                continue
            if other_key not in nodes and (depth >= max_depth or len(nodes) >= max_nodes
                                          or len(paths[key]) >= MAX_PATH):
                pending[key] = {"after": last, "direction": direction}
                break
            last = link.id
            nodes[other_key] = other
            links[link.id] = link
            if other_key not in paths:
                paths[other_key] = paths[key] + [other_key]
            if other_key not in visited:
                queue.append((other_key, depth + 1, "", direction))
        else:
            if len(rows) > size:
                pending[key] = {"after": last, "direction": direction}
    for key, _, after, direction in queue:
        if key not in visited:
            pending.setdefault(key, {"after": after, "direction": direction})
    branches = []
    for key, node in nodes.items():
        node["has_more"] = key in pending
        node["technical_groups"] = _technical_groups(db, user, key, grants)
        node["evidence_cursor"] = _encode(request, root["id"], paths[key], technical=True)
        node["expand"] = ({direction: _encode(request, root["id"], paths[key], direction)
                           for direction in ("upstream", "downstream")}
                          if len(paths[key]) < MAX_PATH else None)
        node["expansion_limit"] = len(paths[key]) >= MAX_PATH
    for key, continuation in pending.items():
        if len(paths[key]) >= MAX_PATH:
            continue
        branches.append({"node_id": key, "direction": continuation["direction"],
                         "cursor": _encode(request, root["id"], paths[key], **continuation)})
    return {"projection": "business", "nodes": list(nodes.values()),
            "edges": _business_edges(links, nodes), "root_id": root["id"],
            "anchor_id": anchor, "version_id": root["resource_id"], "complete": not pending,
            "pending_branches": sorted(pending), "branches": branches,
            "limits": {"max_depth": max_depth, "max_nodes": max_nodes,
                       "adjacency_page_size": PAGE_SIZE, "max_path": MAX_PATH},
            "metadata_relationships_scanned": scanned,
            "semantics": "METADATA_ONLY_NO_ROW_OR_COLUMN_CORRESPONDENCE"}
