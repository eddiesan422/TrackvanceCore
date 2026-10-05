"""Finite effective report budgets, separately enforced by process and DuckDB."""
import os
from dataclasses import asdict, dataclass


def positive(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise RuntimeError(f"{name} debe ser positivo.")
    return value


@dataclass(frozen=True)
class ReportLimits:
    profile: str
    threads: int
    memory_bytes: int
    process_memory_bytes: int
    timeout_seconds: int
    max_rows: int
    max_bytes: int
    batch_rows: int
    batch_bytes: int
    max_join_rows: int
    max_join_expansion: int
    temp_bytes: int
    concurrency: int

    @classmethod
    def configured(cls, profile: str = "PREVIEW"):
        if profile not in {"PREVIEW", "DOWNLOAD", "DATASET", "XLSX"}:
            raise ValueError("Perfil Reportes inválido.")
        rows = {"PREVIEW": 10, "DOWNLOAD": 100000, "DATASET": 5000000, "XLSX": 50000}[profile]
        seconds = {"PREVIEW": 60, "DOWNLOAD": 180, "DATASET": 1800, "XLSX": 180}[profile]
        mb = {"PREVIEW": 8, "DOWNLOAD": 128, "DATASET": 2048, "XLSX": 64}[profile]
        limits = cls(profile, positive("REPORT_THREADS", 2), positive("REPORT_MEMORY_MB", 512) * 1024**2,
                    positive("REPORT_PROCESS_MEMORY_MB", 2048) * 1024**2,
                    positive(f"REPORT_{profile}_TIMEOUT_SECONDS", seconds),
                    min(10, positive(f"REPORT_{profile}_MAX_ROWS", rows)) if profile == "PREVIEW" else positive(f"REPORT_{profile}_MAX_ROWS", rows),
                    positive(f"REPORT_{profile}_MAX_BYTES", mb * 1024**2),
                    positive("REPORT_BATCH_ROWS", 512), positive("REPORT_BATCH_BYTES", 8 * 1024**2),
                    positive("REPORT_MAX_JOIN_ROWS", 5000000), positive("REPORT_MAX_JOIN_EXPANSION", 100),
                    positive("REPORT_DATASET_TEMP_BYTES", 2 * 1024**3) if profile == "DATASET" else 0,
                    positive("REPORT_CONCURRENCY", 2))
        if limits.memory_bytes >= limits.process_memory_bytes or limits.batch_rows > 10000:
            raise RuntimeError("Presupuesto Reportes incompatible: memoria proceso > motor y lote <=10.000.")
        return limits

    def dto(self):
        return {**asdict(self), "max_sources": 8, "max_columns": 100,
                "xlsx_precision_policy": "DECIMAL_AS_TEXT", "max_cell_bytes": 65536,
                "filesystem_results": self.profile == "DATASET"}
