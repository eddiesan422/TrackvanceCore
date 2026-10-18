"""Bounded canonical identities over recorded metadata relationships.

The same projection serves individual identities and SQL group pagination.
Dataset history and arbitrary tracking strings never create process edges.
"""
from sqlalchemy import Text, and_, bindparam, case, cast, func, literal, or_, select, union_all

from .acquisition_models import AcquisitionRun
from .acquisition_schedule_models import AcquisitionOccurrence, AcquisitionSourceConfiguration
from .automation_models import DeliveryOccurrence, OutboxEvent
from .document_models import (
    DocumentBatch,
    DocumentBatchMember,
    DocumentOriginal,
    DocumentPublicationClaim,
)
from .intake_automation_models import IntakeOccurrence
from .models import DatasetVersion, ExceptionCase, Run


def _parents(organization_id):
    """One deterministic parent per native identity, evaluated inside the DB."""
    acquisition = AcquisitionRun.__table__
    occurrence = AcquisitionOccurrence.__table__
    version = DatasetVersion.__table__
    run = Run.__table__
    document = DocumentOriginal.__table__
    claim = DocumentPublicationClaim.__table__
    member = DocumentBatchMember.__table__
    delivery = DeliveryOccurrence.__table__
    intake = IntakeOccurrence.__table__
    event = OutboxEvent.__table__
    exception = ExceptionCase.__table__

    acquired_parent = select(func.min(occurrence.c.id)).where(
        occurrence.c.organization_id == organization_id, occurrence.c.acquisition_id == acquisition.c.id).scalar_subquery()
    acquired = select(literal("acquisition").label("kind"), acquisition.c.id.label("identity"),
        literal("occurrence").label("parent_kind"), acquired_parent.label("parent_id")).where(
            acquisition.c.organization_id == organization_id, acquired_parent.is_not(None))

    publication = version.c.ingestion_metadata["document_publication"]["batch_id"].as_string()
    produced_kind = case((acquisition.c.id.is_not(None), "acquisition"),
        (and_(publication.is_not(None), publication != ""), "document_batch"),
        (version.c.source_run_id.is_not(None), "run"))
    produced_id = case((acquisition.c.id.is_not(None), acquisition.c.id),
        (and_(publication.is_not(None), publication != ""), publication), else_=version.c.source_run_id)
    produced = select(literal("version"), version.c.id, produced_kind, produced_id).select_from(
        version.outerjoin(acquisition, and_(acquisition.c.organization_id == organization_id,
            acquisition.c.output_version_id == version.c.id))).where(
                version.c.organization_id == organization_id, produced_id.is_not(None))

    plan = run.c.execution_plan
    chained = and_(delivery.c.origin == "CHAINED", delivery.c.source_run_id.is_not(None))
    intake_event = and_(intake.c.id.is_not(None), plan["intake_schedule"]["origin"].as_string() == "EVENT",
        intake.c.dataset_version_id == run.c.dataset_version_id,
        intake.c.trigger_key == literal("version:") + run.c.dataset_version_id)
    continued_kind = case((delivery.c.id.is_not(None), case((chained, "run"))),
        (intake_event, "version"), (event.c.id.is_not(None), "version"))
    continued_id = case((delivery.c.id.is_not(None), case((chained, delivery.c.source_run_id))),
        (intake_event, run.c.dataset_version_id), (event.c.id.is_not(None), run.c.dataset_version_id))
    continued = select(literal("run"), run.c.id, continued_kind, continued_id).select_from(
        run.outerjoin(delivery, and_(delivery.c.organization_id == organization_id,
            delivery.c.run_id == run.c.id, delivery.c.id == plan["automation"]["occurrence_id"].as_string()))
        .outerjoin(intake, and_(intake.c.organization_id == organization_id,
            intake.c.run_id == run.c.id, intake.c.id == plan["intake_schedule"]["occurrence_id"].as_string()))
        .outerjoin(event, and_(event.c.organization_id == organization_id,
            event.c.id == plan["sentinel_automatic"]["event_id"].as_string(),
            event.c.event_type == "DATASET_VERSION_PUBLISHED", event.c.aggregate_type == "DATASET_VERSION",
            event.c.aggregate_id == run.c.dataset_version_id))).where(
                run.c.organization_id == organization_id, continued_id.is_not(None))

    claims = and_(claim.c.organization_id == organization_id, claim.c.document_id == document.c.id)
    members = and_(member.c.organization_id == organization_id, member.c.document_id == document.c.id)
    claim_count = select(func.count(func.distinct(claim.c.batch_id))).where(claims).scalar_subquery()
    member_count = select(func.count(func.distinct(member.c.batch_id))).where(members).scalar_subquery()
    batch_id = case((claim_count == 1, select(func.min(claim.c.batch_id)).where(claims).scalar_subquery()),
        (and_(claim_count == 0, member_count == 1), select(func.min(member.c.batch_id)).where(members).scalar_subquery()))
    batched = select(literal("document"), document.c.id, literal("document_batch"), batch_id).where(
        document.c.organization_id == organization_id, batch_id.is_not(None))
    cases = select(literal("exception"), exception.c.id, literal("run"), exception.c.run_id).where(
        exception.c.organization_id == organization_id)
    # PostgreSQL can push the recursive identity equality through this projection;
    # no metadata population is materialized or transferred into Python.
    return union_all(acquired, produced, continued, batched, cases).cte("operation_parent_edges").prefix_with(
        "NOT MATERIALIZED", dialect="postgresql")


def process_roots(seeds, organization_id):
    """Resolve SQL seeds with the identical sixteen-edge and cycle bound."""
    native = seeds.subquery("operation_process_seeds")
    kind, identity = cast(native.c.kind, Text), cast(native.c.identity, Text)
    path = cast(literal("|") + kind + ":" + identity + "|", Text)
    walk = select(kind.label("origin_kind"), identity.label("origin_id"), kind.label("node_kind"),
        identity.label("node_id"), literal(0).label("depth"), path.label("path")).cte("operation_process_walk", recursive=True)
    edges = _parents(organization_id)
    parent_key = literal("|") + edges.c.parent_kind + ":" + edges.c.parent_id + "|"
    walk = walk.union_all(select(walk.c.origin_kind, walk.c.origin_id, cast(edges.c.parent_kind, Text),
        cast(edges.c.parent_id, Text), walk.c.depth + 1, cast(walk.c.path + parent_key, Text)).join(
            edges, and_(edges.c.kind == walk.c.node_kind, edges.c.identity == walk.c.node_id)).where(
                walk.c.depth < 16, ~walk.c.path.contains(parent_key)))
    ranked = select(walk, func.row_number().over(partition_by=(walk.c.origin_kind, walk.c.origin_id),
        order_by=walk.c.depth.desc()).label("depth_rank")).subquery("operation_deepest_node")
    remaining = edges.alias("operation_remaining_parent")
    # A cycle or overlong path remains its own process; never emit a partial root.
    return (select(ranked.c.origin_kind.label("kind"), ranked.c.origin_id.label("identity"),
        case((remaining.c.parent_id.is_not(None), ranked.c.origin_kind), else_=ranked.c.node_kind).label("root_kind"),
        case((remaining.c.parent_id.is_not(None), ranked.c.origin_id), else_=ranked.c.node_id).label("root_id"))
        .outerjoin(remaining, and_(remaining.c.kind == ranked.c.node_kind, remaining.c.identity == ranked.c.node_id))
        .where(ranked.c.depth_rank == 1))


def readable_root_key(roots, grants, organization_id):
    """Keep an unavailable ancestor from absorbing independently readable stages.

    This checks native resource grants/existence only. Normal per-item content
    governance still runs before serialization; no global count is exposed.
    """
    clauses = []
    modules = [value for value in ("intake", "recon", "sentinel", "DELIVERY") if f"{value.lower()}:read" in grants]
    if "delivery:read" in grants:
        modules.append("DELIVERY_PREFLIGHT")
    if modules:
        clauses.append(and_(roots.c.root_kind == "run", select(Run.id).where(
            Run.organization_id == organization_id, Run.id == roots.c.root_id, Run.module.in_(modules)).exists()))
    if "datasets:read" in grants:
        for kind, model in (("acquisition", AcquisitionRun), ("occurrence", AcquisitionOccurrence),
                ("acquisition_schedule", AcquisitionSourceConfiguration)):
            clauses.append(and_(roots.c.root_kind == kind, select(model.id).where(
                model.organization_id == organization_id, model.id == roots.c.root_id).exists()))
    if "documents:read" in grants:
        for document_kind, document_model in (("document", DocumentOriginal), ("document_batch", DocumentBatch)):
            clauses.append(and_(roots.c.root_kind == document_kind, select(document_model.id).where(
                document_model.organization_id == organization_id, document_model.id == roots.c.root_id).exists()))
    return case((or_(*clauses) if clauses else literal(False), roots.c.root_kind + ":" + roots.c.root_id),
        else_=roots.c.kind + ":" + roots.c.identity)


def process_identity(db, organization_id, kind, identity):
    # A constant native identity lets PostgreSQL discard every unrelated edge
    # branch and use its primary key. A recursive CTE for one notification
    # instead expanded the entire edge relation and repeatedly compiled JIT.
    # Both traversals still consume this one canonical SQL edge definition.
    db.flush()
    original = f"{kind}:{identity}"
    cache = db.info.get("operation_process_identities")
    if cache is not None and (organization_id, kind, identity) in cache:
        return cache[(organization_id, kind, identity)]
    queries = db.info.setdefault("operation_parent_queries", {})
    if organization_id not in queries:
        edges = _parents(organization_id)
        queries[organization_id] = select(edges.c.parent_kind, edges.c.parent_id).where(
            edges.c.kind == bindparam("operation_kind"), edges.c.identity == bindparam("operation_identity")).limit(2)
    seen = set()
    for depth in range(17):
        key = (organization_id, kind, identity)
        if key in seen:
            return original
        seen.add(key)
        parents = db.execute(queries[organization_id], {"operation_kind": kind, "operation_identity": identity}).all()
        if not parents:
            result = f"{kind}:{identity}"
            if cache is not None:
                cache.update({node: result for node in seen})
            return result
        if len(parents) != 1 or depth == 16:
            return original
        kind, identity = parents[0]
    return original


def notification_process(db, item):
    kinds = {"ACQUISITION": "acquisition", "RUN": "run", "ACQUISITION_OCCURRENCE": "occurrence"}
    kind = kinds.get(item.resource_type)
    return process_identity(db, item.organization_id, kind, item.resource_id) if kind else f"{item.resource_type.lower()}:{item.resource_id}"
