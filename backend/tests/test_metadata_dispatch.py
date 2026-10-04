"""C05 dispatch architecture and real storage failures before remote STARTED."""
# Pytest injects imported fixtures by their unaliased names.
# ruff: noqa: F811

from copy import deepcopy
from datetime import timedelta

import polars as pl
import pytest
from sqlalchemy import func, select
from test_automation_events import (  # noqa: F401 -- reusable isolated pytest fixtures
    automation_case,
    make_automation,
)
from test_delivery_service_api import (  # noqa: F401 -- reusable isolated pytest fixtures
    delivery_case,
    delivery_runtime,
    publish_configuration,
    queue_run,
)
from test_scheduler import make_monitor

from trackvance import artifactstore, dataset_scans, delivery_service, events
from trackvance.artifactstore import storage_provider
from trackvance.automation import dispatch_due, dispatch_occurrence, revision_for
from trackvance.automation_models import DeliveryOccurrence
from trackvance.dataset_scans import publish_materialized_version
from trackvance.db import utcnow
from trackvance.delivery_streams import DatasetRecords
from trackvance.manifests import configuration_hash
from trackvance.models import (
    Artifact,
    Configuration,
    Dataset,
    DatasetVersion,
    DeliveryAttempt,
    DeliveryDestinationVersion,
    Job,
    MonitorOccurrence,
    Run,
)
from trackvance.planner import WorkloadInput, WorkloadMetadataError
from trackvance.scheduler import dispatch_due as dispatch_sentinel
from trackvance.scheduler import save_schedule
from trackvance.services import enqueue, execute_run


def forbid_dispatch_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Dispatch attempted population I/O or opened a DataSink")

    for name in ("dataset_paths", "dataset_descriptor", "open_read", "materialize", "materialize_reference"):
        monkeypatch.setattr(storage_provider, name, forbidden)
    monkeypatch.setattr(artifactstore, "file_hash", forbidden)
    monkeypatch.setattr(dataset_scans, "iter_version_batches", forbidden)
    monkeypatch.setattr(DatasetRecords, "__init__", forbidden)
    monkeypatch.setattr(DatasetRecords, "__iter__", forbidden)
    for name in ("scan_parquet", "read_parquet", "read_parquet_schema"):
        monkeypatch.setattr(pl, name, forbidden)
    monkeypatch.setattr(delivery_service.sink_registry, "create", forbidden)


@pytest.mark.parametrize("origin", ["SCHEDULED", "MANUAL"])
def test_automation_dispatch_uses_only_metadata(database, automation_case, monkeypatch, origin):
    now = utcnow()
    with database() as db:
        automation = make_automation(db, automation_case, now=now)
        db.commit()
        forbid_dispatch_io(monkeypatch)
        if origin == "SCHEDULED":
            assert dispatch_due(db, now) == 1
            occurrence = db.scalar(select(DeliveryOccurrence))
        else:
            occurrence = dispatch_occurrence(db, automation, revision_for(db, automation),
                                             "manual-request", now, origin=origin)
        assert occurrence.status == "ENQUEUED"
        run = db.get(Run, occurrence.run_id)
        assert run.dataset_version_id == automation_case["source_id"]
        assert run.execution_plan["dispatch_snapshot_version"] == 1
        assert run.execution_plan["destination_config_hash"]
        assert run.execution_plan["source_identity"]["row_count"] == 2
        db.commit()


def test_chaining_consumer_dispatch_uses_only_metadata(database, automation_case, monkeypatch):
    with database() as db:
        source = db.get(DatasetVersion, automation_case["source_id"])
        intake = Configuration(name="C05 accepted source", module="intake", dataset_id=source.dataset_id,
                               config={"required_columns": ["tenant_id"]})
        db.add(intake)
        db.flush()
        automation = make_automation(db, automation_case, now=utcnow() - timedelta(seconds=1),
                                     mode="CHAINED", source_policy="INTAKE_OUTPUT",
                                     intake_configuration_id=intake.id)
        db.commit()
        source_run = enqueue(db, intake, source, None, "Test User")
        db.commit()
        execute_run(db, source_run)
        db.commit()
        assert source_run.decision == "APPROVED"
        output_id, source_run_id, automation_id = source_run.output_version_id, source_run.id, automation.id
    forbid_dispatch_io(monkeypatch)
    assert events.consume_once("CHAINING")
    with database() as db:
        occurrence = db.scalar(select(DeliveryOccurrence).where(DeliveryOccurrence.automation_id == automation_id))
        run = db.get(Run, occurrence.run_id)
        assert occurrence.status == "ENQUEUED"
        assert run.dataset_version_id == output_id
        assert run.execution_plan["automation"]["source_run_id"] == source_run_id
        assert run.execution_plan["source_identity"]["source_run_id"] == source_run_id


def test_manual_delivery_dispatch_uses_only_metadata(authenticated, database, delivery_case, monkeypatch):
    config = publish_configuration(authenticated, delivery_case)
    forbid_dispatch_io(monkeypatch)
    queued = queue_run(authenticated, config["id"], delivery_case.source["version_id"], "c05-metadata-manual")
    with database() as db:
        run = db.get(Run, queued["id"])
        assert run.status == "QUEUED"
        assert run.execution_plan["source_identity"]["dataset_version_id"] == delivery_case.source["version_id"]


def multipart_version(db, source):
    canonical = db.get(Artifact, source.canonical_artifact_id)
    path = storage_provider.materialize(canonical)
    return publish_materialized_version(db, db.get(Dataset, source.dataset_id), [path],
                                        filename="c05-multipart.parquet", source_type="UPLOAD")


@pytest.mark.parametrize("missing_size", [False, True])
def test_sentinel_multipart_dispatch_never_opens_descriptor(database, tmp_path, delivery_runtime, monkeypatch, missing_size):
    now = utcnow()
    with database() as db:
        user, config, source = make_monitor(db, tmp_path)
        source = multipart_version(db, source)
        population_bytes = source.ingestion_metadata["canonical_size_bytes"]
        if missing_size:
            source.ingestion_metadata = {}
        save_schedule(db, config, user, interval_seconds=60, enabled=True, starts_at=now, expected_version=None)
        db.commit()
        forbid_dispatch_io(monkeypatch)
        if missing_size:
            with pytest.raises(WorkloadMetadataError, match="WORKLOAD_METADATA_UNAVAILABLE"):
                WorkloadInput.from_metadata(db, source)
        else:
            assert WorkloadInput.from_metadata(db, source).size_bytes == population_bytes
        assert dispatch_sentinel(db, now) == 1
        occurrence = db.scalar(select(MonitorOccurrence))
        run = db.get(Run, occurrence.run_id)
        assert run.dataset_version_id == source.id
        if missing_size:
            assert occurrence.status == "FAILED_PRECONDITION"
            assert occurrence.reason_code == run.error == "WORKLOAD_METADATA_UNAVAILABLE"
            assert run.execution_plan["estimated_input_bytes"] is None
            assert db.scalar(select(Job).where(Job.run_id == run.id)).status == "FAILED"
        else:
            assert occurrence.status == "ENQUEUED"
            assert run.execution_plan["estimated_input_bytes"] == population_bytes
        db.commit()


def queued_delivery(db, case, *, multipart=False):
    source = db.get(DatasetVersion, case.source["version_id"])
    if multipart:
        source = multipart_version(db, source)
    draft = {**case.draft, "dataset_version_id": source.id}
    config = Configuration(name="C05 immutable dispatch", module="DELIVERY", dataset_id=source.dataset_id,
                           config=delivery_service.DeliveryDraft.model_validate(draft).snapshot())
    db.add(config)
    db.flush()
    from trackvance.models import User

    run = delivery_service.enqueue_delivery(db, config, source, db.get(User, "test-user"))
    db.commit()
    return run


@pytest.mark.parametrize("multipart,damage", [(False, "missing"), (False, "corrupt"),
                                             (True, "descriptor"), (True, "part"), (True, "missing-part")])
def test_storage_damage_after_dispatch_fails_before_started(database, delivery_case, multipart, damage):
    with database() as db:
        run = queued_delivery(db, delivery_case, multipart=multipart)
        source = db.get(DatasetVersion, run.dataset_version_id)
        canonical = db.get(Artifact, source.canonical_artifact_id)
        path = storage_provider.materialize(canonical)
        if damage in {"part", "missing-part"}:
            path = storage_provider.dataset_paths(canonical)[0]
        if damage in {"missing", "missing-part"}:
            path.unlink()
        else:
            path.write_bytes(b"corrupt after dispatch")
        delivery_case.runtime.calls.clear()
        delivery_service.execute_delivery_run(db, run)
        assert run.status == "FAILED_PRECONDITION"
        assert run.started_at is None
        assert db.scalar(select(func.count()).select_from(DeliveryAttempt)) == 0
        assert delivery_case.runtime.calls == []


@pytest.mark.parametrize("field", ["canonical_hash", "row_count", "schema", "schema_json", "config", "destination"])
def test_registered_snapshot_drift_is_never_adopted(database, delivery_case, field):
    with database() as db:
        run = queued_delivery(db, delivery_case)
        original_identity = deepcopy(run.execution_plan["source_identity"])
        source = db.get(DatasetVersion, run.dataset_version_id)
        if field == "canonical_hash":
            db.get(Artifact, source.canonical_artifact_id).sha256 = "a" * 64
        elif field == "row_count":
            source.row_count += 1
        elif field == "schema":
            source.schema_hash = "b" * 64
        elif field == "schema_json":
            source.schema_json = [{**column, "logical_type": "STRING"} for column in source.schema_json]
        elif field == "config":
            config = db.get(Configuration, run.config_id)
            config.config = {**config.config, "write_strategy": "OVERWRITE"}
        else:
            destination = db.get(DeliveryDestinationVersion, run.execution_plan["destination_version_id"])
            destination.config = {**destination.config, "host": "rewritten.example.test"}
            destination.config_hash = configuration_hash({**destination.config, "credential_revision": destination.secret_reference})
        db.commit()
        delivery_case.runtime.calls.clear()
        delivery_service.execute_delivery_run(db, run)
        assert run.status == "FAILED_PRECONDITION"
        assert run.execution_plan["source_identity"] == original_identity
        assert run.started_at is None
        assert db.scalar(select(func.count()).select_from(DeliveryAttempt)) == 0
        assert delivery_case.runtime.calls == []


def test_worker_verifies_and_scans_outside_metadata_transactions(database, delivery_case, monkeypatch):
    with database() as db:
        run = queued_delivery(db, delivery_case)
        verification_transactions, scan_transactions = [], []
        real_paths, real_iter = storage_provider.dataset_paths, DatasetRecords.__iter__

        def paths(artifact):
            verification_transactions.append(db.in_transaction())
            return real_paths(artifact)

        def records(frame):
            scan_transactions.append(db.in_transaction())
            yield from real_iter(frame)

        monkeypatch.setattr(storage_provider, "dataset_paths", paths)
        monkeypatch.setattr(DatasetRecords, "__iter__", records)
        delivery_service.execute_delivery_run(db, run)
        assert run.status == "SUCCESS"
        assert len(verification_transactions) == 3 and not any(verification_transactions)
        assert scan_transactions and not any(scan_transactions)
        assert db.scalar(select(DeliveryAttempt)).status == "COMMITTED"


def test_modified_source_after_preparation_is_reverified_before_started(database, delivery_case, monkeypatch):
    from trackvance.data_sinks import DatabaseDataSink

    with database() as db:
        run = queued_delivery(db, delivery_case)
        source = db.get(DatasetVersion, run.dataset_version_id)
        canonical = db.get(Artifact, source.canonical_artifact_id)
        path = storage_provider.materialize(canonical)
        real_prepare = DatabaseDataSink.prepare_batched

        def prepare(sink, *args, **kwargs):
            prepared = real_prepare(sink, *args, **kwargs)
            if (kwargs.get("binding") or {}).get("run_id"):
                path.write_bytes(b"changed while preparing")
            return prepared

        monkeypatch.setattr(DatabaseDataSink, "prepare_batched", prepare)
        delivery_case.runtime.calls.clear()
        delivery_service.execute_delivery_run(db, run)
        assert run.status == "FAILED_PRECONDITION" and run.started_at is None
        assert db.scalar(select(func.count()).select_from(DeliveryAttempt)) == 0
        assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)


def test_metadata_drift_during_preparation_is_rechecked_under_start_fence(database, delivery_case, monkeypatch):
    from trackvance.data_sinks import DatabaseDataSink

    with database() as db:
        run = queued_delivery(db, delivery_case)
        frozen = deepcopy(run.execution_plan["source_identity"])
        real_prepare = DatabaseDataSink.prepare_batched

        def prepare(sink, *args, **kwargs):
            prepared = real_prepare(sink, *args, **kwargs)
            if (kwargs.get("binding") or {}).get("run_id"):
                assert not db.in_transaction()
                with database() as concurrent:
                    source = concurrent.get(DatasetVersion, run.dataset_version_id)
                    source.row_count += 1
                    concurrent.commit()
            return prepared

        monkeypatch.setattr(DatabaseDataSink, "prepare_batched", prepare)
        delivery_case.runtime.calls.clear()
        delivery_service.execute_delivery_run(db, run)
        assert run.status == "FAILED_PRECONDITION" and run.started_at is None
        assert run.execution_plan["source_identity"] == frozen
        assert db.scalar(select(func.count()).select_from(DeliveryAttempt)) == 0
        assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)


def test_hash_phase_allows_independent_lease_heartbeat(database, delivery_case, monkeypatch):
    from sqlalchemy import update

    with database() as db:
        run = queued_delivery(db, delivery_case)
        job = db.scalar(select(Job).where(Job.run_id == run.id))
        job.status, job.lease_owner = "RUNNING", "c05-heartbeat-owner"
        job.lease_until = utcnow() + timedelta(minutes=5)
        job.attempts = 1
        db.commit()
        real_paths = storage_provider.dataset_paths
        renewals = []

        def paths(artifact):
            assert not db.in_transaction()
            with database() as heartbeat:
                changed = heartbeat.execute(update(Job).where(
                    Job.id == job.id, Job.status == "RUNNING", Job.lease_owner == job.lease_owner,
                ).values(lease_until=utcnow() + timedelta(minutes=5)))
                heartbeat.commit()
                renewals.append(changed.rowcount)
            return real_paths(artifact)

        monkeypatch.setattr(storage_provider, "dataset_paths", paths)
        delivery_service.execute_delivery_run(db, run, lease_owner=job.lease_owner)
        assert run.status == "SUCCESS"
        assert renewals == [1, 1, 1]
        assert db.scalar(select(DeliveryAttempt)).status == "COMMITTED"


def test_pre_c05_queued_identity_remains_verifiable_without_rewriting_history(database, delivery_case):
    with database() as db:
        run = queued_delivery(db, delivery_case)
        plan = deepcopy(run.execution_plan)
        plan.pop("dispatch_snapshot_version")
        plan.pop("destination_config_hash")
        keys = {"dataset_version_id", "source_sha256", "schema_hash", "canonical_artifact_id", "canonical_sha256"}
        plan["source_identity"] = {key: value for key, value in plan["source_identity"].items() if key in keys}
        run.execution_plan = plan
        db.commit()
        delivery_service.execute_delivery_run(db, run)
        assert run.status == "SUCCESS"
        assert all(run.execution_plan[key] == value for key, value in plan.items())
        assert "dispatch_snapshot_version" not in run.execution_plan
