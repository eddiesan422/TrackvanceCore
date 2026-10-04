"""Publication contracts isolated from the opt-in JVM parity suite."""

import json
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import select

from trackvance import services, spark_engine, spark_execution
from trackvance.artifactstore import ArtifactIntegrityError, storage_provider
from trackvance.db import utcnow
from trackvance.models import (
    Artifact,
    Configuration,
    Dataset,
    DatasetVersion,
    Finding,
    Job,
    Run,
    SentinelMetricHistory,
    User,
)
from trackvance.planner import WorkloadInput
from trackvance.processing import ProcessingError, intake
from trackvance.services import _verify_lease, ensure_input, iter_result_rows, result_rows
from trackvance.spark_engine import RECORD_NUMBER_COLUMN, SparkProcessingResult, _result_tuple
from trackvance.worker import process_once


def test_plan_uses_physical_multipart_bytes_and_global_width(database, tmp_path):
    from trackvance.dataset_scans import publish_materialized_version

    with database() as db:
        dataset = Dataset(name="Plan exact multipart")
        db.add(dataset)
        db.flush()
        parts = []
        for index in range(2):
            path = tmp_path / f"part-{index}.parquet"
            pl.DataFrame({"id": ["😀" * 4, "1"], "amount": ["0.00000000000000000001", "2"]}).write_parquet(path)
            parts.append(path)
        version = publish_materialized_version(db, dataset, parts, filename="canonical", actor="Test User")
        workload = WorkloadInput.from_version(db, version)
        canonical = db.get(Artifact, version.canonical_artifact_id)
        physical_size = sum(path.stat().st_size for path in storage_provider.dataset_paths(canonical))
        assert workload.size_bytes == physical_size
        assert workload.observed_record_bytes == 32 * 2 + 4 * (4 + 22)


def test_plan_preview_includes_reference_versions(database, queued_intake, tmp_path, monkeypatch):
    from trackvance import planner
    from trackvance.api import PlanPreviewBody, preview_execution

    observed = []

    def capture(self, module, inputs, config, **kwargs):
        observed.extend(inputs)
        return {"allowed": True}

    monkeypatch.setattr(planner.ExecutionPlanner, "plan", capture)
    with database() as db:
        config = db.get(Configuration, queued_intake["config_id"])
        reference = Dataset(name="Immutable reference", organization_id=config.organization_id)
        db.add(reference)
        db.flush()
        path = tmp_path / "reference.csv"
        path.write_text("id\nA\n", encoding="utf-8")
        version = services.create_version(db, reference, path, path.name)
        config.config = {"rules": [{"type": "reference", "column": "order_id", "parameters": {
            "dataset_version_id": version.id, "reference_columns": ["id"]}}]}
        db.flush()
        preview_execution(PlanPreviewBody(configuration_id=config.id,
                          dataset_version_id=queued_intake["version_id"]), db, db.get(User, "test-user"))
        assert len(observed) == 2 and observed[1].row_count == 1


def test_sentinel_history_retains_only_the_exact_compatible_window(database, queued_intake):
    with database() as db:
        config = db.get(Configuration, queued_intake["config_id"])
        source = db.get(DatasetVersion, queued_intake["version_id"])
        current = db.get(Run, queued_intake["run_id"])
        config.module = "sentinel"
        config.config = {"rules": [{"type": "historical_band", "parameters": {
            "metric": "row_count", "window": 3, "min_history": 2}}]}
        for index in range(30):
            run = Run(id=f"history-{index}", organization_id=current.organization_id, module="sentinel",
                      name=config.name, config_id=config.id, dataset_version_id=source.id, status="SUCCESS",
                      initiated_by="Test User")
            db.add(run)
            db.flush()
            db.add(SentinelMetricHistory(organization_id=current.organization_id, monitor_id=config.id,
                   run_id=run.id, metric_key="row_count", dimension_hash="none",
                   numeric_value=None if index > 27 else str(index),
                   method="INCOMPATIBLE" if index == 27 else source.profile["metric_method"],
                   metric_definition_version=source.profile["metric_definition_version"],
                   observed_at=utcnow() + timedelta(seconds=index)))
        db.flush()
        context = services._sentinel_context(db, current, source, config)
        assert [record["numeric_value"] for record in context["history"]] == ["24", "25", "26"]


def enable_spark_plan(database, run_id, monkeypatch):
    monkeypatch.setattr(spark_engine, "runtime_status", lambda: {"available": True})
    with database() as db:
        run = db.get(Run, run_id)
        run.execution_plan = {**run.execution_plan, "requested_engine": "PYSPARK"}
        db.commit()


def test_spark_publication_uses_complete_multipart_and_preserves_lineage(database, queued_intake, monkeypatch):
    enable_spark_plan(database, queued_intake["run_id"], monkeypatch)

    def compute(db, run, source, config, directory, **_):
        directory.mkdir()
        frame = pl.read_parquet(storage_provider.dataset_paths(db.get(Artifact, source.canonical_artifact_id)))
        rows, metrics, accepted = intake(frame, config, run.started_at, [2, 3, 4])
        result_paths = []
        for index, record in enumerate(reversed(rows)):
            path = directory / f"result-{index}.parquet"
            pl.DataFrame([_result_tuple(record, "intake")],
                schema={"classification": pl.String, "payload": pl.String, "__tv_sort_key": pl.String},
                orient="row").write_parquet(path)
            result_paths.append(path)
        accepted_path = directory / "accepted.parquet"
        accepted.with_columns(pl.Series(RECORD_NUMBER_COLUMN, [2], dtype=pl.Int64)).write_parquet(accepted_path)
        return SparkProcessingResult(tuple(result_paths), (accepted_path,), metrics, metrics["decision"],
                                     {"engine": "PYSPARK", "engine_version": "4.0.3", "application_id": "local-contract"})

    monkeypatch.setattr(spark_execution, "compute_spark_run", compute)
    monkeypatch.setattr(services, "ensure_input", lambda *_: pytest.fail("Spark must not materialize driver input"))
    assert process_once("spark-publication-worker")
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        assert run.status == "SUCCESS", run.error
        assert run.execution_plan["runtime"]["application_id"] == "local-contract"
        assert run.metrics["source_row_numbering"] == "PHYSICAL_LINE"
        output = db.get(DatasetVersion, run.output_version_id)
        assert output.parent_version_id == queued_intake["version_id"] and output.source_run_id == run.id
        assert output.ingestion_metadata["record_number_column"] == RECORD_NUMBER_COLUMN
        assert ensure_input(output, db).to_dicts() == [{"order_id": "A", "amount": "12.25"}]
        assert [row["original_row_number"] for row in iter_result_rows(run, db=db, batch_rows=1)] == [3, 4]
        assert result_rows(run, offset=1, limit=1, db=db)["items"][0]["original_row_number"] == 4
        assert result_rows(run, db=db)["total"] == 2
        assert len(db.scalars(select(Finding).where(Finding.run_id == run.id)).all()) == 1
        manifest = json.loads(Path(run.evidence_path).read_text(encoding="utf-8"))
        assert manifest["processing"]["runtime"]["engine_version"] == "4.0.3"
        result = db.scalar(select(Artifact).where(Artifact.path == run.result_path))
        parts = storage_provider.dataset_paths(result)
        parts[0].write_bytes(b"corrupt")
        with pytest.raises(ArtifactIntegrityError):
            list(iter_result_rows(run, db=db))


def test_spark_cancellation_cannot_publish_partial_results(database, queued_intake, monkeypatch):
    enable_spark_plan(database, queued_intake["run_id"], monkeypatch)
    staged = []

    def compute(_db, _run, _source, _config, directory, **_):
        directory.mkdir()
        (directory / "partial.parquet").write_bytes(b"partial")
        staged.append(directory)
        raise spark_execution.SparkRunCancelled("RUN_CANCELLED")

    monkeypatch.setattr(spark_execution, "compute_spark_run", compute)
    assert process_once("spark-cancel-worker")
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        assert run.status == "CANCELLED" and run.result_path is None and run.output_version_id is None
        assert run.evidence_path is None
    assert staged and not staged[0].exists()


def test_publication_fence_rechecks_authority_after_commit(database, queued_intake):
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        job = db.scalar(select(Job).where(Job.run_id == run.id))
        job.status, job.lease_owner, job.lease_until = "RUNNING", "owner", utcnow() + timedelta(seconds=5)
        db.commit()
        _verify_lease(db, run, "owner")
        # Authority is retained by the transaction's DB fence until commit.
        _verify_lease(db, run, "owner")
        db.commit()
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
        with pytest.raises(ProcessingError, match="WORKER_LEASE_LOST"):
            _verify_lease(db, run, "owner")
