"""Organization-authorized document search; the browser receives one bounded page.

Document grants are organization scoped in the existing permission model. Dataset
links keep their independent governance check; no extracted values enter this DTO.
"""
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode

from sqlalchemy import JSON, and_, exists, func, or_, select, true, type_coerce
from sqlalchemy.orm import Session, aliased, load_only

from .db import iso
from .document_models import (
    DocumentBatch,
    DocumentBatchMember,
    DocumentExtraction,
    DocumentFamily,
    DocumentOriginal,
    DocumentPublicationClaim,
    DocumentTemplate,
)
from .document_service import owned, permission
from .governance import authorize_dataset
from .models import Dataset, DatasetVersion, User
from .operations_common import OperationError
from .scheduler import aware

DocumentOrder = Literal["received_at", "filename", "family", "status"]
PublicationFilter = Literal["PUBLISHED", "UNPUBLISHED", "BATCHED"]
DocumentStatus = Literal["QUEUED", "RUNNING", "REVIEW_REQUIRED", "REVIEWED", "PUBLISHED", "FAILED", "CANCELLED", "DUPLICATE_FILE"]


def _like(value: str) -> str:
    # User input is literal text, never a wildcard or SQL expression.
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _value_match(db: Session, pattern: str):
    """Search only actual field/cell values, including repeated table rows.

    PostgreSQL and SQLite use their native JSON iterators. The correlated EXISTS
    never duplicates documents and never materializes all extraction payloads.
    """
    fields = func.json_each(DocumentExtraction.payload["fields"]).table_valued("key", "value", joins_implicitly=True).alias("field_values")
    tables = func.json_each(DocumentExtraction.payload["tables"]).table_valued("key", "value", joins_implicitly=True).alias("table_values")
    if db.get_bind().dialect.name == "postgresql":
        rows = func.json_array_elements(tables.c.value).table_valued("value", joins_implicitly=True).alias("table_rows")
    else:
        rows = func.json_each(tables.c.value).table_valued("key", "value", joins_implicitly=True).alias("table_rows")
    cells = func.json_each(rows.c.value).table_valued("key", "value", joins_implicitly=True).alias("cell_values")
    field_match = exists(select(1).select_from(fields).where(
        type_coerce(fields.c.value, JSON)["value"].as_string().ilike(pattern, escape="\\"))).correlate(DocumentExtraction)
    cell_match = exists(select(1).select_from(tables).join(rows, true()).join(cells, true()).where(
        type_coerce(cells.c.value, JSON)["value"].as_string().ilike(pattern, escape="\\"))).correlate(DocumentExtraction)
    return or_(field_match, cell_match)


def _outputs(db: Session, user: User, batch: DocumentBatch | None) -> list[dict]:
    result: list[dict] = []
    if batch is None:
        return result
    for structure, identity in batch.published_versions.items():
        version = db.get(DatasetVersion, identity)
        if version is None or version.organization_id != user.organization_id:
            continue
        dataset = db.get(Dataset, version.dataset_id)
        if dataset is None or dataset.organization_id != user.organization_id:
            continue
        try:
            authorize_dataset(db, user, dataset.id, "METADATA")
        except OperationError:
            continue
        result.append({"structure": structure, "dataset_id": dataset.id, "dataset_name": dataset.name,
                       "version_id": version.id, "version": version.version,
                       "href": f"/datasets/{dataset.id}?" + urlencode({"version": version.id})})
    return result


def list_originals(db: Session, user: User, *, family_id: str | None = None, search: str = "",
                   status: str | None = None, received_from: datetime | None = None,
                   received_to: datetime | None = None, mode: str | None = None,
                   publication: PublicationFilter | None = None, order_by: DocumentOrder = "received_at",
                   direction: Literal["asc", "desc"] = "desc", offset: int = 0, limit: int = 50,
                   purpose: Literal["OPERATIONAL", "LEGACY_REFERENCE"] | None = None):
    permission(db, user, "read")
    if not 0 <= offset or not 1 <= limit <= 100:
        raise OperationError(422, "DOCUMENT_PAGE_LIMIT", "Selecciona una página de 1 a 100 documentos.")
    if received_from:
        received_from = aware(received_from)
    if received_to:
        received_to = aware(received_to)
    if received_from and received_to and received_from > received_to:
        raise OperationError(422, "DOCUMENT_RECEIVED_RANGE", "La recepción inicial debe preceder a la final.")
    if family_id:
        owned(db, DocumentFamily, family_id, user)
    # All joins are tenant scoped BEFORE search, total, sorting and pagination.
    # Extracted values remain raiseload-protected and never enter the bounded
    # page query, count or list DTO.
    query = select(DocumentOriginal, DocumentFamily, DocumentTemplate, DocumentExtraction).options(
        load_only(DocumentExtraction.id, DocumentExtraction.revision, DocumentExtraction.created_at,
                  DocumentExtraction.reviewed, raiseload=True)).join(
        DocumentFamily, and_(DocumentOriginal.family_id == DocumentFamily.id,
                             DocumentFamily.organization_id == user.organization_id)).join(
        DocumentTemplate, and_(DocumentOriginal.template_id == DocumentTemplate.id,
                               DocumentTemplate.family_id == DocumentFamily.id,
                               DocumentTemplate.organization_id == user.organization_id)).outerjoin(
        DocumentExtraction, and_(DocumentOriginal.extraction_id == DocumentExtraction.id,
                                 DocumentExtraction.document_id == DocumentOriginal.id,
                                 DocumentExtraction.organization_id == user.organization_id)).where(
        DocumentOriginal.organization_id == user.organization_id)
    published = exists(select(1).select_from(DocumentPublicationClaim).join(DocumentBatch,
        and_(DocumentBatch.id == DocumentPublicationClaim.batch_id,
             DocumentBatch.family_id == DocumentOriginal.family_id,
             DocumentBatch.organization_id == user.organization_id,
             DocumentBatch.status == "PUBLISHED")).where(
        DocumentPublicationClaim.document_id == DocumentOriginal.id,
        DocumentPublicationClaim.organization_id == user.organization_id)).correlate(DocumentOriginal)
    if family_id:
        query = query.where(DocumentOriginal.family_id == family_id)
    if purpose:
        query = query.where(DocumentOriginal.is_example.is_(purpose == "LEGACY_REFERENCE"))
    if search.strip():
        pattern = _like(search.strip())
        query = query.where(or_(DocumentOriginal.filename.ilike(pattern, escape="\\"),
            DocumentOriginal.id.ilike(pattern, escape="\\"), DocumentFamily.name.ilike(pattern, escape="\\"),
            DocumentExtraction.id.ilike(pattern, escape="\\"), _value_match(db, pattern)))
    if status:
        query = query.where(DocumentOriginal.status == status)
    if received_from:
        query = query.where(DocumentOriginal.received_at >= received_from)
    if received_to:
        query = query.where(DocumentOriginal.received_at < received_to)
    if mode:
        query = query.where(DocumentTemplate.definition["publication"]["mode"].as_string() == mode)
    if publication == "PUBLISHED":
        query = query.where(published)
    elif publication == "UNPUBLISHED":
        query = query.where(~published)
    elif publication == "BATCHED":
        query = query.where(exists(select(1).where(DocumentBatchMember.document_id == DocumentOriginal.id,
            DocumentBatchMember.organization_id == user.organization_id)).correlate(DocumentOriginal))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    columns = {"received_at": DocumentOriginal.received_at, "filename": DocumentOriginal.filename,
               "family": DocumentFamily.name, "status": DocumentOriginal.status}
    column = columns[order_by]
    order = column.asc() if direction == "asc" else column.desc()
    # A stable unique tie breaker prevents duplicate/missing rows across pages.
    page = db.execute(query.order_by(order, DocumentOriginal.id.asc()).offset(offset).limit(limit)).all()
    ids = [row[0].id for row in page]
    # One bounded projection for this authorized page reads only fixed worker
    # completion scalars, never complete engine/payload JSON or document values.
    extraction_ids = [row[3].id for row in page if row[3] is not None]
    completions = {}
    if extraction_ids:
        completion_query = select(DocumentExtraction.id,
            DocumentExtraction.engine["inference_completion"]["status"].as_string(),
            DocumentExtraction.engine["inference_completion"]["failed_stage"].as_string(),
            DocumentExtraction.engine["inference_completion"]["error_code"].as_string(),
            DocumentExtraction.engine["inference_completion_sha256"].as_string(),
            DocumentExtraction.payload["inference_completion_resolution"]["marker_sha256"].as_string()).where(
                DocumentExtraction.id.in_(extraction_ids), DocumentExtraction.organization_id == user.organization_id)
        completions = {row[0]: tuple(row[1:]) for row in db.execute(completion_query)}
    batch_info: dict[str, DocumentBatch] = {}
    publication_info: dict[str, DocumentBatch] = {}
    # Window functions select one latest link per page document in SQL, keeping
    # correction history and batch membership from multiplying list rows.
    for link, target in ((DocumentBatchMember, batch_info), (DocumentPublicationClaim, publication_info)):
        if not ids:
            break
        ranked_query = select(link.document_id.label("document_id"), DocumentBatch.id.label("batch_id"),
            func.row_number().over(partition_by=link.document_id,
                order_by=(DocumentBatch.created_at.desc(), DocumentBatch.id.desc())).label("position")).join(
            DocumentBatch, and_(DocumentBatch.id == link.batch_id,
                DocumentBatch.organization_id == user.organization_id)).join(DocumentOriginal,
                and_(DocumentOriginal.id == link.document_id,
                    DocumentOriginal.family_id == DocumentBatch.family_id,
                    DocumentOriginal.organization_id == user.organization_id)).where(
            link.document_id.in_(ids), link.organization_id == user.organization_id)
        if link is DocumentPublicationClaim:
            # A claim can reserve a SEALED extraction before durable publication.
            ranked_query = ranked_query.where(DocumentBatch.status == "PUBLISHED")
        ranked = ranked_query.subquery()
        batch_alias = aliased(DocumentBatch)
        for document_id, batch in db.execute(select(ranked.c.document_id, batch_alias).join(
                batch_alias, batch_alias.id == ranked.c.batch_id).where(ranked.c.position == 1)):
            target[document_id] = batch
    items = []
    output_cache: dict[str, list[dict]] = {}
    for original, family, template, extraction in page:
        completion_status, completion_stage, completion_error, completion_sha256, resolution_sha256 = completions.get(
            extraction.id if extraction else None, (None, None, None, None, None))
        href = f"/documents/families/{family.id}?" + urlencode({"document": original.id})
        batch = batch_info.get(original.id)
        published_batch = publication_info.get(original.id)
        if published_batch and published_batch.id not in output_cache:
            output_cache[published_batch.id] = _outputs(db, user, published_batch)
        items.append({"id": original.id, "family_id": family.id, "template_id": template.id,
            "filename": original.filename, "sha256": original.sha256, "received_at": iso(original.received_at),
            "status": original.status, "progress": original.progress, "error_code": original.error_code,
            "duplicate_of_id": original.duplicate_of_id, "correction_of_id": original.correction_of_id,
            "is_example": original.is_example, "extraction_id": original.extraction_id,
            "started_at": iso(original.started_at), "finished_at": iso(original.finished_at),
            "engine_snapshot": ({**original.engine_snapshot, "snapshot_sha256": original.engine_snapshot_hash}
                if original.engine_snapshot else None),
            "phase": (original.processing_phase if original.status == "RUNNING" and original.processing_phase else
                {"PUBLISHED": "COMPLETE", "AUTO_VERIFIED": "PUBLICATION", "REVIEWED": "PUBLICATION"}.get(original.status, original.status)),
            **({"incomplete_inference": {"status": "HUMAN_RESOLVED" if extraction.reviewed and completion_sha256
                    and resolution_sha256 == completion_sha256 else "PARTIAL",
                "failed_stage": completion_stage, "error_code": completion_error,
                "automatic_approval_blocked": True, "requires_full_source_review": not (extraction.reviewed
                    and completion_sha256 and resolution_sha256 == completion_sha256), "can_confirm": False}}
                if completion_status == "PARTIAL" and extraction else {}),
            "family_name": family.name, "mode": template.definition["publication"]["mode"],
            "extraction": {"id": extraction.id, "revision": extraction.revision,
                "created_at": iso(extraction.created_at), "reviewed": extraction.reviewed} if extraction else None,
            "publication": "PUBLISHED" if published_batch else "UNPUBLISHED",
            "batch": {"id": batch.id, "status": batch.status,
                "href": f"/documents/families/{family.id}?" + urlencode({"batch": batch.id})} if batch else None,
            "outputs": output_cache.get(published_batch.id, []) if published_batch else [],
            "links": {"family": f"/documents/families/{family.id}", "document": href,
                "extraction": href, "review": href, "original": f"/api/v1/documents/originals/{original.id}/file"}})
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "order_by": order_by, "direction": direction}
