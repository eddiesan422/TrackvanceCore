"""Committed real Intake → frozen context → report → multipart dataset → Intake."""
import copy
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from trackvance.db import utcnow
from trackvance.governance import strict_approval
from trackvance.governance_models import (
    DataDomain,
    DatasetBlock,
    DatasetSecurityDependency,
    MacroDomain,
)
from trackvance.models import Artifact, Configuration, Dataset, DatasetVersion, Job, Run, User
from trackvance.operations_common import OperationError
from trackvance.report_models import (
    ReportContext,
    ReportDefinition,
    ReportExecution,
    ReportRevision,
)
from trackvance.report_service import (
    execution_sources,
    resolve,
    resolved_context,
    revalidate,
    start_execution,
)
from trackvance.report_worker import _lease, process_once
from trackvance.services import create_version, enqueue, execute_run


@pytest.fixture
def report_draft(database, tmp_path):
    requests = []
    with database() as db:
        macro = MacroDomain(name="Governed", normalized_name="governed")
        db.add(macro)
        db.flush()
        domain = DataDomain(name="Sales", normalized_name="sales", macro_domain_id=macro.id)
        db.add(domain)
        db.flush()
        for alias, rows in [("a", "001,alpha,12.01\n002,beta,3.10\n"), ("b", "001,uno,7.00\n003,tres,8.00\n")]:
            path = tmp_path / f"{alias}.csv"
            path.write_text("key,value,amount\n" + rows, encoding="utf-8")
            dataset = Dataset(name=f"Report input {alias}", macro_domain_id=macro.id, domain_id=domain.id)
            db.add(dataset)
            db.flush()
            source = create_version(db, dataset, path, path.name, column_overrides={"key": {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}})
            config = Configuration(name=f"Strict {alias}", module="intake", dataset_id=dataset.id,
                                   config={"required_columns": ["key", "value"], "max_error_rate": 0})
            db.add(config)
            db.flush()
            run = enqueue(db, config, source, None, "Test User")
            db.commit()
            execute_run(db, run)
            db.commit()
            assert strict_approval(db, run)["approved"]
            requests.append({"alias": alias, "input_dataset_id": dataset.id, "contract_id": config.id,
                             "contract_revision_ids": [config.id], "policy": "LATEST_APPROVED"})
        return {"mode": "GUIDED", "sources": requests,
                "joins": [{"left_alias": "a", "right_alias": "b", "type": "FULL",
                           "keys": [{"left_column": "key", "right_column": "key"}],
                           "expected_cardinality": "1:1", "allow_many_to_many": False}],
                "columns": [{"source_alias": "a", "column": "key", "alias": "left_id"},
                            {"source_alias": "b", "column": "key", "alias": "right_id"},
                            {"source_alias": "a", "column": "amount", "alias": "exact_amount"}]}


def test_joint_resolution_freezes_approved_output_and_rejects_fallback(database, report_draft):
    with database() as db:
        user = db.get(User, "test-user")
        context = resolve(db, user, report_draft)
        frozen = copy.deepcopy(context.snapshot)
        for source in frozen["sources"]:
            assert source["output_version_id"] != source["input_version_id"]
            assert source["approval_run_id"] == db.get(DatasetVersion, source["output_version_id"]).source_run_id
        chosen = frozen["sources"][0]
        output = db.get(DatasetVersion, chosen["output_version_id"])
        db.add(DatasetBlock(dataset_id=output.dataset_id, scope="REPORT", reason="Explicit report revocation", created_by_id=user.id))
        db.commit()
        with pytest.raises(OperationError) as revoked:
            revalidate(db, context, user, "reports:preview")
        assert revoked.value.code == "DATASET_BLOCKED"
        assert db.get(ReportContext, context.id).snapshot == frozen
        with pytest.raises(OperationError) as rejected:
            resolve(db, user, report_draft)
        assert rejected.value.code == "REPORT_SOURCE_INELIGIBLE"


def test_context_expiry_owner_and_integrity(database, report_draft):
    with database() as db:
        user = db.get(User, "test-user")
        context = resolve(db, user, report_draft)
        other = User(id="other-report-user", name="Other", email="other-report@example.test", password_hash="unused")
        db.add(other)
        db.commit()
        with pytest.raises(OperationError) as error:
            resolved_context(db, context.id, other)
        assert error.value.code == "REPORT_CONTEXT_OWNER"
        context = db.get(ReportContext, context.id)
        context.expires_at = utcnow() - timedelta(seconds=1)
        db.add(context)
        db.commit()
        with pytest.raises(OperationError) as error:
            resolved_context(db, context.id, user)
        assert error.value.code == "REPORT_CONTEXT_EXPIRED"
        context.expires_at = utcnow() + timedelta(minutes=1)
        context.snapshot = {**context.snapshot, "unexpected": "tampered"}
        db.commit()
        with pytest.raises(OperationError) as error:
            resolved_context(db, context.id, user)
        assert error.value.code == "REPORT_CONTEXT_INTEGRITY"


def test_full_evidence_hashing_runs_outside_resolution_and_detects_corruption(database, report_draft):
    with database() as db:
        context = resolve(db, db.get(User, "test-user"), report_draft)
        source = context.snapshot["sources"][0]
        run = db.get(Run, source["approval_run_id"])
        Path(run.evidence_path).write_bytes(b"synthetic corrupted manifest")
        with pytest.raises(OperationError):
            execution_sources(db, context)


@pytest.mark.skipif(sys.platform != "linux", reason="Real isolated executor runs in Docker")
def test_ephemeral_outputs_publication_idempotency_and_native_intake(authenticated, database, report_draft):
    client = authenticated
    resolution = client.post("/api/v1/reports/resolve", json={"draft": report_draft})
    assert resolution.status_code == 200, resolution.text
    context = resolution.json()["context_id"]
    with database() as db:
        before_artifacts = db.scalar(select(func.count()).select_from(Artifact))
        before_versions = db.scalar(select(func.count()).select_from(DatasetVersion))
    preview = client.post("/api/v1/reports/preview", json={"context_id": context})
    assert preview.status_code == 200, preview.text
    assert preview.json()["total_rows"] is None and len(preview.json()["rows"]) == 3
    for kind in ("CSV", "XLSX"):
        output = client.post("/api/v1/reports/download", json={"context_id": context, "format": kind})
        assert output.status_code == 200 and output.content
        with database() as db:
            history = db.get(ReportExecution, output.headers["X-Report-Execution-Id"])
            assert history.status == "SUCCESS" and history.transmission_status == "COMPLETE"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Artifact)) == before_artifacts
        assert db.scalar(select(func.count()).select_from(DatasetVersion)) == before_versions
        for execution in db.scalars(select(ReportExecution)):
            assert "rows" not in execution.publication
            assert "rows" not in db.get(ReportContext, execution.context_id).snapshot
    body = {"context_id": context, "idempotency_key": "dataset-request-0001", "name": "Joined report"}
    first = client.post("/api/v1/reports/datasets", json=body)
    assert first.status_code == 200, first.text
    assert client.post("/api/v1/reports/datasets", json=body).json()["id"] == first.json()["id"]
    assert process_once("report-test-worker")
    result = client.get("/api/v1/reports/executions/" + first.json()["id"]).json()
    assert result["status"] == "SUCCESS", (result["error_code"], result["error_message"])
    with database() as db:
        version = db.get(DatasetVersion, result["output_version_id"])
        assert version.source_type == "REPORT_OUTPUT" and version.version == 1 and version.row_count == 3
        assert version.ingestion_metadata["quality_status"] == "PENDING_VALIDATION"
        assert len(list(db.scalars(select(DatasetSecurityDependency).where(DatasetSecurityDependency.dataset_id == version.dataset_id)))) == 2
        # Intake can use the generated source before quality approval; access and
        # approval remain separate. A new strict contract can approve its output.
        config = Configuration(name="Validate joined population", module="intake", dataset_id=version.dataset_id,
                               config={"rules": [{"rule_id": "left_or_right", "type": "expression", "expression": "unsupported"}]})
        # Requiring a complete synthetic stable row-number would invent a
        # business column; validate a simple nullable-safe type rule instead.
        config.config = {"rules": [{"rule_id": "amount_type", "type": "type", "column": "exact_amount",
                                    "parameters": {"logical_type": "DECIMAL"}, "when": {"column": "exact_amount", "operator": "not_null"}}]}
        db.add(config)
        db.flush()
        run = enqueue(db, config, version, None, "Test User")
        db.commit()
        execute_run(db, run)
        db.commit()
        assert strict_approval(db, run)["approved"], run.metrics
    assert client.post("/api/v1/reports/datasets", json=body).json()["output_version_id"] == result["output_version_id"]


def test_lease_fence_rejects_recovered_worker(database, report_draft):
    from trackvance.report_service import start_execution
    with database() as db:
        user = db.get(User, "test-user")
        context = resolve(db, user, report_draft)
        execution = start_execution(db, user, context, "DATASET", {"name": "fence synthetic"}, "fence-synthetic")
        job = db.scalar(select(Job).where(Job.report_execution_id == execution.id))
        job.status, job.lease_owner, job.lease_until = "RUNNING", "new-worker", utcnow() + timedelta(minutes=1)
        db.commit()
        with pytest.raises(OperationError) as error:
            _lease(db, execution, "old-worker", lock=True)
        assert error.value.code == "REPORT_LEASE_LOST"


def _new_approval(db, source_request, tmp_path, *, changed_type=False):
    dataset = db.get(Dataset, source_request["input_dataset_id"])
    config = db.get(Configuration, source_request["contract_revision_ids"][0])
    path = tmp_path / (source_request["alias"] + "-new.csv")
    path.write_text("key,value,amount\n004,nuevo," + ("text" if changed_type else "9.00") + "\n", encoding="utf-8")
    version = create_version(db, dataset, path, path.name,
                             column_overrides={"key": {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}})
    run = enqueue(db, config, version, None, "Test User")
    db.commit()
    execute_run(db, run)
    db.commit()
    assert strict_approval(db, run)["approved"]
    return run


def test_multisource_snapshot_never_mixes_concurrent_approval_commit(database, report_draft, tmp_path, monkeypatch):
    from trackvance import report_service
    from trackvance.db import engine
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    with database() as db:
        before = resolve(db, db.get(User, "test-user"), report_draft)
        old_ids = [s["output_version_id"] for s in before.snapshot["sources"]]
        approvals = [_new_approval(db, source, tmp_path) for source in report_draft["sources"]]
        new_ids = [run.output_version_id for run in approvals]
        identities = [run.id for run in approvals]
        for run in approvals:
            run.status = "QUEUED"
        db.commit()
    original = report_service._source
    updated = False
    started = threading.Event()
    writer_errors = []

    def commit_approvals():
        try:
            with database() as concurrent:
                started.set()
                for identity in identities:
                    concurrent.get(Run, identity).status = "SUCCESS"
                concurrent.commit()
        except Exception as exc:  # noqa: BLE001 - project concurrent thread error to test.
            writer_errors.append(exc)

    writer = threading.Thread(target=commit_approvals)

    def committing_between_sources(joint, request, user):
        nonlocal updated
        result = original(joint, request, user)
        if not updated:
            updated = True
            writer.start()
            assert started.wait(5)
        return result

    monkeypatch.setattr(report_service, "_source", committing_between_sources)
    with database() as db:
        user = db.get(User, "test-user")
        during = resolve(db, user, report_draft)
        assert [s["output_version_id"] for s in during.snapshot["sources"]] == old_ids
        writer.join(timeout=10)
        assert not writer.is_alive() and not writer_errors
        after = resolve(db, user, report_draft)
        assert [s["output_version_id"] for s in after.snapshot["sources"]] == new_ids


def test_latest_input_rank_and_changed_used_schema_fail_without_fallback(database, report_draft, tmp_path):
    with database() as db:
        user = db.get(User, "test-user")
        context = resolve(db, user, report_draft)
        source = report_draft["sources"][0]
        old_run = db.get(Run, context.snapshot["sources"][0]["approval_run_id"])
        latest = _new_approval(db, source, tmp_path, changed_type=True)
        # A late execution of the old input must not win over newer INPUT.
        old_run.finished_at = utcnow() + timedelta(minutes=1)
        db.commit()
        selected = resolve(db, user, report_draft)
        assert selected.snapshot["sources"][0]["output_version_id"] == latest.output_version_id
        guarded = {**report_draft, "expected_schemas": context.snapshot["plan"]["expected_schemas"]}
        with pytest.raises(OperationError) as error:
            resolve(db, user, guarded)
        assert error.value.code == "REPORT_SCHEMA_CHANGED"


def _queue(database, draft, name, key):
    with database() as db:
        user = db.get(User, "test-user")
        context = resolve(db, user, draft)
        return start_execution(db, user, context, "DATASET", {"name": name}, key).id


def _assert_no_output(database, identity):
    from trackvance.artifactstore import artifact_store
    with database() as db:
        item = db.get(ReportExecution, identity)
        assert item.output_version_id is None
        assert not db.scalar(select(DatasetVersion.id).where(DatasetVersion.ingestion_metadata["report_execution_id"].as_string() == identity))
    staging = artifact_store.location("report-staging", identity)
    assert not staging.exists() or not list(staging.iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="Real isolated executor runs in Docker")
def test_empty_result_publishes_valid_empty_multipart(database, report_draft):
    from trackvance.artifactstore import storage_provider
    empty = {**report_draft, "post_filter": {"source_alias": "a", "column": "amount", "operator": "GT", "value": "9999"}}
    identity = _queue(database, empty, "Empty report", "empty-result-key")
    assert process_once("empty-worker")
    with database() as db:
        item = db.get(ReportExecution, identity)
        assert item.status == "SUCCESS", item.error_code
        version = db.get(DatasetVersion, item.output_version_id)
        assert version.row_count == 0 and version.column_count == 3 and version.profile_status == "READY"
        artifact = db.get(Artifact, version.canonical_artifact_id)
        assert storage_provider.dataset_paths(artifact)


@pytest.mark.skipif(sys.platform != "linux", reason="Real isolated executor runs in Docker")
@pytest.mark.parametrize("stage", ["QUEUED", "BATCHES", "PUBLICATION"])
def test_cancel_at_each_materialization_stage_leaves_no_partial_output(database, report_draft, monkeypatch, stage):
    from trackvance import report_worker
    identity = _queue(database, report_draft, "Cancelled " + stage, "cancel-stage-" + stage)

    def cancel():
        with database() as db:
            db.get(ReportExecution, identity).cancel_requested = True
            db.commit()

    if stage == "QUEUED":
        cancel()
    elif stage == "BATCHES":
        original = report_worker._write_parts

        def cancel_in_batches(messages, staging, limits, checkpoint, progress):
            def observed():
                for message in messages:
                    if message["kind"] == "batch":
                        cancel()
                    yield message
            return original(observed(), staging, limits, checkpoint, progress)
        monkeypatch.setattr(report_worker, "_write_parts", cancel_in_batches)
    else:
        original = report_worker.storage_provider.put_dataset

        def cancel_before_promotion(db, *args, **kwargs):
            cancel()
            return original(db, *args, **kwargs)
        monkeypatch.setattr(report_worker.storage_provider, "put_dataset", cancel_before_promotion)
    assert process_once("cancel-worker")
    with database() as db:
        item = db.get(ReportExecution, identity)
        assert item.status == "CANCELLED" and item.error_code == "REPORT_CANCELLED"
    _assert_no_output(database, identity)


@pytest.mark.skipif(sys.platform != "linux", reason="Real isolated executor runs in Docker")
def test_pending_publications_same_name_have_one_winner_and_no_orphans(database, report_draft):
    first = _queue(database, report_draft, "Publication contention", "contention-first")
    second = _queue(database, report_draft, "Publication contention", "contention-second")
    assert process_once("publisher-first") and process_once("publisher-second")
    with database() as db:
        assert db.get(ReportExecution, first).status == "SUCCESS"
        rejected = db.get(ReportExecution, second)
        assert rejected.status == "FAILED" and rejected.error_code == "DATASET_NAME_CONFLICT"
        assert db.scalar(select(func.count()).select_from(Dataset).where(Dataset.name == "Publication contention")) == 1
    _assert_no_output(database, second)


@pytest.mark.skipif(sys.platform != "linux", reason="Real isolated executor runs in Docker")
def test_worker_recovery_reclaims_old_prepared_attempt_and_publishes_once(database, report_draft, monkeypatch):
    from trackvance import report_worker
    from trackvance.artifactstore import artifact_store
    identity = _queue(database, report_draft, "Recovered report", "recovered-report")
    artifact_root = artifact_store.location("artifacts")
    before = {folder.name for folder in artifact_root.iterdir() if folder.is_dir()}
    original = report_worker.storage_provider.put_dataset
    stolen = False

    def recover_before_promotion(db, *args, **kwargs):
        nonlocal stolen
        if not stolen:
            stolen = True
            with database() as recovery:
                job = recovery.scalar(select(Job).where(Job.report_execution_id == identity))
                job.lease_owner, job.lease_until = "recovering-worker", utcnow() - timedelta(seconds=1)
                recovery.commit()
        return original(db, *args, **kwargs)

    monkeypatch.setattr(report_worker.storage_provider, "put_dataset", recover_before_promotion)
    assert process_once("lost-worker")
    _assert_no_output(database, identity)
    monkeypatch.setattr(report_worker.storage_provider, "put_dataset", original)
    assert process_once("recovering-worker")
    with database() as db:
        item = db.get(ReportExecution, identity)
        assert item.status == "SUCCESS", item.error_code
        job = db.scalar(select(Job).where(Job.report_execution_id == identity))
        assert job.status == "SUCCESS" and job.attempts == 2
        count = db.scalar(select(func.count()).select_from(DatasetVersion).where(DatasetVersion.dataset_id == db.get(DatasetVersion, item.output_version_id).dataset_id))
        assert count == 1
        referenced = set(db.scalars(select(Artifact.id)))
    artifacts = artifact_store.location("artifacts")
    assert all(folder.name in referenced for folder in artifacts.iterdir() if folder.is_dir() and folder.name not in before)


@pytest.mark.skipif(sys.platform != "linux", reason="Real isolated executor runs in Docker")
def test_actual_worker_process_crash_restart_and_abandoned_staging_cleanup(database, report_draft):
    from trackvance import report_worker
    from trackvance.artifactstore import artifact_store
    from trackvance.config import DATABASE_URL, STORAGE_DIR
    identity = _queue(database, report_draft, "Process crash recovery", "process-crash-recovery")
    script = '''
import os, sys
sys.path.insert(0, sys.argv[1])
from trackvance import report_worker
original = report_worker._write_parts
def crash_after_part(messages, staging, limits, checkpoint, progress):
    def crash_checkpoint():
        checkpoint()
        if list(staging.glob("part-*.parquet")):
            os._exit(77)
    return original(messages, staging, limits, crash_checkpoint, progress)
report_worker._write_parts = crash_after_part
report_worker.process_once("crashed-process")
sys.exit(1)
'''
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
                   "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                   "DATABASE_URL": DATABASE_URL, "TRACKVANCE_STORAGE_DIR": str(STORAGE_DIR),
                   "LD_LIBRARY_PATH": str(Path(sys.base_prefix) / "lib")}
    crashed = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                              str(Path(__file__).resolve().parents[1] / "src")],
                              env=environment, capture_output=True, text=True, timeout=60, check=False)
    assert crashed.returncode == 77, crashed.stderr
    staging = artifact_store.location("report-staging", identity)
    abandoned = staging / "1-crashed-process"
    assert list(abandoned.glob("part-*.parquet"))
    with database() as db:
        job = db.scalar(select(Job).where(Job.report_execution_id == identity))
        assert job.status == "RUNNING" and job.attempts == 1
        assert db.get(ReportExecution, identity).output_version_id is None
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_once("restarted-process")
    with database() as db:
        item = db.get(ReportExecution, identity)
        assert item.status == "SUCCESS", item.error_code
        assert db.scalar(select(Job).where(Job.report_execution_id == identity)).attempts == 2
    old = (utcnow() - timedelta(hours=2)).timestamp()
    os.utime(abandoned, (old, old))
    with database() as db:
        assert report_worker.cleanup_abandoned(db) >= 1
    assert not abandoned.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Real isolated executor runs in Docker")
def test_download_disconnect_kills_real_child_and_preserves_interruption(database, report_draft, monkeypatch):
    import anyio
    from starlette.requests import ClientDisconnect

    from trackvance import report_api, report_executor
    launched = []
    original = report_executor.subprocess.Popen

    def observed_process(*args, **kwargs):
        process = original(*args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(report_executor.subprocess, "Popen", observed_process)
    with database() as db:
        user = db.get(User, "test-user")
        context = resolve(db, user, report_draft)
        response = report_api.download(report_api.DownloadBody(context_id=context.id, format="CSV"), db, user)

    async def disconnect():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("Synthetic consumer disconnect")
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "method": "POST", "asgi": {"spec_version": "2.4"}}, receive, send)
    anyio.run(disconnect)
    assert launched and all(process.poll() is not None for process in launched)
    with database() as db:
        item = db.get(ReportExecution, response.identity)
        assert item.status == "INTERRUPTED" and item.transmission_status == "INTERRUPTED"
    _assert_no_output(database, response.identity)


def test_source_and_revision_history_pages_bound_metadata_instantiation(authenticated, database, report_draft):
    with database() as db:
        source_id = report_draft["sources"][0]["input_dataset_id"]
        for ordinal in range(140):
            db.add(Configuration(name=f"Paged contract {ordinal:03d}", module="intake", dataset_id=source_id, config={}))
        definition = ReportDefinition(name="Paged definition", owner_user_id="test-user")
        db.add(definition)
        db.flush()
        for revision in range(1, 151):
            db.add(ReportRevision(definition_id=definition.id, version=revision, draft={},
                                  query_hash="synthetic-metadata", created_by_user_id="test-user"))
        db.commit()
        definition_id = definition.id
    loaded = []

    def observed(session, instance):
        if isinstance(instance, Configuration):
            loaded.append(instance.id)
    event.listen(Session, "loaded_as_persistent", observed)
    try:
        first = authenticated.get("/api/v1/reports/sources?limit=1&offset=0")
        second = authenticated.get("/api/v1/reports/sources?limit=1&offset=1")
    finally:
        event.remove(Session, "loaded_as_persistent", observed)
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == 142
    assert first.json()["items"][0]["contract_id"] != second.json()["items"][0]["contract_id"]
    # A page of one contract must not instantiate the 140 unrelated configs.
    assert len(set(loaded)) <= 2
    detail = authenticated.get(f"/api/v1/reports/definitions/{definition_id}?revision_offset=100&revision_limit=2")
    assert detail.status_code == 200
    assert detail.json()["revision_total"] == 150
    assert [revision["version"] for revision in detail.json()["revisions"]] == [50, 49]
    assert detail.json()["selected_revision"]["version"] == 150
    listing = authenticated.get("/api/v1/reports/definitions").json()
    assert len(listing["items"][0]["revisions"]) == 1
    one = first.json()["items"][0]["contract_id"]
    selected = authenticated.get("/api/v1/reports/sources", params={"contract_id": one}).json()
    assert selected["total"] == 1 and selected["items"][0]["contract_id"] == one
    with database() as db:
        historical = db.scalar(select(ReportRevision).where(ReportRevision.definition_id == definition_id, ReportRevision.version == 1))
        other = ReportDefinition(name="Other definition", owner_user_id="test-user")
        db.add(other)
        db.flush()
        foreign = ReportRevision(definition_id=other.id, version=1, draft={}, query_hash="foreign", created_by_user_id="test-user")
        db.add(foreign)
        db.commit()
        historical_id, foreign_id = historical.id, foreign.id
    exact = authenticated.get(f"/api/v1/reports/definitions/{definition_id}", params={"revision_id": historical_id})
    assert exact.status_code == 200 and exact.json()["selected_revision"]["version"] == 1
    assert exact.json()["revisions"][0]["version"] == 150
    for wrong in (foreign_id, "nonexistent-revision"):
        assert authenticated.get(f"/api/v1/reports/definitions/{definition_id}", params={"revision_id": wrong}).status_code == 404


def test_global_api_worker_admission_and_expired_runner_cannot_revive(database, report_draft):
    from trackvance.report_service import admission, check_execution
    with database() as db:
        user = db.get(User, "test-user")
        context = resolve(db, user, report_draft)
        first = start_execution(db, user, context, "PREVIEW")
        admission(db, first)
        second = start_execution(db, user, context, "DATASET", {"name": "Global admission worker"}, "global-admission")
        admission(db, second)
        third = start_execution(db, user, context, "DOWNLOAD")
        with pytest.raises(OperationError) as error:
            admission(db, third)
        assert error.value.code == "REPORT_CONCURRENCY_LIMIT"
        db.rollback()
        first = db.get(ReportExecution, first.id)
        first.started_at = utcnow() - timedelta(minutes=3)
        db.commit()
        first_id, third_id = first.id, third.id
    with pytest.raises(OperationError) as error:
        check_execution(first_id, "reports:preview")
    assert error.value.code == "REPORT_TIMEOUT"
    with database() as db:
        admission(db, db.get(ReportExecution, third_id))
        assert db.get(ReportExecution, first_id).status == "INTERRUPTED"
        assert db.scalar(select(func.count()).select_from(ReportExecution).where(ReportExecution.status == "RUNNING")) == 2
    with pytest.raises(OperationError) as error:
        check_execution(first_id, "reports:preview")
    assert error.value.code == "REPORT_EXECUTOR_LOST"


def test_idempotent_submission_replay_conflict_and_definition_name_collision(authenticated, database, report_draft):
    response = authenticated.post("/api/v1/reports/resolve", json={"draft": report_draft})
    context_id = response.json()["context_id"]
    body = {"context_id": context_id, "idempotency_key": "lost-submission-response", "name": "Lost submission"}
    first = authenticated.post("/api/v1/reports/datasets", json=body)
    replay = authenticated.post("/api/v1/reports/datasets", json=body)
    assert first.status_code == replay.status_code == 200 and first.json()["id"] == replay.json()["id"]
    collision = authenticated.post("/api/v1/reports/datasets", json={**body, "name": "Different request"})
    assert collision.status_code == 409
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.report_execution_id == first.json()["id"])) == 1
    definition = {"name": "Unique report definition", "draft": report_draft}
    assert authenticated.post("/api/v1/reports/definitions", json=definition).status_code == 200
    assert authenticated.post("/api/v1/reports/definitions", json=definition).status_code == 409
