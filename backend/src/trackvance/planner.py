"""Immutable local resource preflight shared by preview and execution."""

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

from .config import STORAGE_DIR


@dataclass(frozen=True)
class ResourceBudget:
    memory_soft_bytes: int = 512 * 1024 * 1024
    temp_min_free_bytes: int = 16 * 1024 * 1024
    timeout_seconds: int = 300

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


class ExecutionPlanner:
    def __init__(self, budget: ResourceBudget | None = None, storage: Path = STORAGE_DIR):
        self.budget = budget or ResourceBudget.from_environment()
        self.storage = storage

    def plan(self, module: str, inputs: list[WorkloadInput], config: dict, free_bytes: int | None = None) -> dict:
        source_bytes = sum(i.size_bytes for i in inputs)
        # Account for decoded text, Python row evidence and hash-group joins in this bounded adapter.
        estimated = max(source_bytes * 12, sum(i.row_count * i.column_count * 96 for i in inputs))
        if module == "recon":
            estimated *= 2
        free = shutil.disk_usage(self.storage).free if free_bytes is None else free_bytes
        required_disk = self.budget.temp_min_free_bytes + source_bytes * 3
        rejection = "RESOURCE_DISK_INSUFFICIENT" if free < required_disk else None
        engine = "POLARS" if estimated <= self.budget.memory_soft_bytes else "PYSPARK"
        if engine == "PYSPARK" and not rejection:
            rejection = "ENGINE_UNAVAILABLE"
        reason = rejection or "STANDARD_MEMORY_BUDGET"
        return {
            "schema_version": 2, "engine": engine, "engine_version": pl.__version__ if engine == "POLARS" else None,
            "reason_code": reason, "allowed": rejection is None, "rejection_code": rejection,
            "estimated_input_bytes": source_bytes, "estimated_working_set_bytes": estimated,
            "free_temp_bytes": free, "required_temp_bytes": required_disk,
            "resource_budget": asdict(self.budget), "spill_allowed": False,
            "key_normalization": config.get("key_normalization"),
            "warnings": ["La carga necesita un motor de gran volumen que no está disponible en esta instalación."] if engine == "PYSPARK" else [],
        }
