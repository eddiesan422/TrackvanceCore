"""Immutable local resource preflight shared by preview and execution."""

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

from .config import STORAGE_DIR


class WorkloadMetadataError(ValueError):
    """A dispatch estimate cannot substitute descriptor bytes for data bytes."""


@dataclass(frozen=True)
class ResourceBudget:
    memory_soft_bytes: int = 512 * 1024 * 1024
    temp_min_free_bytes: int = 16 * 1024 * 1024
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or value <= 0 for value in asdict(self).values()):
            raise ValueError("Los presupuestos de memoria, disco y tiempo deben ser positivos.")

    @classmethod
    def from_environment(cls) -> "ResourceBudget":
        return cls(
            int(os.getenv("TRACKVANCE_WORKER_MEMORY_SOFT_BYTES", str(cls.memory_soft_bytes))),
            int(os.getenv("TRACKVANCE_TEMP_MIN_FREE_BYTES", str(cls.temp_min_free_bytes))),
            int(os.getenv("TRACKVANCE_RUN_TIMEOUT_SECONDS", str(cls.timeout_seconds))),
        )


@dataclass(frozen=True)
class WorkloadInput:
    row_count: int
    column_count: int
    size_bytes: int
    observed_record_bytes: int | None = None

    @classmethod
    def from_metadata(cls, db, version) -> "WorkloadInput":
        """Plan dispatch from publication facts; never open population storage.

        Old multipart publications without a persisted data size require an
        explicit metadata repair. Their small descriptor is not a safe estimate
        of the population. Execution still uses ``from_version`` and verifies it.
        """
        from .models import Artifact

        artifact = db.get(Artifact, version.canonical_artifact_id) if version.canonical_artifact_id else None
        if artifact is None or artifact.organization_id != version.organization_id:
            raise WorkloadMetadataError("WORKLOAD_METADATA_UNAVAILABLE")
        size = artifact.size_bytes
        if artifact.media_type == "application/vnd.trackvance.parquet-set+json":
            size = (version.ingestion_metadata or {}).get("canonical_size_bytes")
        counts = (version.row_count, version.column_count, size)
        if version.profile_status != "READY" or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise WorkloadMetadataError("WORKLOAD_METADATA_UNAVAILABLE")
        return cls(version.row_count, version.column_count, size, observed_record_bound(version))

    @classmethod
    def from_version(cls, db, version) -> "WorkloadInput":
        from .artifactstore import ArtifactIntegrityError, storage_provider
        from .models import Artifact

        size = version.size_bytes
        if version.canonical_artifact_id:
            artifact = db.get(Artifact, version.canonical_artifact_id)
            if artifact is None or artifact.organization_id != version.organization_id:
                raise ArtifactIntegrityError("No existe el artefacto canónico de esta versión.")
            size = artifact.size_bytes
            if artifact.media_type == "application/vnd.trackvance.parquet-set+json":
                with storage_provider.open_read(artifact) as stream:
                    descriptor = json.load(stream)
                size = descriptor.get("data_size_bytes")
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise ArtifactIntegrityError("El descriptor no contiene un tamaño de datos válido.")
        return cls(version.row_count, version.column_count, size, observed_record_bound(version))


def observed_record_bound(version) -> int | None:
    """Global profile fact, never an average inferred from compressed file size."""
    profile = version.profile or {}
    bound = profile.get("observed_record_bytes_upper_bound")
    if isinstance(bound, int) and not isinstance(bound, bool) and bound > 0:
        return bound
    columns = {column["name"]: column for column in profile.get("columns", [])}
    widths = []
    for schema in version.schema_json:
        column = columns.get(schema["name"], {})
        if column.get("null_count") == version.row_count:
            widths.append(32)
        elif isinstance(column.get("max_length"), int) and column["max_length"] >= 0:
            widths.append(32 + 4 * column["max_length"])
        else:
            return None
    return sum(widths) or 32


def _polars_evidence_bytes(module: str, inputs: list[WorkloadInput], config: dict) -> int:
    """Conservative population-wide output allowance before materializing Polars.

    Every Intake rule can fail every input row. Recon retains per-comparison
    details and lineage. Include text values and declared parameters/conditions,
    plus dictionaries and simultaneous JSON/frame output representations.
    This estimate is deliberately independent of an inspection sample.
    """
    if not inputs:
        return 0
    relevant = inputs[:1] if module == "intake" else inputs[:2]
    width = max(max(item.column_count * 96, item.observed_record_bytes or 0) for item in relevant)

    def declaration_bytes(rule: dict) -> int:
        return len(json.dumps(rule, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))

    if module == "intake":
        rules = [rule for rule in config.get("rules", []) if rule.get("enabled", True)]
        shorthand = sum(len(config.get(key, [])) for key in
                        ("required_columns", "unique_columns", "numeric_columns", "positive_columns"))
        per_row = sum(512 + width + declaration_bytes(rule) for rule in rules)
        per_row += shorthand * (512 + width + 128)
        return inputs[0].row_count * per_row * 2
    if module == "recon":
        rules = config.get("comparison_rules", [])
        if not rules and config.get("amount_column"):
            rules = [{"amount_column": config["amount_column"], "tolerance": config.get("tolerance", 0)}]
        per_row = 1024 + 2 * width + sum(512 + 2 * width + declaration_bytes(rule) for rule in rules)
        return sum(item.row_count for item in inputs[:2]) * per_row * 2
    return 0


class ExecutionPlanner:
    def __init__(self, budget: ResourceBudget | None = None, storage: Path = STORAGE_DIR,
                 *, spark_available: bool | None = None):
        self.budget = budget or ResourceBudget.from_environment()
        self.storage = storage
        self.spark_available = spark_available

    def plan(self, module: str, inputs: list[WorkloadInput], config: dict, free_bytes: int | None = None,
             *, requested_engine: str | None = None) -> dict:
        from .spark_engine import SPARK_VERSION, SparkSettings, runtime_status

        requested = (requested_engine or config.get("processing_engine") or "AUTO").upper()
        if requested not in {"AUTO", "POLARS", "PYSPARK"}:
            raise ValueError("Seleccione Automático, Polars o PySpark.")
        if any(i.row_count < 0 or i.column_count < 0 or i.size_bytes < 0
               or i.observed_record_bytes is not None and i.observed_record_bytes <= 0 for i in inputs):
            raise ValueError("La identidad de la carga contiene conteos negativos.")
        source_bytes = sum(i.size_bytes for i in inputs)
        # Account for decoded text, Python row evidence and hash-group joins in this bounded adapter.
        estimated = max(source_bytes * 12, sum(i.row_count * max(i.column_count * 96, i.observed_record_bytes or 0) for i in inputs))
        if module == "recon":
            estimated *= 2
        evidence_estimated = _polars_evidence_bytes(module, inputs, config)
        estimated += evidence_estimated
        free = shutil.disk_usage(self.storage).free if free_bytes is None else free_bytes
        required_disk = self.budget.temp_min_free_bytes + source_bytes * 3
        rejection = "RESOURCE_DISK_INSUFFICIENT" if free < required_disk else None
        engine = ("POLARS" if estimated <= self.budget.memory_soft_bytes else "PYSPARK") if requested == "AUTO" else requested
        settings = SparkSettings.from_environment()
        spark_memory_budget = int(os.getenv("TRACKVANCE_SPARK_MEMORY_BUDGET_BYTES", str(2 * 1024 ** 3)))
        if spark_memory_budget <= 0:
            raise ValueError("TRACKVANCE_SPARK_MEMORY_BUDGET_BYTES debe ser positivo.")
        spark_estimated = settings.driver_memory_mb * 1024 ** 2
        if settings.deployment_mode == "STANDALONE_CLIENT":
            spark_estimated += settings.executor_memory_mb * 1024 ** 2 * max(1, settings.total_cores // settings.executor_cores)
        # Python originals/transforms, Arrow and evidence coexist in each task.
        # A record/group can exceed a batch but must retain its own finite cap.
        spark_estimated += max(settings.batch_bytes * 4, settings.max_record_bytes * 4,
                               settings.max_group_bytes * 4 if module == "recon" else 0) * settings.total_cores
        available = runtime_status()["available"] if self.spark_available is None else self.spark_available
        if not rejection and engine == "POLARS" and estimated > self.budget.memory_soft_bytes:
            rejection = "RESOURCE_MEMORY_INSUFFICIENT"
        if not rejection and engine == "PYSPARK":
            if not available:
                rejection = "ENGINE_UNAVAILABLE"
            elif spark_estimated > spark_memory_budget:
                rejection = "RESOURCE_SPARK_MEMORY_INSUFFICIENT"
        reason = rejection or ("EXPLICIT_ENGINE_SELECTION" if requested != "AUTO" else "SPARK_BOUNDED_EXECUTION" if engine == "PYSPARK" else "STANDARD_MEMORY_BUDGET")
        return {
            "schema_version": 3, "requested_engine": requested, "engine": engine, "engine_version": pl.__version__ if engine == "POLARS" else SPARK_VERSION if available else None,
            "reason_code": reason, "allowed": rejection is None, "rejection_code": rejection,
            "estimated_input_bytes": source_bytes, "estimated_working_set_bytes": estimated,
            "estimated_polars_evidence_bytes": evidence_estimated,
            "free_temp_bytes": free, "required_temp_bytes": required_disk,
            "resource_budget": asdict(self.budget), "spill_allowed": engine == "PYSPARK",
            "budget_is_estimate": True,
            "deployment_mode": "IN_PROCESS" if engine == "POLARS" else settings.deployment_mode,
            "spark_parameters": asdict(settings) if engine == "PYSPARK" else None,
            "spark_estimated_working_set_bytes": spark_estimated if engine == "PYSPARK" else None,
            "spark_memory_budget_bytes": spark_memory_budget if engine == "PYSPARK" else None,
            "capabilities": ["INTAKE", "RECON", "SENTINEL", "EXACT_DECIMAL", "GLOBAL_UNIQUENESS", "REFERENCES"],
            "key_normalization": config.get("key_normalization"),
            "warnings": (["La carga necesita PySpark y Java compatibles en esta instalación."] if engine == "PYSPARK" and not available else ["La memoria estimada no sustituye los límites de los procesos/contenedores."]),
        }
