"""Opt-in real local JVM + transactional Run/Job/artifact publication."""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from trackvance import services
from trackvance.artifactstore import storage_provider
from trackvance.models import Artifact, ArtifactLink, DatasetVersion, Finding, Job, Run
from trackvance.services import ensure_input, iter_result_rows
from trackvance.spark_engine import runtime_status
from trackvance.worker import process_once


def test_real_local_spark_run_publishes_complete_registered_lineage(database, queued_intake, monkeypatch):
    if os.getenv("TRACKVANCE_SPARK_TESTS") != "1":
        pytest.skip("NOT_RUN_OPT_IN: real local Spark publication suite")
    assert runtime_status()["available"], "Real Spark publication requires Java 17/21 and pinned PySpark"
    monkeypatch.setenv("TRACKVANCE_SPARK_MASTER", "local[2]")
    monkeypatch.setattr(services, "ensure_input", lambda *_: pytest.fail("Spark must not load the driver's population"))
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        run.execution_plan = {**run.execution_plan, "requested_engine": "PYSPARK"}
        db.commit()
    assert process_once("real-spark-publication")
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        assert run.status == "SUCCESS", run.error
        job = db.scalar(select(Job).where(Job.run_id == run.id))
        assert job.status == "SUCCESS" and job.lease_until is None
        assert run.metrics["total_rows"] == 3 and run.metrics["error_rows"] == 2
        assert run.metrics["source_row_numbering"] == "PHYSICAL_LINE"
        runtime = run.execution_plan["runtime"]
        assert runtime["engine_version"] == "4.0.3" and runtime["deployment_mode"] == "LOCAL"
        assert runtime["application_id"].startswith("local-")
        rows = list(iter_result_rows(run, db=db, batch_rows=1))
        assert [row["original_row_number"] for row in rows] == [3, 4]
        output = db.get(DatasetVersion, run.output_version_id)
        assert output.parent_version_id == queued_intake["version_id"] and output.source_run_id == run.id
        assert output.row_count == 1 and output.column_count == 2
        assert ensure_input(output, db).to_dicts() == [{"order_id": "A", "amount": "12.25"}]
        result = db.scalar(select(Artifact).where(Artifact.path == run.result_path))
        descriptor = storage_provider.dataset_descriptor(result)
        assert descriptor["row_count"] == 2
        assert storage_provider.dataset_paths(result)
        assert db.scalar(select(ArtifactLink).where(ArtifactLink.source_id == run.id,
            ArtifactLink.relation == "RUN_OUTPUT", ArtifactLink.target_id == result.id))
        assert len(db.scalars(select(Finding).where(Finding.run_id == run.id)).all()) == 1
        manifest = json.loads(Path(run.evidence_path).read_text(encoding="utf-8"))
        assert manifest["processing"]["runtime"]["application_id"] == runtime["application_id"]
        assert manifest["output_version_id"] == output.id and manifest["references"] == []
