import importlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from trackvance import worker as worker_module
from trackvance.credential_store import (
    EncryptedFileSecretStore,
    SecretStore,
    SecretStoreError,
)
from trackvance.db import utcnow
from trackvance.jobqueue import DatabaseJobQueue, JobQueue
from trackvance.models import (
    Configuration,
    Dataset,
    DeliveryAttempt,
    DeliveryDestination,
    DeliveryDestinationVersion,
    Job,
    Run,
)
from trackvance.services import create_version
from trackvance.worker import configured_lane, heartbeat_path, process_once


def delivery_graph(db, tmp_path, suffix: str):
    source = tmp_path / f"delivery-{suffix}.csv"
    source.write_text("customer_id,amount\n001,10.25\n", encoding="utf-8")
    dataset = Dataset(name=f"Delivery dataset {suffix}")
    db.add(dataset)
    db.flush()
    dataset_version = create_version(db, dataset, source, source.name)
    configuration = Configuration(
        name=f"Delivery configuration {suffix}",
        module="delivery",
        dataset_id=dataset.id,
        config={"schema_version": 1, "dataset_version_id": dataset_version.id},
    )
    destination = DeliveryDestination(
        name=f"Destination {suffix}",
        sink_type="POSTGRESQL",
    )
    db.add_all([configuration, destination])
    db.flush()
    destination_version = DeliveryDestinationVersion(
        organization_id=destination.organization_id,
        destination_id=destination.id,
        version=1,
        config={
            "host": "destination.example.test",
            "port": 5432,
            "database": "warehouse",
            "username": "writer",
            "options": {"sslmode": "require"},
        },
        secret_reference="local:" + "a" * 32,
        config_hash="b" * 64,
    )
    db.add(destination_version)
    db.flush()
    run = Run(
        organization_id=dataset.organization_id,
        module="delivery",
        name=configuration.name,
        config_id=configuration.id,
        dataset_version_id=dataset_version.id,
        initiated_by="Test User",
        initiated_by_type="USER",
        initiated_by_id="test-user",
    )
    db.add(run)
    db.flush()
    return destination, destination_version, run


def test_delivery_models_persist_immutable_revision_and_attempt(database, tmp_path):
    with database() as db:
        destination, revision, run = delivery_graph(db, tmp_path, "models")
        attempt = DeliveryAttempt(
            organization_id=run.organization_id,
            run_id=run.id,
            destination_version_id=revision.id,
            attempt_number=1,
            idempotency_key="c" * 64,
            target_locator="sales.delivered_orders",
            rows_attempted=1,
        )
        db.add(attempt)
        db.commit()

        saved_destination = db.get(DeliveryDestination, destination.id)
        saved_revision = db.get(DeliveryDestinationVersion, revision.id)
        saved_attempt = db.get(DeliveryAttempt, attempt.id)
        assert saved_destination is not None
        assert (
            saved_destination.enabled,
            saved_destination.deleted,
            saved_destination.version,
            saved_destination.last_test_status,
        ) == (True, False, 1, "UNTESTED")
        assert saved_revision is not None
        assert saved_revision.destination_id == saved_destination.id
        assert saved_revision.config["options"] == {"sslmode": "require"}
        assert saved_revision.secret_reference not in saved_revision.config.values()
        assert saved_attempt is not None
        assert saved_attempt.status == "STARTED"
        assert saved_attempt.rows_attempted == 1
        assert saved_attempt.rows_written is None
        assert saved_attempt.finished_at is None
        assert isinstance(saved_attempt.started_at, datetime)


def test_delivery_revision_and_attempt_numbers_are_unique(database, tmp_path):
    with database() as db:
        destination, revision, run = delivery_graph(db, tmp_path, "constraints")
        db.add(
            DeliveryAttempt(
                organization_id=run.organization_id,
                run_id=run.id,
                destination_version_id=revision.id,
                attempt_number=1,
                idempotency_key="d" * 64,
                target_locator="sales.delivered_orders",
            )
        )
        db.commit()

        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(
                DeliveryDestinationVersion(
                    organization_id=destination.organization_id,
                    destination_id=destination.id,
                    version=1,
                    config={},
                    secret_reference="local:" + "e" * 32,
                    config_hash="f" * 64,
                )
            )
            db.flush()

        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(
                DeliveryAttempt(
                    organization_id=run.organization_id,
                    run_id=run.id,
                    destination_version_id=revision.id,
                    attempt_number=1,
                    idempotency_key="0" * 64,
                    target_locator="sales.delivered_orders",
                )
            )
            db.flush()


def test_database_job_queue_assigns_supported_lanes(database, tmp_path):
    queue = DatabaseJobQueue()
    assert isinstance(queue, JobQueue)
    with database() as db:
        _, _, default_run = delivery_graph(db, tmp_path, "default-lane")
        _, _, delivery_run = delivery_graph(db, tmp_path, "delivery-lane")
        _, _, rejected_run = delivery_graph(db, tmp_path, "invalid-lane")
        default_job = queue.submit(db, default_run)
        delivery_job = queue.submit(db, delivery_run, lane="DELIVERY")
        db.flush()

        assert default_job.lane == "DEFAULT"
        assert delivery_job.lane == "DELIVERY"
        with pytest.raises(ValueError, match="Lane"):
            queue.submit(db, rejected_run, lane="UNKNOWN")
        assert db.scalar(select(func.count(Job.id))) == 2


def test_workers_claim_only_their_lane(database, tmp_path):
    class RecordingEngine:
        key = "LANE_TEST"
        supported_processing_engines = frozenset({"POLARS"})

        def __init__(self):
            self.run_ids: list[str] = []

        def execute(self, db, run, *, lease_owner=None, observed_at=None):
            self.run_ids.append(run.id)
            run.status = "SUCCESS"

    queue = DatabaseJobQueue()
    engine = RecordingEngine()
    with database() as db:
        _, _, delivery_run = delivery_graph(db, tmp_path, "worker-delivery")
        _, _, default_run = delivery_graph(db, tmp_path, "worker-default")
        queue.submit(db, delivery_run, lane="DELIVERY")
        queue.submit(db, default_run)
        db.commit()
        delivery_run_id, default_run_id = delivery_run.id, default_run.id

    assert process_once("default-worker", engine=engine, lane="DEFAULT") is True
    assert engine.run_ids == [default_run_id]
    with database() as db:
        default_job = db.scalar(select(Job).where(Job.run_id == default_run_id))
        delivery_job = db.scalar(select(Job).where(Job.run_id == delivery_run_id))
        assert default_job is not None and default_job.status == "SUCCESS"
        assert delivery_job is not None and delivery_job.status == "QUEUED"

    assert process_once("delivery-worker", engine=engine, lane="DELIVERY") is True
    assert engine.run_ids == [default_run_id, delivery_run_id]
    assert process_once("delivery-worker", engine=engine, lane="DELIVERY") is False


def test_delivery_worker_does_not_replay_committed_attempt_after_retry_exhaustion(
    database, tmp_path
):
    class NoReplayEngine:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("A COMMITTED delivery must never be replayed")

    with database() as db:
        _, revision, run = delivery_graph(db, tmp_path, "committed-recovery")
        job = DatabaseJobQueue().submit(db, run, lane="DELIVERY")
        job.status = "RUNNING"
        job.attempts = 3
        job.lease_until = utcnow() - timedelta(minutes=1)
        run.status = "SUCCESS"
        run.decision = "COMMITTED"
        attempt = DeliveryAttempt(
            organization_id=run.organization_id,
            run_id=run.id,
            destination_version_id=revision.id,
            attempt_number=1,
            idempotency_key="1" * 64,
            target_locator="sales.delivered_orders",
            status="COMMITTED",
            rows_attempted=1,
            rows_written=1,
            finished_at=utcnow(),
        )
        db.add(attempt)
        db.commit()
        run_id = run.id

    assert process_once("recovery-worker", engine=NoReplayEngine(), lane="DELIVERY")

    with database() as db:
        saved_run = db.get(Run, run_id)
        saved_job = db.scalar(select(Job).where(Job.run_id == run_id))
        saved_attempt = db.scalar(
            select(DeliveryAttempt).where(DeliveryAttempt.run_id == run_id)
        )
        assert saved_run is not None and saved_run.status == "SUCCESS"
        assert saved_run.decision == "COMMITTED"
        assert saved_run.metrics["evidence_status"] == "PENDING_REPAIR"
        assert saved_job is not None and saved_job.status == "SUCCESS"
        assert saved_job.last_error is None and saved_job.lease_until is None
        assert saved_attempt is not None and saved_attempt.status == "COMMITTED"


def test_delivery_worker_claims_expired_job_before_durable_reconciliation(
    database, tmp_path, monkeypatch
):
    class NoReplayEngine:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("A COMMITTED delivery must never be replayed")

    with database() as db:
        _, revision, run = delivery_graph(db, tmp_path, "claim-before-reconcile")
        job = DatabaseJobQueue().submit(db, run, lane="DELIVERY")
        job.status = "RUNNING"
        job.lease_owner = "expired-owner"
        job.lease_until = utcnow() - timedelta(minutes=1)
        job.attempts = 1
        run.status = "SUCCESS"
        run.decision = "COMMITTED"
        db.add(
            DeliveryAttempt(
                organization_id=run.organization_id,
                run_id=run.id,
                destination_version_id=revision.id,
                attempt_number=1,
                idempotency_key="9" * 64,
                target_locator="sales.delivered_orders",
                status="COMMITTED",
                rows_attempted=1,
                rows_written=1,
                finished_at=utcnow(),
            )
        )
        db.commit()
        run_id = run.id

    original_reconcile = worker_module._reconcile_durable_delivery_state

    def assert_claimed_first(db, job, run):
        assert job.lease_owner == "new-owner"
        assert job.lease_until is not None
        return original_reconcile(db, job, run)

    monkeypatch.setattr(
        worker_module, "_reconcile_durable_delivery_state", assert_claimed_first
    )

    assert process_once("new-owner", engine=NoReplayEngine(), lane="DELIVERY")

    with database() as db:
        saved_job = db.scalar(select(Job).where(Job.run_id == run_id))
        assert saved_job is not None
        assert saved_job.status == "SUCCESS"
        assert saved_job.attempts == 1


def test_delivery_worker_does_not_replay_durable_failed_attempt(
    database, tmp_path
):
    class NoReplayEngine:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("A terminal failed delivery requires a new Run")

    with database() as db:
        _, revision, run = delivery_graph(db, tmp_path, "failed-recovery")
        job = DatabaseJobQueue().submit(db, run, lane="DELIVERY")
        job.status = "RUNNING"
        job.lease_owner = "interrupted-worker"
        job.attempts = 1
        job.lease_until = utcnow() - timedelta(minutes=1)
        run.status = "FAILED"
        run.decision = "FAILED"
        run.error = "El destino confirmó rollback."
        db.add(
            DeliveryAttempt(
                organization_id=run.organization_id,
                run_id=run.id,
                destination_version_id=revision.id,
                attempt_number=1,
                idempotency_key="3" * 64,
                target_locator="sales.delivered_orders",
                status="FAILED",
                error_code="DESTINATION_CONSTRAINT_VIOLATION",
                error_message=run.error,
                finished_at=utcnow(),
            )
        )
        db.commit()
        run_id = run.id

    assert process_once("recovery-worker", engine=NoReplayEngine(), lane="DELIVERY")

    with database() as db:
        saved_run = db.get(Run, run_id)
        saved_job = db.scalar(select(Job).where(Job.run_id == run_id))
        attempts = db.scalars(
            select(DeliveryAttempt).where(DeliveryAttempt.run_id == run_id)
        ).all()
        assert saved_run is not None and saved_run.status == "FAILED"
        assert saved_run.decision == "FAILED"
        assert saved_job is not None and saved_job.status == "FAILED"
        assert saved_job.attempts == 1
        assert len(attempts) == 1 and attempts[0].status == "FAILED"


@pytest.mark.parametrize("durable_status", ["COMMITTED", "UNKNOWN"])
def test_delivery_worker_preserves_remote_outcome_when_job_sync_crashes(
    database, tmp_path, durable_status
):
    class DurableOutcomeThenCrashEngine:
        def __init__(self, revision_id):
            self.revision_id = revision_id

        def execute(self, db, run, *, lease_owner=None, observed_at=None):
            attempt = DeliveryAttempt(
                organization_id=run.organization_id,
                run_id=run.id,
                destination_version_id=self.revision_id,
                attempt_number=1,
                idempotency_key="2" * 64,
                target_locator="sales.delivered_orders",
                status=durable_status,
                error_code=(
                    "DESTINATION_COMMIT_UNKNOWN"
                    if durable_status == "UNKNOWN"
                    else None
                ),
                error_message=(
                    "No se confirmó el commit." if durable_status == "UNKNOWN" else None
                ),
                finished_at=utcnow(),
            )
            db.add(attempt)
            run.status = "SUCCESS" if durable_status == "COMMITTED" else "UNKNOWN"
            run.decision = durable_status
            run.error = attempt.error_message
            db.commit()
            raise RuntimeError("falló la sincronización local posterior")

    with database() as db:
        _, revision, run = delivery_graph(db, tmp_path, f"sync-{durable_status}")
        DatabaseJobQueue().submit(db, run, lane="DELIVERY")
        db.commit()
        run_id, revision_id = run.id, revision.id

    assert process_once(
        "delivery-worker",
        engine=DurableOutcomeThenCrashEngine(revision_id),
        lane="DELIVERY",
    )

    with database() as db:
        saved_run = db.get(Run, run_id)
        saved_job = db.scalar(select(Job).where(Job.run_id == run_id))
        saved_attempt = db.scalar(
            select(DeliveryAttempt).where(DeliveryAttempt.run_id == run_id)
        )
        expected = "SUCCESS" if durable_status == "COMMITTED" else "UNKNOWN"
        assert saved_run is not None and saved_run.status == expected
        assert saved_run.decision == durable_status
        assert saved_job is not None and saved_job.status == expected
        assert saved_attempt is not None and saved_attempt.status == durable_status


def test_delivery_worker_preserves_concurrent_cancellation(database, tmp_path):
    class CancelThenRaiseEngine:
        def execute(self, db, run, *, lease_owner=None, observed_at=None):
            job = db.scalar(select(Job).where(Job.run_id == run.id))
            assert job is not None
            run.status = "CANCELLED"
            run.progress_stage = "Cancelado"
            run.finished_at = utcnow()
            job.status = "CANCELLED"
            job.lease_until = None
            db.commit()
            raise RuntimeError("lease cancelado durante preflight")

    with database() as db:
        _, _, run = delivery_graph(db, tmp_path, "cancel-race")
        DatabaseJobQueue().submit(db, run, lane="DELIVERY")
        db.commit()
        run_id = run.id

    assert process_once(
        "delivery-worker", engine=CancelThenRaiseEngine(), lane="DELIVERY"
    )

    with database() as db:
        saved_run = db.get(Run, run_id)
        saved_job = db.scalar(select(Job).where(Job.run_id == run_id))
        assert saved_run is not None and saved_run.status == "CANCELLED"
        assert saved_job is not None and saved_job.status == "CANCELLED"
        assert saved_job.last_error is None


def test_lane_configuration_and_heartbeats_are_separate():
    assert configured_lane(" default ") == "DEFAULT"
    assert configured_lane("delivery") == "DELIVERY"
    assert heartbeat_path("DEFAULT") != heartbeat_path("DELIVERY")
    with pytest.raises(RuntimeError, match="DEFAULT o DELIVERY"):
        configured_lane("priority")


def test_destination_secret_store_uses_separate_scoped_key(monkeypatch, tmp_path):
    from trackvance import delivery_credential_store as destination_module

    previous_store = destination_module.destination_secret_store
    destination_root = tmp_path / "delivery_credentials"
    destination_key = tmp_path / "delivery_keys" / "master.key"
    monkeypatch.setenv("TRACKVANCE_DESTINATION_SECRETS_DIR", str(destination_root))
    monkeypatch.setenv("TRACKVANCE_DESTINATION_SECRET_KEY_FILE", str(destination_key))
    try:
        reloaded = importlib.reload(destination_module)
        destination_store = reloaded.destination_secret_store
        source_store = EncryptedFileSecretStore(
            tmp_path / "connection_credentials",
            tmp_path / "connection_keys" / "master.key",
        )
        assert isinstance(destination_store, SecretStore)
        assert isinstance(destination_store, EncryptedFileSecretStore)
        assert destination_store.root == destination_root.resolve()
        assert destination_store.key_path == destination_key.resolve()
        assert destination_store.root != source_store.root
        assert destination_store.key_path != source_store.key_path

        source_reference = source_store.put("org-a", "source-read-password")
        destination_reference = destination_store.put("org-a", "destination-write-password")
        assert destination_store.get("org-a", destination_reference) == "destination-write-password"
        with pytest.raises(SecretStoreError):
            destination_store.get("org-b", destination_reference)
        assert destination_store.key_path.read_bytes() != source_store.key_path.read_bytes()

        copied = destination_store._path("org-a", source_reference)
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(source_store._path("org-a", source_reference).read_bytes())
        with pytest.raises(SecretStoreError):
            destination_store.get("org-a", source_reference)
        assert all(
            b"destination-write-password" not in path.read_bytes()
            for path in destination_root.rglob("*.secret")
        )
    finally:
        destination_module.destination_secret_store = previous_store
