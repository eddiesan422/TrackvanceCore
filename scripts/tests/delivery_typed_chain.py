"""Small real R085-03 chain and independent full SQL value oracle.

This module uses HTTP and the runner's owned native SQL targets. It does not
import product serializers or conversion helpers. Host tests exercise rejection
guards; only the real Delivery runner can produce native certification evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

SOURCE_NAMES = {"optional_note": "preserved_note"}
ENGINES = ("POLARS", "PYSPARK")
TYPES = ["STRING", "STRING", "DECIMAL", "INT64", "DATE", "TIMESTAMP", "BOOLEAN", "STRING", "INT64"]


def column_names(runner):
    return [*runner.DATASET_COLUMNS, "all_null_int"]


def fixture_rows(runner):
    rows = [list(row) for row in runner.DATASET_ROWS]
    # Numeric-looking STRING values with leading zeros, and an explicit NULL.
    for row, note in zip(rows, ("000001", None, "000003"), strict=True):
        row[-1] = note
        row.append(None)  # Declared INT64 remains INT64 even with no non-null observations.
    return rows


def normalize_rows(rows):
    """Compare all 27 values, preserving native scalar families and UTC microseconds."""
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 9:
            raise ValueError("TYPED_CHAIN_ROW_WIDTH")
        key, name, amount, quantity, day, stamp, active, note, all_null = row
        if (not isinstance(key, str) or not isinstance(name, str)
                or not isinstance(amount, str) or not re.fullmatch(r"-?\d+\.\d{2}", amount)
                or type(quantity) is not int or not isinstance(day, str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
                or not isinstance(stamp, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", stamp)
                or not (note is None or isinstance(note, str)) or all_null is not None):
            raise ValueError("TYPED_CHAIN_SCALAR_FAMILY_OR_PRECISION")
        # SQL Server FOR JSON emits BIT as JSON true/false; integers are rejected.
        if type(active) is not bool:
            raise ValueError("TYPED_CHAIN_BOOLEAN_FAMILY")
        parsed = datetime.fromisoformat(stamp).astimezone(UTC)
        result.append([key, name, str(Decimal(amount).quantize(Decimal("0.01"))), quantity,
                       day, parsed.isoformat(timespec="microseconds").replace("+00:00", "Z"), active, note, None])
    return sorted(result, key=lambda row: row[0])


def normalize_canonical_rows(rows):
    """Canonical Parquet is String/Null; parse declared INT64/BOOLEAN independently."""
    parsed = []
    for source in rows:
        if not isinstance(source, list) or len(source) != 9:
            raise ValueError("TYPED_CHAIN_ROW_WIDTH")
        row = list(source)
        if any(value is not None and type(value) is not str for value in row):
            raise ValueError("TYPED_CHAIN_CANONICAL_SCALAR_FAMILY")
        if not isinstance(row[3], str) or not re.fullmatch(r"[+-]?\d+", row[3]):
            raise ValueError("TYPED_CHAIN_CANONICAL_INT64")
        row[3] = int(row[3])
        if not -(2**63) <= row[3] < 2**63:
            raise ValueError("TYPED_CHAIN_CANONICAL_INT64")
        if not isinstance(row[6], str) or row[6].casefold() not in {"true", "false"}:
            raise ValueError("TYPED_CHAIN_CANONICAL_BOOLEAN")
        row[6] = row[6].casefold() == "true"
        parsed.append(row)
    return normalize_rows(parsed)


def content_hash(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def verify_profile(runner, api, checks, version_id, phase, expected_rows, *, renamed=False):
    profile = api.get(f"/api/v1/dataset-versions/{version_id}/profile")
    names = [SOURCE_NAMES.get(name, name) if renamed else name for name in column_names(runner)]
    schema = [{"name": column["name"], "logical_type": column["logical_type"],
               "semantic_tag": column.get("semantic_tag")} for column in profile["schema"]]
    expected_schema = [{"name": name, "logical_type": kind,
                        "semantic_tag": "IDENTIFIER" if index == 0 else None}
                       for index, (name, kind) in enumerate(zip(names, TYPES, strict=True))]
    checks.verify(schema == expected_schema and profile["row_count"] == 3
                  and profile["column_count"] == 9 and profile["sampled_rows"] == 3
                  and profile["schema"][-1]["nullable"] is True
                  and any(column["name"] == "all_null_int" and column["null_count"] == 3
                          and column["distinct_count"] == 0 and column["null_rate"] == 1
                          for column in profile["profile"]["columns"])
                  and profile["sample_limited"] is False,
                  f"R085-03 {phase}: esquema declarado y etiquetas exactas en población completa")
    actual = normalize_canonical_rows([[row[name] for name in names] for row in profile["sample"]])
    checks.verify(actual == normalize_rows(expected_rows), f"R085-03 {phase}: nueve columnas y tres filas exactas")
    canonical = next(artifact for artifact in profile["artifacts"] if artifact["id"] == profile["canonical_artifact_id"])
    return {"status": "PASS", "phase": phase, "version_id": profile["id"],
            "dataset_id": profile["dataset_id"], "rows": 3, "columns": 9,
            "sha256": profile["sha256"], "schema_hash": profile["schema_hash"],
            "canonical_artifact_id": canonical["id"], "canonical_sha256": canonical["sha256"],
            "schema": schema, "logical_values_sha256": content_hash(actual),
            "all_null_int_rows": 3,
            "source_run_id": profile["source_run_id"], "parent_version_id": profile["parent_version_id"]}


def intake(runner, api, checks, contract, version_id, engine, phase):
    queued = api.post("/api/v1/intake/runs", {"contract_id": contract["id"],
                      "dataset_version_id": version_id, "requested_engine": engine}, expected=(202,))
    complete = runner.wait_for_run(api, queued["id"], timeout=300)
    metrics, plan = complete.get("metrics") or {}, complete.get("execution_plan") or {}
    checks.verify(complete["status"] == "SUCCESS" and complete["decision"] == "APPROVED"
                  and plan.get("engine") == engine and plan.get("requested_engine") == engine
                  and all(type(metrics.get(key)) is int and metrics[key] == 3 for key in (
                      "total_rows", "processed_rows", "valid_rows", "output_rows", "validation_coverage_rows"))
                  and all(metrics.get(key) == 0 for key in ("error_rows", "warning_rows", "discarded_rows")),
                  f"R085-03 {phase}: aprobación estricta real {engine}, cobertura 3/3")
    runtime = plan.get("runtime") or {}
    if engine == "PYSPARK":
        parameters, budget = runtime.get("effective_parameters", {}), runtime.get("resource_budget", {})
        checks.verify(runtime.get("engine") == engine and runtime.get("engine_version") == "4.0.3"
                      and plan.get("spark_memory_budget_bytes") == 2048 * 1024**2
                      and runtime.get("master") == "local[1]" and runtime.get("deployment_mode") == "LOCAL"
                      and str(runtime.get("application_id", "")).startswith("local-")
                      and all(parameters.get(key) == value for key, value in {
                          "spark.driver.memory": "768m", "spark.executor.memory": "768m",
                          "spark.executor.cores": "1", "spark.cores.max": "1",
                          "spark.sql.shuffle.partitions": "1", "spark.default.parallelism": "1"}.items())
                      and all(budget.get(key) == value for key, value in {
                          "master": "local[1]", "driver_memory_mb": 768, "executor_memory_mb": 768,
                          "executor_cores": 1, "total_cores": 1, "partitions": 1}.items()),
                      f"R085-03 {phase}: JVM PySpark real y configuración finita comprobada")
    evidence = api.get(f"/api/v1/runs/{complete['id']}/evidence")
    checks.verify(evidence.get("processing", {}).get("engine") == engine,
                  f"R085-03 {phase}: manifest acredita el motor ejecutado")
    return complete, {"status": "PASS", "phase": phase, "run_id": complete["id"],
                      "input_version_id": version_id, "output_version_id": complete["output_version_id"],
                      "engine": engine, "decision": complete["decision"],
                      "spark_memory_budget_bytes": plan.get("spark_memory_budget_bytes"),
                      "coverage_rows": metrics["validation_coverage_rows"], "runtime": runtime}


def report_draft(dataset_id, contract_id, names):
    return {"mode": "SQL", "sources": [{"alias": "a", "input_dataset_id": dataset_id,
             "contract_id": contract_id, "contract_revision_ids": [contract_id], "policy": "LATEST_APPROVED"}],
            "joins": [], "columns": [], "order_by": [], "parameters": [],
            "sql": "SELECT " + ",".join(f"a.{name} AS {SOURCE_NAMES.get(name, name)}" for name in names)
            + " FROM a ORDER BY a.record_id"}


def certify_sources(runner, api, checks, worker_cgroup):
    rows = fixture_rows(runner)
    macro = api.post("/api/v1/catalog/macrodomains", {"name": "Tipos 085 " + uuid.uuid4().hex[:8]})
    domain = api.post("/api/v1/catalog/domains", {"name": "Delivery tipos", "macro_domain_id": macro["id"]})
    governance = {"macro_domain_id": macro["id"], "domain_id": domain["id"], "information_classification": "INTERNAL"}
    cases = []
    for engine in ENGINES:
        dataset = api.post("/api/v1/datasets", {"name": f"Cadena tipos 085 {engine}"})
        api.request("PATCH", f"/api/v1/datasets/{dataset['id']}/governance",
                    {"expected_version": dataset["governance_version"], **governance})
        overrides = {name: {"logical_type": kind, **({"semantic_tag": "IDENTIFIER"} if index == 0 else {})}
                     for index, (name, kind) in enumerate(zip(column_names(runner), TYPES, strict=True))}
        upload = api.upload(f"/api/v1/datasets/{dataset['id']}/versions/upload", f"typed-{engine}.csv",
                            runner.smoke.csv_bytes(column_names(runner), rows),
                            fields={"column_overrides": json.dumps(overrides)})
        phases = [verify_profile(runner, api, checks, upload["id"], "UPLOAD", rows)]
        config = {"required_columns": ["record_id"], "unique_columns": ["record_id"], "max_error_rate": 0}
        contract = api.post("/api/v1/intake/contracts", {"name": f"Tipos originales {engine}",
                            "dataset_id": dataset["id"], "config": config})
        approved, first = intake(runner, api, checks, contract, upload["id"], engine, "INTAKE_1")
        phases.append(verify_profile(runner, api, checks, approved["output_version_id"], "INTAKE_1", rows))
        checks.verify(phases[-1]["parent_version_id"] == upload["id"]
                      and phases[-1]["source_run_id"] == approved["id"], "R085-03: primera salida enlaza entrada y aprobación exactas")
        context = api.post("/api/v1/reports/resolve", {"draft": report_draft(dataset["id"], contract["id"], column_names(runner))}, expected=(200,))
        bound = context["sources"]
        checks.verify(len(bound) == 1 and bound[0]["input_version_id"] == upload["id"]
                      and bound[0]["output_version_id"] == approved["output_version_id"]
                      and bound[0]["approval_run_id"] == approved["id"], "R085-03: Reportes congela la salida aprobada exacta")
        publication = {"context_id": context["context_id"], "idempotency_key": "typed-chain-" + uuid.uuid4().hex,
                       "name": f"Reporte tipos {engine}", **governance}
        generated = api.post("/api/v1/reports/datasets", publication, expected=(200,))
        checks.verify(api.post("/api/v1/reports/datasets", publication, expected=(200,))["id"] == generated["id"],
                      "R085-03: generación idempotente publica un único dataset")
        report = runner.smoke.wait_until("el dataset Reportes tipado", lambda generated=generated: api.get(
            f"/api/v1/reports/executions/{generated['id']}"),
            lambda current: current["status"] in {"SUCCESS", "FAILED", "CANCELLED"}, timeout=180, interval=.5)
        checks.verify(report["status"] == "SUCCESS" and report["output_version_id"] not in {
            upload["id"], approved["output_version_id"]}, "R085-03: Reportes publica una nueva versión completa")
        phases.append(verify_profile(runner, api, checks, report["output_version_id"], "REPORT_DATASET", rows, renamed=True))
        second_contract = api.post("/api/v1/intake/contracts", {"name": f"Tipos reporte {engine}",
                                    "dataset_id": report["output_dataset_id"], "config": config})
        pending_query = report_draft(report["output_dataset_id"], second_contract["id"],
                                    [SOURCE_NAMES.get(name, name) for name in column_names(runner)])
        rejection = api.request("POST", "/api/v1/reports/resolve", {"draft": pending_query}, expected=(422,)).json()
        checks.verify(rejection["error"]["code"] == "REPORT_NO_STRICT_APPROVAL", "R085-03: dataset generado no hereda aprobación")
        second, last = intake(runner, api, checks, second_contract, report["output_version_id"], engine, "INTAKE_2")
        phases.append(verify_profile(runner, api, checks, second["output_version_id"], "INTAKE_2", rows, renamed=True))
        checks.verify(phases[-1]["parent_version_id"] == report["output_version_id"]
                      and phases[-1]["source_run_id"] == second["id"], "R085-03: segunda salida enlaza la versión publicada exacta")
        downstream = api.post("/api/v1/reports/resolve", {"draft": pending_query}, expected=(200,))["sources"]
        checks.verify(len(downstream) == 1 and downstream[0]["input_version_id"] == report["output_version_id"]
                      and downstream[0]["output_version_id"] == second["output_version_id"]
                      and downstream[0]["approval_run_id"] == second["id"], "R085-03: segunda aprobación es estricta y resoluble")
        for phase in phases:
            again = api.get(f"/api/v1/dataset-versions/{phase['version_id']}/profile")
            canonical = next((artifact for artifact in again["artifacts"]
                              if artifact["id"] == again["canonical_artifact_id"]), {})
            checks.verify(again["sha256"] == phase["sha256"] and again["schema_hash"] == phase["schema_hash"]
                          and again["canonical_artifact_id"] == phase["canonical_artifact_id"]
                          and canonical.get("sha256") == phase["canonical_sha256"],
                          "R085-03: bytes y esquema de cada versión de origen permanecen inmutables")
        cases.append({"status": "PASS", "engine": engine, "phases": phases, "intakes": [first, last],
                      "report_execution_id": report["id"], "context_id": context["context_id"],
                      "report_input_version_id": upload["id"], "report_source_version_id": approved["output_version_id"],
                      "report_version_id": report["output_version_id"], "final_version_id": second["output_version_id"],
                      "renamed_columns": dict(SOURCE_NAMES), "generated_approval_inherited": False,
                      "source_versions_unchanged": True, "logical_values_sha256": content_hash(normalize_rows(rows))})
    return {"status": "PASS", "requirement": "R085-03", "scope": "REAL_ALL_TYPES_UPLOAD_INTAKE_REPORT_DATASET_INTAKE_DELIVERY",
            "rows": 3, "columns": 9, "worker_cgroup": worker_cgroup, "cases": cases}


def certify_destination(runner, api, checks, sink, destination, chain, credentials, run):
    results = []
    expected = normalize_rows(fixture_rows(runner))
    for case in chain["cases"]:
        table = "typed_085_" + case["engine"].lower()
        locator = f'"existing_delivery"."{table}"' if sink == "POSTGRESQL" else f"[existing_delivery].[{table}]"
        draft = runner.delivery_draft(case["final_version_id"], destination, mode="CREATE_TABLE",
                    schema_name="existing_delivery", table_name=table, strategy="CREATE_AND_LOAD",
                    primary_key_columns=["record_id"], source_names=SOURCE_NAMES)
        null_mapping = {"source_name": "all_null_int", "target_name": "all_null_int", "target_type": "INT64", "ordinal": 8, "nullable": True}
        draft["columns"].append(null_mapping)
        delivered = runner.publish_and_run(api, checks, draft, f"R085-03 {case['engine']} {sink}", credentials, expected_columns=9)
        source = case["phases"][-1]
        checks.verify(delivered["dataset_version_id"] == source["version_id"]
                      and delivered["source_sha256"] == source["sha256"]
                      and delivered["canonical_artifact_id"] == source["canonical_artifact_id"]
                      and delivered["canonical_sha256"] == source["canonical_sha256"],
                      f"R085-03 {sink}: receipt remota acredita identidad y hashes de la segunda salida aprobada")
        metadata = api.get(f"/api/v1/delivery/destinations/{destination['id']}/table-metadata?schema_name=existing_delivery&table_name={table}")
        columns = metadata["columns"]
        primary = [constraint for constraint in metadata["constraints"] if constraint["type"] == "PRIMARY_KEY"]
        checks.verify([column["name"] for column in columns] == column_names(runner)
                      and [column["logical_type"] for column in columns] == TYPES
                      and len(primary) == 1 and primary[0]["columns"] == ["record_id"]
                      and all(column["nullable"] == mapping["nullable"] for column, mapping in zip(columns, [*runner.COLUMN_MAPPING, null_mapping], strict=True))
                      and columns[2]["precision"] == 18 and columns[2]["scale"] == 2
                      and columns[5]["datetime_precision"] == 6,
                      f"R085-03 {sink}: tipos nativos, orden, NULL, precisión y PK exactos")
        if sink == "POSTGRESQL":
            sql = f"SELECT jsonb_agg(jsonb_build_array(record_id,customer_name,amount::text,quantity,to_char(happened_on,'YYYY-MM-DD'),to_char(updated_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),is_active,optional_note,all_null_int) ORDER BY record_id)::text FROM {locator};"
            index_sql = f"SELECT COUNT(*) FROM pg_index WHERE indrelid='existing_delivery.{table}'::regclass;"
        else:
            sql = f"SELECT record_id,customer_name,CONVERT(varchar(50),amount) AS amount,quantity,CONVERT(varchar(10),happened_on,23) AS happened_on,CONVERT(varchar(40),SWITCHOFFSET(updated_at,'+00:00'),127) AS updated_at,is_active,optional_note,all_null_int FROM {locator} ORDER BY record_id FOR JSON PATH,INCLUDE_NULL_VALUES;"
            index_sql = f"SELECT COUNT(*) FROM sys.indexes WHERE object_id=OBJECT_ID(N'existing_delivery.{table}') AND index_id>0;"
        raw = json.loads("".join(line.strip() for line in runner.target_sql(run, sink, sql, capture=True, json_oracle=True).splitlines() if line.strip()))
        rows = [[row[name] for name in column_names(runner)] for row in raw] if sink == "SQLSERVER" else raw
        actual = normalize_rows(rows)
        indexes = int(runner.target_scalar(run, sink, index_sql))
        checks.verify(actual == expected and len(actual) == 3 and indexes == 1,
                      f"R085-03 {case['engine']} → {sink}: oráculo SQL independiente confirma 27 valores y único índice PK")
        results.append({"status": "PASS", "engine": case["engine"], "sink_type": sink,
                        "input_version_id": case["final_version_id"], "run": delivered,
                        "rows": 3, "columns": 9, "values_compared": 27, "backing_indexes": indexes,
                        "primary_key_columns": ["record_id"], "logical_values_sha256": content_hash(actual),
                        "native_schema": columns, "table": table, "source_mapping": draft["columns"]})
    return {"status": "PASS", "cases": results}
