"""Spark compute orchestration, with cancellation/lease monitoring before publication.

Only the owning application worker publishes metadata and artifacts. Executors
receive immutable materialized paths and package code; no SQL secrets or sessions.
"""

from __future__ import annotations

import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .artifactstore import storage_provider
from .db import SessionLocal
from .models import Job, Run, utcnow
from .processing import ProcessingError
from .spark_engine import (
    SparkDataset,
    SparkProcessingResult,
    SparkRuntime,
    SparkSettings,
    process_dataset_run,
)


class SparkRunCancelled(ProcessingError):
    pass


def spark_input(db, version) -> SparkDataset:
    from .dataset_scans import version_paths
    from .planner import observed_record_bound

    metadata = version.ingestion_metadata or {}
    original = None
    if (not metadata.get("record_number_column") and version.original_path
            and metadata.get("source_format") in {None, "CSV", "TXT"}):
        original = str(storage_provider.materialize_reference(
            version.original_path, expected_sha256=version.sha256,
            expected_size=version.size_bytes
        ))
    return SparkDataset(
        paths=tuple(str(path) for path in version_paths(db, version)),
        columns=tuple(column["name"] for column in version.schema_json),
        row_count=version.row_count,
        row_numbering=metadata.get("row_numbering", "PHYSICAL_LINE" if original else "RECORD_NUMBER"),
        record_number_column=metadata.get("record_number_column"),
        original_delimited_path=original,
        delimiter=metadata.get("reader_options", {}).get("delimiter"),
        observed_record_bound=observed_record_bound(version),
    )


def compute_spark_run(db, run, source, config, directory: Path, *, target=None,
                      references=None, sentinel_context=None, lease_owner=None) -> SparkProcessingResult:
    """Compute complete parts; callers must re-lock Run/Job before publishing them."""
    source_input = spark_input(db, source)
    target_input = spark_input(db, target) if target else None
    reference_inputs = {version.id: spark_input(db, version) for version in references or []}
    run_id = run.id
    deadline = time.monotonic() + run.execution_plan.get("resource_budget", {}).get("timeout_seconds", 300)
    stop = threading.Event()
    reason: list[str] = []
    monitor: threading.Thread | None = None
    settings = SparkSettings(**run.execution_plan["spark_parameters"]) if run.execution_plan.get("spark_parameters") else SparkSettings.from_environment()
    minimum_free = run.execution_plan.get("resource_budget", {}).get("temp_min_free_bytes", 16 * 1024 ** 2)

    def start_monitor(runtime: SparkRuntime):
        nonlocal monitor

        def observe():
            while not stop.wait(0.5):
                try:
                    with SessionLocal() as control:
                        current = control.get(Run, run_id)
                        job = control.scalar(select(Job).where(Job.run_id == run_id))
                        if current is None or current.cancel_requested:
                            reason.append("RUN_CANCELLED")
                        elif lease_owner is not None and (
                            job is None or job.lease_owner != lease_owner or job.status != "RUNNING"
                            or job.lease_until is None
                            or job.lease_until.replace(tzinfo=UTC) <= utcnow()
                        ):
                            reason.append("WORKER_LEASE_LOST")
                        elif time.monotonic() >= deadline:
                            reason.append("RUN_TIMEOUT_EXCEEDED")
                        elif shutil.disk_usage(directory).free < minimum_free:
                            reason.append("RESOURCE_DISK_INSUFFICIENT")
                except (SQLAlchemyError, OSError):
                    # Metadata unavailable means authority cannot be verified; fail closed.
                    reason.append("WORKER_AUTHORITY_UNVERIFIED")
                if reason:
                    runtime.cancel()
                    return

        monitor = threading.Thread(target=observe, name="spark-control-" + run_id, daemon=True)
        monitor.start()

    try:
        result = process_dataset_run(
            run.module, source_input, config, directory,
            observed_at=run.started_at or datetime.now(UTC), target=target_input,
            references=reference_inputs, sentinel_context=sentinel_context,
            on_runtime=start_monitor,
            settings=settings,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 - sanitize opaque Spark/Java failures at this boundary.
        if reason:
            if reason[0] == "RUN_CANCELLED":
                raise SparkRunCancelled("RUN_CANCELLED") from None
            raise ProcessingError(reason[0]) from None
        # Executor exceptions arrive wrapped in an opaque Java traceback. Expose
        # only the known portable resource code, never its payload or stack.
        if "trackvance.processing.ProcessingError: RESOURCE_RECON_GROUP_LIMIT:" in str(exc):
            raise ProcessingError("RESOURCE_RECON_GROUP_LIMIT: La clave excede el límite configurado por grupo.") from None
        if "trackvance.processing.ProcessingError: RESOURCE_SPARK_RECORD_LIMIT:" in str(exc):
            raise ProcessingError("RESOURCE_SPARK_RECORD_LIMIT: El registro o transformación excede el presupuesto de bytes.") from None
        # Spark/driver exception text can contain values or source paths. Keep it private.
        raise ProcessingError("SPARK_EXECUTION_FAILED: Falló el procesamiento Spark; consulte la referencia de ejecución.") from None
    finally:
        stop.set()
        if monitor:
            monitor.join(timeout=2)
    if reason:
        if reason[0] == "RUN_CANCELLED":
            raise SparkRunCancelled("RUN_CANCELLED")
        raise ProcessingError(reason[0])
    return result


class PySparkExecutionEngine:
    key = "SPARK"
    supported_processing_engines = frozenset({"PYSPARK"})

    def execute(self, db, run, *, lease_owner=None, observed_at=None):
        from .services import execute_run

        if run.execution_plan.get("engine") != "PYSPARK":
            raise ProcessingError("ENGINE_PLAN_MISMATCH: El plan no seleccionó PySpark.")
        execute_run(db, run, lease_owner=lease_owner, observed_at=observed_at)
