"""Sentinel observations from an immutable, complete profile; no data reads."""

import json
import shutil
from dataclasses import asdict

import polars as pl

from .config_semantics import METRIC_TYPES


def profile_execution_plan(source, config: dict, requested_engine: str = "AUTO", *, population_bytes: int | None = None) -> dict:
    from .config import STORAGE_DIR
    from .planner import ResourceBudget

    budget = ResourceBudget.from_environment()
    size = len(json.dumps({"profile": source.profile, "schema": source.schema_json, "config": config},
                          ensure_ascii=False).encode("utf-8"))
    free = shutil.disk_usage(STORAGE_DIR).free
    # Metadata and bounded check evidence coexist. Data rows are not decoded.
    estimated = size * 8 + 65536
    required = budget.temp_min_free_bytes + size * 3
    rejection = "RESOURCE_DISK_INSUFFICIENT" if free < required else (
        "RESOURCE_MEMORY_INSUFFICIENT" if estimated > budget.memory_soft_bytes else None)
    return {"schema_version": 3, "requested_engine": requested_engine, "engine": "POLARS",
        "engine_version": pl.__version__, "allowed": rejection is None,
        "reason_code": rejection or "COMPLETE_PROFILE_REUSE", "rejection_code": rejection,
        "estimated_input_bytes": population_bytes, "metadata_profile_bytes": size,
        "estimated_working_set_bytes": estimated,
        "estimated_polars_evidence_bytes": size * 2, "free_temp_bytes": free,
        "required_temp_bytes": required, "resource_budget": asdict(budget),
        "budget_is_estimate": True, "deployment_mode": "IN_PROCESS", "spill_allowed": False,
        "spark_parameters": None, "spark_estimated_working_set_bytes": None,
        "spark_memory_budget_bytes": None, "capabilities": ["SENTINEL", "COMPLETE_PROFILE_REUSE"],
        "warnings": [], "profile_population_rows": source.row_count, "profile_dataset_version_id": source.id}


def profile_sufficient(source, config: dict) -> bool:
    profile = source.profile or {}
    if source.profile_status != "READY" or profile.get("row_count") != source.row_count:
        return False
    if not isinstance(profile.get("columns"), list) or len(profile["columns"]) != source.column_count:
        return False
    if any(not isinstance(column.get(key), int) or isinstance(column.get(key), bool) or column[key] < 0
           for column in profile["columns"] for key in ("null_count", "distinct_count")):
        return False
    if config.get("automatic_base_version") and "duplicate_row_count" not in profile:
        return False
    return all(not rule.get("enabled", True) or rule.get("type") in METRIC_TYPES
               for rule in config.get("rules", []))


def augment_base_observations(metrics: dict, source, config: dict, previous_schema: list | None,
                              *, profile_snapshot: dict | None = None) -> dict:
    """Informational nulls/categories never become implicit required/unique rules."""
    profile = profile_snapshot if profile_snapshot is not None else source.profile or {}
    metrics["profile_snapshot"] = profile
    metrics["schema_snapshot"] = source.schema_json
    metrics["profile_dataset_version_id"] = source.id
    metrics["valid_rows"] = None
    metrics["valid_rows_reason"] = "SENTINEL_CONTROLS_ARE_NOT_ROW_VALIDATION"
    if not config.get("automatic_base_version"):
        metrics["conforming_checks"] = metrics.get("total_checks", 0) - metrics.get("failed_checks", 0)
        return metrics
    checks = list(metrics["checks"])
    method = profile.get("metric_method", "LEGACY_NORMALIZED")
    definition = profile.get("metric_definition_version", 1)

    def add(code, name, actual, expected, passed=True, message="", kind="OBSERVATION", **extra):
        checks.append({"code": code, "name": name, "actual": actual, "expected": expected,
            "status": ("UNAVAILABLE" if actual is None else "OBSERVED") if kind == "OBSERVATION" else "PASS" if passed else "FAIL",
            "classification": "PASS" if passed else "FAIL",
            "message": message, "kind": kind, "severity": "WARNING",
            "metric_method": method, "metric_definition_version": definition, **extra})

    for column in profile.get("columns", []):
        name = column["name"]
        add("BASE_NULLS", f"Nulos · {name}", column.get("null_count"), "Observación exacta",
            message="Los nulos se describen; solo una regla explícita establece su obligatoriedad.", column=name)
        add("BASE_DIVERSITY", f"Diversidad · {name}", column.get("distinct_count"), "Valores distintos sin nulos",
            message="La repetición de categorías no implica un incumplimiento de unicidad.", column=name)
    duplicates = profile.get("duplicate_row_count")
    add("BASE_DUPLICATE_ROWS", "Filas duplicadas exactas", duplicates,
        {"max": 0} if duplicates is not None else "No disponible en el perfil histórico",
        passed=duplicates is None or duplicates == 0, kind="CONTROL" if duplicates is not None else "OBSERVATION",
        message="Filas adicionales a la primera de cada grupo idéntico; incluye nulos iguales. Advertencia descriptiva.")
    if previous_schema is not None:
        signature = lambda schema: [(item["name"], item.get("logical_type")) for item in schema]
        add("BASE_SCHEMA_CHANGE", "Estabilidad del esquema", signature(source.schema_json), signature(previous_schema),
            passed=signature(source.schema_json) == signature(previous_schema), kind="CONTROL",
            message="Compara nombres, orden y tipos observados con la versión anterior; no usa la nullable observada.")
    else:
        add("BASE_SCHEMA_BASELINE", "Esquema inicial", source.schema_json, "Primera referencia",
            message="Sin versión anterior: se registra el esquema y no se infiere estabilidad.")
    controls = [check for check in checks if check.get("kind") != "OBSERVATION"]
    failed = sum(check["status"] == "FAIL" for check in controls)
    metrics.update(checks=checks, total_checks=len(controls), failed_checks=failed,
                   conforming_checks=len(controls) - failed, observation_count=len(checks) - len(controls))
    metrics["health_score"] = None
    metrics["quality_interpretation"] = "CONTROL_COUNTS_ONLY"
    metrics["conforming_controls_count"] = len(controls) - failed
    metrics.pop("null_rate", None)  # No configured null threshold; zero is not evidence.
    for key in ("duplicate_row_count", "distinct_row_count"):
        if key in profile:
            metrics["metric_records"].append({"metric_key": key, "numeric_value": str(profile[key]),
                "method": method, "metric_definition_version": definition, "dimensions": {
                    "definition": profile.get("row_duplicate_definition")}})
    return metrics
