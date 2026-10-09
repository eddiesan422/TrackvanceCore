import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.common import EvidenceError, load_manifest
from scripts.ci.validators import (
    SPARK_REAL_CASES,
    integrity,
    native_fixture_restore,
    validate_content,
    validate_junit,
)


@pytest.mark.parametrize("spec", [s for g in load_manifest()["groups"] for s in g["scenarios"]], ids=lambda s: s["id"])
def test_no_scenario_can_be_certified_by_a_standalone_pass_flag(spec):
    with pytest.raises((EvidenceError, KeyError, TypeError)):
        validate_content(spec, {"status": "PASS"})


def test_full_population_hash_cannot_hide_sample_or_physical_number_loss():
    digest = "a" * 64
    with pytest.raises(EvidenceError, match="INCOMPLETE_POPULATION"):
        integrity({"rows": 100, "canonical_rows_sha256": digest}, 1000000, digest)
    with pytest.raises(EvidenceError, match="POPULATION_ORACLE_MISMATCH"):
        integrity({"rows": 1000000, "canonical_rows_sha256": "b" * 64}, 1000000, digest)
    with pytest.raises(EvidenceError, match="PHYSICAL_NUMBERING_MISMATCH"):
        integrity({"rows": 1000000, "canonical_rows_sha256": digest}, 1000000, digest, numbering=True)


def test_skips_only_allowed_for_exact_dedicated_spark_opt_in_reason():
    name = "test_real_spark_job_group_cancellation_stops_computation"
    case = {"id": "tests.test_spark_engine." + name, "classname": "tests.test_spark_engine", "name": name,
            "failure": False, "error": False, "skip": "NOT_RUN_OPT_IN: dedicated suite requires TRACKVANCE_SPARK_TESTS=1"}
    validate_junit({"cases": [case]}, allow_spark_opt_in=True)
    with pytest.raises(EvidenceError, match="UNEXPECTED_TEST_SKIP"):
        validate_junit({"cases": [case]})
    case["skip"] = "strace unavailable"
    with pytest.raises(EvidenceError, match="UNEXPECTED_TEST_SKIP"):
        validate_junit({"cases": [case]}, allow_spark_opt_in=True)


def test_new_skips_cannot_impersonate_the_known_opt_in_spark_reason():
    case = {"id": "tests.test_spark_engine.new_case", "classname": "tests.test_spark_engine", "name": "new_case",
            "failure": False, "error": False, "skip": "NOT_RUN_OPT_IN: dedicated suite requires TRACKVANCE_SPARK_TESTS=1"}
    with pytest.raises(EvidenceError, match="UNEXPECTED_TEST_SKIP"):
        validate_junit({"cases": [case]}, allow_spark_opt_in=True)


def spark_backend_documents():
    cases = [{"id": f"tests.{module}.{name}", "classname": f"tests.{module}", "name": name,
              "failure": False, "error": False, "skip": None} for module, name in sorted(SPARK_REAL_CASES)]
    cases += [{"id": f"tests.ordinary.case_{index}", "classname": "tests.ordinary", "name": f"case_{index}",
               "failure": False, "error": False, "skip": None} for index in range(1800)]
    return {"documents": {"checks": {"checks": [{"name": "unit-tests", "status": "PASS", "exit_code": 0,
                                               "duration_seconds": 1}]}, "junit": {"cases": cases}}}


@pytest.mark.parametrize("case", sorted(SPARK_REAL_CASES))
def test_functional_backend_requires_every_real_spark_case_even_with_sufficient_unit_coverage(case):
    result = spark_backend_documents()
    spec = {"validator": "backend-check", "check": "unit-tests"}
    validate_content(spec, result, execution_profile="functional")
    result["documents"]["junit"]["cases"] = [value for value in result["documents"]["junit"]["cases"]
        if (value["classname"].split(".")[-1], value["name"]) != case]
    with pytest.raises(EvidenceError, match="SPARK_FUNCTIONAL_COVERAGE_MISSING"):
        validate_content(spec, result, execution_profile="functional")


@pytest.mark.parametrize("damage,code", [("skip", "SPARK_FUNCTIONAL_CASE_SKIPPED"),
                                        ("failure", "TEST_FAILURE_OR_ERROR"), ("error", "TEST_FAILURE_OR_ERROR")])
def test_functional_backend_rejects_skipped_or_failed_real_spark_publication(damage, code):
    result = spark_backend_documents()
    case = next(value for value in result["documents"]["junit"]["cases"]
                if value["classname"] == "tests.test_spark_service_e2e")
    case[damage] = "NOT_RUN_OPT_IN: real local Spark publication suite" if damage == "skip" else True
    with pytest.raises(EvidenceError, match=code):
        validate_content({"validator": "backend-check", "check": "unit-tests"}, result, execution_profile="functional")


def test_deep_backend_retains_dedicated_spark_opt_in_contract():
    result = spark_backend_documents()
    for case in result["documents"]["junit"]["cases"]:
        if case["classname"] == "tests.test_spark_engine":
            case["skip"] = "NOT_RUN_OPT_IN: dedicated suite requires TRACKVANCE_SPARK_TESTS=1"
        elif case["classname"] == "tests.test_spark_service_e2e":
            case["skip"] = "NOT_RUN_OPT_IN: real local Spark publication suite"
    validate_content({"validator": "backend-check", "check": "unit-tests"}, result, execution_profile="deep")


def test_browser_requires_actual_cases_and_zero_skip_or_flakiness():
    from scripts.ci.validators import browser
    with pytest.raises(EvidenceError, match="BROWSER_EMPTY"):
        browser({"status": "PASS", "expected": 0, "skipped": 0, "unexpected": 0, "flaky": 0})
    with pytest.raises(EvidenceError, match="BROWSER_SKIP_FAILURE_OR_FLAKY"):
        browser({"status": "PASS", "expected": 1, "skipped": 0, "unexpected": 0, "flaky": 1})


def typed_delivery_documents(destinations):
    """Synthetic validator fixtures are not claimed as native chain evidence."""
    names = ["record_id", "customer_name", "amount", "quantity", "happened_on", "updated_at", "is_active", "optional_note", "all_null_int"]
    types = ["STRING", "STRING", "DECIMAL", "INT64", "DATE", "TIMESTAMP", "BOOLEAN", "STRING", "INT64"]
    cases = []
    for engine in ("POLARS", "PYSPARK"):
        phases = [{"status": "PASS", "phase": phase, "version_id": engine + str(index), "dataset_id": engine + "dataset",
                   "canonical_artifact_id": engine + "artifact" + str(index), "canonical_sha256": "b" * 64,
                   "sha256": "b" * 64, "schema_hash": "c" * 64, "logical_values_sha256": "a" * 64,
                   "rows": 3, "columns": 9, "all_null_int_rows": 3,
                   "schema": [{"name": "preserved_note" if name == "optional_note" and index >= 2 else name,
                               "logical_type": kind, "semantic_tag": "IDENTIFIER" if position == 0 else None}
                              for position, (name, kind) in enumerate(zip(names, types, strict=True))]}
                  for index, phase in enumerate(("UPLOAD", "INTAKE_1", "REPORT_DATASET", "INTAKE_2"))]
        runtime = {"engine": "PYSPARK", "engine_version": "4.0.3", "master": "local[1]", "deployment_mode": "LOCAL",
                   "application_id": "local-real-application", "effective_parameters": {
                       "spark.driver.memory": "768m", "spark.executor.memory": "768m", "spark.executor.cores": "1",
                       "spark.cores.max": "1", "spark.sql.shuffle.partitions": "1", "spark.default.parallelism": "1"}}
        intakes = []
        for index in range(2):
            source, output = phases[index * 2:index * 2 + 2]
            run_id = engine + "run" + str(index)
            output.update(parent_version_id=source["version_id"], source_run_id=run_id)
            intakes.append({"status": "PASS", "phase": f"INTAKE_{index + 1}", "engine": engine, "decision": "APPROVED",
                            "coverage_rows": 3, "run_id": run_id, "input_version_id": source["version_id"],
                            "spark_memory_budget_bytes": 2048 * 1024**2 if engine == "PYSPARK" else None,
                            "output_version_id": output["version_id"], "runtime": runtime if engine == "PYSPARK" else {}})
        case = {"status": "PASS", "engine": engine, "phases": phases, "intakes": intakes,
                "logical_values_sha256": "a" * 64, "report_input_version_id": phases[0]["version_id"],
                "report_source_version_id": phases[1]["version_id"], "report_version_id": phases[2]["version_id"],
                "final_version_id": phases[3]["version_id"], "context_id": engine + "context",
                "report_execution_id": engine + "report", "source_versions_unchanged": True,
                "generated_approval_inherited": False, "renamed_columns": {"optional_note": "preserved_note"}}
        cases.append(case)
        for destination in destinations:
            destination.setdefault("typed_chain", {"status": "PASS", "cases": []})["cases"].append({
                "status": "PASS", "engine": engine, "sink_type": destination["sink_type"],
                "input_version_id": case["final_version_id"], "rows": 3, "columns": 9, "values_compared": 27,
                "backing_indexes": 1, "primary_key_columns": ["record_id"], "logical_values_sha256": "a" * 64,
                "run": {"status": "COMMITTED", "dataset_version_id": phases[3]["version_id"],
                        "source_sha256": phases[3]["sha256"], "canonical_artifact_id": phases[3]["canonical_artifact_id"],
                        "canonical_sha256": phases[3]["canonical_sha256"]}, "native_schema": [
                    {"name": name, "logical_type": kind, "nullable": index in {2, 7, 8},
                     "precision": 18 if index == 2 else None, "scale": 2 if index == 2 else None,
                     "datetime_precision": 6 if index == 5 else None}
                    for index, (name, kind) in enumerate(zip(names, types, strict=True))],
                "source_mapping": [{"source_name": "preserved_note" if name == "optional_note" else name,
                                    "target_name": name} for name in names]})
    return {"status": "PASS", "requirement": "R085-03", "scope": "REAL_ALL_TYPES_UPLOAD_INTAKE_REPORT_DATASET_INTAKE_DELIVERY",
            "rows": 3, "columns": 9, "cases": cases, "worker_cgroup": {"service": "worker",
                "memory_limit_bytes": 2048 * 1024**2, "nano_cpus": 10**9, "pids_limit": 512}}


def delivery_documents():
    cases = [{"status": "PASS", "table": table, "primary_key_columns": keys, "rows": 3,
              "backing_indexes": 1, "not_null": True, "existing_strategies_preserve_pk": True}
             for table, keys in (("pk_simple_085", ["quantity"]),
                                 ("pk_composite_085", ["transaction_code", "quantity"]))]
    cases += [{"case": case, "status": "REJECTED_PREFLIGHT"}
              for case in ("NULL_BEYOND_PREVIEW", "REPEATED_POPULATION", "NATIVE_KEY_BYTES", "COLLATION")]
    cases.append({"case": "EXPLICIT_NONE", "status": "PASS", "primary_key_mode": "NONE"})
    result = {"documents": {"browser": {"status": "PASS", "expected": 2, "skipped": 0,
                                         "unexpected": 0, "flaky": 0}, "result": {
        "status": "PASS", "destinations": [{"status": "PASS", "sink_type": sink,
            "runs": [1, 2, 3, 4], "audit_columns": {"status": "PASS"},
            "primary_keys": {"status": "PASS", "requirement": "R085-02", "cases": [dict(case) for case in cases]}}
            for sink in ("POSTGRESQL", "SQLSERVER")],
        "integral": {"status": "PASS", "scope": "R085_INTEGRAL", "remote_sql": {"rows": 3,
            "backing_indexes": 1, "client_codes": "001,002,003"}, "browser": {"person_id": "person-1",
            "excel": {"crc": "PASS"}, "delivery": {"decision": "COMMITTED"}}},
        "controlled_ack_loss": {"status": "PASS", "automatic_replay": False},
        "playwright": "PASS", "postgres_metrics_matrix": {"status": "PASS"}}}}
    value = result["documents"]["result"]
    value["typed_chain"] = typed_delivery_documents(value["destinations"])
    value["resource_profile"] = {"status": "PASS", "memory_bytes": 9728 * 1024**2, "cpus": 2.8,
                                 "intake_worker_memory_mib": 2048, "intake_worker_cpus": 1}
    return result


@pytest.mark.parametrize("damage,code", [
    ("no-pk", "CONTENT_NOT_PASS"),
    ("wrong-key-order", "DELIVERY_PRIMARY_KEY_COVERAGE_MISSING"),
    ("duplicate-index", "DELIVERY_PRIMARY_KEY_COVERAGE_MISSING"),
    ("nullable", "DELIVERY_PRIMARY_KEY_COVERAGE_MISSING"),
    ("existing-altered", "DELIVERY_PRIMARY_KEY_COVERAGE_MISSING"),
    ("negative-passed", "DELIVERY_PRIMARY_KEY_COVERAGE_MISSING"),
    ("no-none", "DELIVERY_PRIMARY_KEY_COVERAGE_MISSING"),
    ("no-collation", "DELIVERY_PRIMARY_KEY_COLLATION_MISSING"),
    ("no-integral", "CONTENT_NOT_PASS"),
    ("wrong-leading-zero", "DELIVERY_INTEGRAL_085_MISSING"),
    ("no-person", "DELIVERY_INTEGRAL_085_MISSING"),
    ("bad-excel", "DELIVERY_INTEGRAL_085_MISSING"),
    ("uncommitted", "DELIVERY_INTEGRAL_085_MISSING"),
    ("skip-browser", "BROWSER_SKIP_FAILURE_OR_FLAKY"),
])
def test_delivery_gate_requires_native_pk_and_entire_integral(damage, code):
    result = delivery_documents()
    spec = {"validator": "delivery"}
    validate_content(spec, result)
    value = result["documents"]["result"]
    primary = value["destinations"][1]["primary_keys"]
    composite = primary["cases"][1]
    if damage == "no-pk":
        value["destinations"][1].pop("primary_keys")
    elif damage == "wrong-key-order":
        composite["primary_key_columns"] = ["quantity", "transaction_code"]
    elif damage == "duplicate-index":
        composite["backing_indexes"] = 2
    elif damage == "nullable":
        composite["not_null"] = False
    elif damage == "existing-altered":
        composite["existing_strategies_preserve_pk"] = False
    elif damage == "negative-passed":
        primary["cases"][2]["status"] = "PASS"
    elif damage == "no-none":
        primary["cases"] = [case for case in primary["cases"] if case.get("case") != "EXPLICIT_NONE"]
    elif damage == "no-collation":
        primary["cases"] = [case for case in primary["cases"] if case.get("case") != "COLLATION"]
    elif damage == "no-integral":
        value.pop("integral")
    elif damage == "wrong-leading-zero":
        value["integral"]["remote_sql"]["client_codes"] = "1,2,3"
    elif damage == "no-person":
        value["integral"]["browser"].pop("person_id")
    elif damage == "bad-excel":
        value["integral"]["browser"]["excel"]["crc"] = "FAIL"
    elif damage == "uncommitted":
        value["integral"]["browser"]["delivery"]["decision"] = "UNKNOWN"
    else:
        result["documents"]["browser"]["skipped"] = 1
    with pytest.raises(EvidenceError, match=code):
        validate_content(spec, result)


@pytest.mark.parametrize("damage,code", [
    ("missing", "DELIVERY_TYPED_CHAIN_MISSING"), ("engine", "DELIVERY_TYPED_CHAIN_ENGINES"),
    ("worker_memory", "DELIVERY_TYPED_CHAIN_RESOURCES"), ("worker_cpu", "DELIVERY_TYPED_CHAIN_RESOURCES"),
    ("phase_missing", "DELIVERY_TYPED_CHAIN_PHASES"), ("source_changed", "DELIVERY_TYPED_CHAIN_PHASES"),
    ("null_type", "DELIVERY_TYPED_CHAIN_SCHEMA_OR_VALUES"), ("null_count", "DELIVERY_TYPED_CHAIN_SCHEMA_OR_VALUES"),
    ("phase_values", "DELIVERY_TYPED_CHAIN_SCHEMA_OR_VALUES"), ("wrong_input", "DELIVERY_TYPED_CHAIN_APPROVAL"),
    ("spark_fake", "DELIVERY_TYPED_CHAIN_REAL_SPARK"), ("spark_unbounded", "DELIVERY_TYPED_CHAIN_REAL_SPARK"),
    ("sql_missing", "DELIVERY_TYPED_CHAIN_DESTINATIONS"), ("sql_values", "DELIVERY_TYPED_CHAIN_SQL_ORACLE"),
    ("sql_wrong_source", "DELIVERY_TYPED_CHAIN_SQL_ORACLE"), ("sql_wrong_type", "DELIVERY_TYPED_CHAIN_SQL_ORACLE"),
])
def test_delivery_gate_rejects_incomplete_or_misbound_real_all_types_chain(damage, code):
    result = delivery_documents()
    spec = {"validator": "delivery"}
    validate_content(spec, result)
    value = result["documents"]["result"]
    chain = value["typed_chain"]
    case = chain["cases"][1]
    delivery = value["destinations"][1]["typed_chain"]["cases"][1]
    if damage == "missing":
        value.pop("typed_chain")
    elif damage == "engine":
        case["engine"] = "POLARS"
    elif damage == "worker_memory":
        chain["worker_cgroup"]["memory_limit_bytes"] = 512 * 1024**2
    elif damage == "worker_cpu":
        chain["worker_cgroup"]["nano_cpus"] = 200000000
    elif damage == "phase_missing":
        case["phases"].pop()
    elif damage == "source_changed":
        case["source_versions_unchanged"] = False
    elif damage == "null_type":
        case["phases"][2]["schema"][-1]["logical_type"] = "STRING"
    elif damage == "null_count":
        case["phases"][2]["all_null_int_rows"] = 2
    elif damage == "phase_values":
        case["phases"][2]["logical_values_sha256"] = "d" * 64
    elif damage == "wrong_input":
        case["intakes"][1]["input_version_id"] = case["report_input_version_id"]
    elif damage == "spark_fake":
        case["intakes"][1]["runtime"].pop("application_id")
    elif damage == "spark_unbounded":
        case["intakes"][1]["runtime"]["effective_parameters"]["spark.cores.max"] = "16"
    elif damage == "sql_missing":
        value["destinations"][1]["typed_chain"]["cases"].pop()
    elif damage == "sql_values":
        delivery["logical_values_sha256"] = "d" * 64
    elif damage == "sql_wrong_source":
        delivery["input_version_id"] = case["report_input_version_id"]
    else:
        delivery["native_schema"][-1]["logical_type"] = "STRING"
    with pytest.raises(EvidenceError, match=code):
        validate_content(spec, result)


@pytest.mark.parametrize('damage', ['missing-core', 'duplicate', 'missing-exclusion', 'unmapped-new-spec'])
def test_general_browser_cannot_omit_applicable_specs_or_hide_unknown_opt_ins(tmp_path, monkeypatch, damage):
    from scripts.ci import validators

    spec = next(s for g in load_manifest()['groups'] for s in g['scenarios'] if s['validator'] == 'compose-browser')
    optional = {
        'automation.spec.ts': ('TV_AUTOMATION_E2E', 'async-volume-100'),
        'connections.spec.ts': ('TV_CONNECTIONS_E2E', 'connections'),
        'corrections-volume.spec.ts': ('TV_CORRECTIONS_E2E', 'corrections-browser'),
        'delivery.spec.ts': ('TV_DELIVERY_E2E', 'delivery'),
        'demo-access-clean.spec.ts': ('TV_EXPECT_CLEAN_DEMO', 'compose-critical'),
        'reports-download-opfs.spec.ts': ('TV_REPORT_DOWNLOAD_OPFS', 'catalog-reports'),
        'identity-sso.spec.ts': ('TV_IDENTITY_SSO_E2E', 'identity-sso'),
        'roadmap-source-cycle.spec.ts': ('TV_CONNECTIONS_E2E', 'connections'),
        'volume.spec.ts': ('TV_VOLUME_E2E', 'async-volume-100'),
    }
    value = {'selected_spec_files': list(spec['browser_required_specs']),
             'excluded_opt_in_specs': [{'file': 'tests-e2e/' + name, 'required_flag': flag, 'covered_by_group': group}
                                      for name, (flag, group) in sorted(optional.items()) if "tests-e2e/" + name not in spec["browser_required_specs"]]}
    validators.browser_selection(value, spec)
    if damage == 'missing-core':
        value['selected_spec_files'].pop()
    elif damage == 'duplicate':
        value['selected_spec_files'].append(value['selected_spec_files'][0])
    elif damage == 'missing-exclusion':
        value['excluded_opt_in_specs'].pop()
    else:
        fixture = tmp_path / 'frontend/tests-e2e'
        fixture.mkdir(parents=True)
        for name in value['selected_spec_files'] + [row['file'] for row in value['excluded_opt_in_specs']]:
            (fixture / Path(name).name).write_text('')
        (fixture / 'new.spec.ts').write_text('')
        monkeypatch.setattr(validators, 'ROOT', tmp_path)
    with pytest.raises(EvidenceError, match='BROWSER_'):
        validators.browser_selection(value, spec)


@pytest.mark.parametrize("field,value,code", [
    ("revision", "0016_acquisition_diagnostics", "NATIVE_FIXTURE_RESTORE_SCHEMA"),
    ("tables", 42, "NATIVE_FIXTURE_RESTORE_SCHEMA"),
    ("automatic_processes_started", True, "NATIVE_FIXTURE_RESTORE_CONSUMERS_OR_MAIN"),
    ("restored_state_sha256", "b" * 64, "NATIVE_FIXTURE_RESTORE_HASH_MISMATCH"),
    ("verified_delivery_secrets", 0, "NATIVE_FIXTURE_RESTORE_ARTIFACT_OR_KEY_FAMILY"),
    ("cleanup_error_type", "RuntimeError", "NATIVE_FIXTURE_RESTORE_ERROR"),
])
def test_native_fixture_restore_requires_schema_exact_hashes_key_families_and_stopped_consumers(field, value, code):
    receipt = {"status": "PASS", "main_inventory": "UNCHANGED", "automatic_processes_started": False,
               "restore": "STOPPED_VERIFIED", "revision": "0019_governance_people", "state_schema_version": 9,
               "tables": 56, "multipart_integrity": "PASS", "historical_bytes_and_hashes": "PASS",
               "exact_state_comparison": "PASS", "source_state_sha256": "a" * 64,
               "restored_state_sha256": "a" * 64, "manifest_sha256": "c" * 64,
               "verified_artifacts": 1, "verified_source_secrets": 1, "verified_delivery_secrets": 1}
    native_fixture_restore(receipt)
    receipt[field] = value
    with pytest.raises(EvidenceError, match=code):
        native_fixture_restore(receipt)


def selective_receipt_fixture():
    from docker_state import CURRENT_STATE_TABLES
    from selective_cleanup_recovery import CHECKS, PROTECTED
    removed = {name: 0 if name in PROTECTED | {"audit_events"} else 1 for name in CURRENT_STATE_TABLES}
    removed["datasets"] = 2
    return {"status": "PASS", "scope": "OWNED_RESTORED_POSTGRES_COPY", "database": "POSTGRESQL",
        "project": "trackvance-v080-test-restore085-012345abcdef", "source_sha": "a" * 40, "schema_version": 9,
        "migration": "0019_governance_people", "tables": 56, "verified_restore": "STOPPED_VERIFIED",
        "verified_restore_project": "trackvance-v080-test-cleanuprestore085-123456abcdef", "backup_manifest_sha256": "b" * 64,
        "verified_state_sha256": "c" * 64, "plan_sha256": "d" * 64, "external_destinations_touched": False,
        "backup_retained": True, "usual_inventory": "UNCHANGED", "reset_audit_added": 1,
        "protected_tables": sorted(PROTECTED), "removed_counts": removed, "quarantined_files": 5,
        "unknown_run_id": "retained-unknown", "protected_dataset_id": "retained-dataset",
        "verified_surviving_artifacts": 3, "protected_secret_volumes": 4, **dict.fromkeys(CHECKS, "PASS")}


def test_selective_cleanup_gate_accepts_complete_native_trial_receipt():
    from scripts.ci.validators import selective_cleanup_receipt
    selective_cleanup_receipt(selective_receipt_fixture(), "a" * 40)


@pytest.mark.parametrize("field,value", [("database", "SQLITE"), ("project", "trackvance-certification"),
    ("source_sha", "e" * 40), ("schema_version", 8), ("verified_restore", "RUNNING_VERIFIED"),
    ("verified_restore_project", "trackvance-certification"), ("backup_manifest_sha256", None),
    ("abrupt_fault_recovery", "NOT_RUN"), ("protected_secret_bytes", "NOT_RUN"), ("external_destinations_touched", True),
    ("backup_retained", False), ("quarantined_files", 0), ("reset_audit_added", 0), ("unknown_run_id", None)])
def test_selective_cleanup_gate_rejects_missing_real_proofs(field, value):
    from scripts.ci.validators import selective_cleanup_receipt
    receipt = selective_receipt_fixture()
    receipt[field] = value
    with pytest.raises(EvidenceError):
        selective_cleanup_receipt(receipt, "a" * 40)
