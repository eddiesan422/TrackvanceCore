"""Complete dataset publications and their event share the caller's transaction."""

from sqlalchemy.orm import Session

from .audit_context import Actor, actor_context
from .models import DatasetVersion, Run


def publish_version_event(db: Session, version: DatasetVersion, actor=None):
    from .events import append_event

    if version.profile_status != "READY" or not version.canonical_artifact_id or not version.sha256:
        raise ValueError("DATASET_PUBLICATION_INCOMPLETE")
    identity = actor if isinstance(actor, Actor) else actor_context.get()
    responsible = identity.id if identity and identity.type == "USER" else None
    if not responsible:
        from .acquisition_models import AcquisitionRun

        metadata = version.ingestion_metadata or {}
        acquisition_id = metadata.get("acquisition_id")
        acquisition = db.get(AcquisitionRun, acquisition_id) if acquisition_id else None
        if (acquisition and acquisition.organization_id == version.organization_id
                and acquisition.dataset_id == version.dataset_id
                and acquisition.source_type == version.source_type
                and acquisition.attempt_id == metadata.get("acquisition_attempt_id")):
            # Publication still records the worker as actor. Its authorized
            # requester is the effective executor of the dependent automation;
            # the consumer revalidates that user's current permissions/content.
            responsible = acquisition.initiated_by_id
    if not responsible and version.source_run_id:
        run = db.get(Run, version.source_run_id)
        if run and run.organization_id == version.organization_id:
            responsible = ((run.execution_plan or {}).get("automation", {}).get("responsible_user_id")
                           or (run.execution_plan or {}).get("intake", {}).get("responsible_user_id")
                           or (run.execution_plan or {}).get("intake_schedule", {}).get("responsible_user_id")
                           or ((run.initiated_by_id) if run.initiated_by_type == "USER" else None))
    return append_event(db, organization_id=version.organization_id,
        dedupe_key=f"dataset-version:{version.id}:published", event_type="DATASET_VERSION_PUBLISHED",
        aggregate_type="DATASET_VERSION", aggregate_id=version.id, module="datasets",
        payload={"dataset_id": version.dataset_id, "dataset_version_id": version.id,
                 "version_id": version.id, "version": version.version,
                 "sha256": version.sha256, "schema_hash": version.schema_hash,
                 "row_count": version.row_count, "responsible_user_id": responsible,
                 "source_run_id": version.source_run_id, "publication_status": "COMPLETE"})
