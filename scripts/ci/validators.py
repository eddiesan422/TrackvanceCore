"""Content checks for real suite evidence, independent of the receipt PASS flag."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    from .common import DIGEST, ROOT, EvidenceError, require
except ImportError:
    from common import DIGEST, ROOT, EvidenceError, require


SPARK_REAL_CASES = {
    ("test_spark_engine", name)
    for name in (
        "test_real_spark_catalog_intake_global_references_and_exact_values",
        "test_real_spark_recon_partitions_decimals_nulls_and_aggregation[None]",
        "test_real_spark_recon_partitions_decimals_nulls_and_aggregation[SOURCE]",
        "test_real_spark_recon_partitions_decimals_nulls_and_aggregation[TARGET]",
        "test_real_spark_sentinel_rules_and_profile_metrics",
        "test_real_spark_global_nulls_conditions_and_collision_free_compound_keys[ALLOW]",
        "test_real_spark_global_nulls_conditions_and_collision_free_compound_keys[FAIL]",
        "test_real_spark_global_nulls_conditions_and_collision_free_compound_keys[IGNORE]",
        "test_real_spark_all_transforms_preserve_original_evidence_and_numbering",
        "test_real_spark_recon_all_comparisons_normalized_compound_keys_and_empty[False]",
        "test_real_spark_recon_all_comparisons_normalized_compound_keys_and_empty[True]",
        "test_real_spark_job_group_cancellation_stops_computation",
        "test_real_spark_global_hot_key_uniqueness_and_reference",
        "test_real_spark_recon_count_aggregation_preserves_lineage[SOURCE]",
        "test_real_spark_recon_count_aggregation_preserves_lineage[TARGET]",
    )
} | {("test_spark_service_e2e", "test_real_local_spark_run_publishes_complete_registered_lineage")}


def junit_document(path: Path) -> dict[str, Any]:
    try:
        cases = list(ET.parse(path).getroot().iter("testcase"))
    except ET.ParseError as error:
        raise EvidenceError("INVALID_JUNIT") from error
    return {"cases": [{"id": f"{c.get('classname', '')}.{c.get('name', '')}",
                       "classname": c.get("classname", ""), "name": c.get("name", ""),
                       "failure": c.find("failure") is not None, "error": c.find("error") is not None,
                       "skip": c.find("skipped").get("message", "") if c.find("skipped") is not None else None}
                      for c in cases]}


def validate_junit(data: dict[str, Any], *, minimum: int = 1, allow_spark_opt_in: bool = False) -> None:
    cases = data.get("cases", [])
    require(isinstance(cases, list) and len(cases) >= minimum, "INSUFFICIENT_TEST_COVERAGE")
    require(len({c.get("id") for c in cases}) == len(cases), "DUPLICATE_TEST_CASE")
    for case in cases:
        require(case.get("failure") is False and case.get("error") is False, "TEST_FAILURE_OR_ERROR")
        if case.get("skip") is not None:
            allowed = {"test_spark_engine": "NOT_RUN_OPT_IN: dedicated suite requires TRACKVANCE_SPARK_TESTS=1",
                       "test_spark_service_e2e": "NOT_RUN_OPT_IN: real local Spark publication suite"}
            module = case.get("classname", "").split(".")[-1]
            spark = (allow_spark_opt_in and (module, case.get("name")) in SPARK_REAL_CASES
                     and case["skip"] == allowed[module])
            powershell = (allow_spark_opt_in and module == "test_reset_local"
                          and case.get("name") == "test_reset_wrapper_resolves_optional_project_before_applying_guard"
                          and case["skip"] == "PowerShell no está disponible")
            require(spark or powershell, "UNEXPECTED_TEST_SKIP")


def passing(value: Any, code: str = "CONTENT_NOT_PASS") -> None:
    require(isinstance(value, dict) and value.get("status") == "PASS", code)


def digest(value: Any) -> None:
    require(isinstance(value, str) and bool(DIGEST.fullmatch(value)), "MISSING_POPULATION_HASH")


def integrity(value: dict[str, Any], rows: int, expected: str | None = None, *, numbering: bool = False) -> None:
    require(isinstance(value, dict) and value.get("rows") == rows, "INCOMPLETE_POPULATION")
    observed = value.get("canonical_rows_sha256", value.get("sha256"))
    digest(observed)
    if expected is not None:
        digest(expected)
        require(observed == expected, "POPULATION_ORACLE_MISMATCH")
    if numbering:
        require(value.get("physical_numbering") == {"rows": rows, "first": 5, "last": rows + 4,
                                                    "numbering_mismatches": 0}, "PHYSICAL_NUMBERING_MISMATCH")


def browser(value: dict[str, Any], expected: int | None = None) -> None:
    passing(value, "BROWSER_NOT_PASS")
    require(type(value.get("expected")) is int and value["expected"] > 0, "BROWSER_EMPTY")
    if expected is not None:
        require(value["expected"] == expected, "BROWSER_SCENARIO_COUNT")
    require(all(value.get(k) == 0 for k in ("skipped", "unexpected", "flaky")), "BROWSER_SKIP_FAILURE_OR_FLAKY")


def browser_selection(value: dict[str, Any], spec: dict[str, Any]) -> None:
    """All applicable specs run; foreign fixtures are covered by dedicated groups."""
    opt_ins = {
        'automation.spec.ts': ('TV_AUTOMATION_E2E', 'async-volume-100'),
        'corrections-volume.spec.ts': ('TV_CORRECTIONS_E2E', 'corrections-browser'),
        'connections.spec.ts': ('TV_CONNECTIONS_E2E', 'connections'),
        'demo-access-clean.spec.ts': ('TV_EXPECT_CLEAN_DEMO', 'compose-critical'),
        'delivery.spec.ts': ('TV_DELIVERY_E2E', 'delivery'),
        'volume.spec.ts': ('TV_VOLUME_E2E', 'async-volume-100'),
        'identity-sso.spec.ts': ('TV_IDENTITY_SSO_E2E', 'identity-sso'),
        'roadmap-source-cycle.spec.ts': ('TV_CONNECTIONS_E2E', 'connections'),
    }
    required = spec['browser_required_specs']
    selected = value.get('selected_spec_files')
    require(isinstance(selected, list) and sorted(selected) == sorted(required)
            and len(set(selected)) == len(selected), 'BROWSER_APPLICABLE_SPEC_SELECTION')
    inventory = {'tests-e2e/' + path.name for path in (ROOT / 'frontend/tests-e2e').glob('*.spec.ts')}
    expected_exclusions = [{'file': 'tests-e2e/' + name, 'required_flag': flag, 'covered_by_group': group}
                           for name, (flag, group) in sorted(opt_ins.items()) if 'tests-e2e/' + name not in required]
    require(value.get('excluded_opt_in_specs') == expected_exclusions
            and inventory == set(required) | {row['file'] for row in expected_exclusions},
            'BROWSER_UNEXPECTED_SPEC_OMISSION')


def full_profile(value: dict[str, Any], fixture: dict[str, Any], rows: int) -> None:
    require(value.get("row_count") == rows, "INCOMPLETE_GLOBAL_PROFILE")
    columns = {item["name"]: item for item in value.get("columns", [])}
    expected = fixture.get("expected_cardinality")
    require(isinstance(expected, dict) and expected, "MISSING_TYPE_ORACLE")
    for name, count in expected.items():
        require(columns.get(name, {}).get("distinct_count") == count, "PROFILE_CARDINALITY_MISMATCH")
    require(columns.get("observed", {}).get("null_count") == fixture.get("expected_observed_nulls"), "NULL_EMPTY_MISMATCH")
    if "late_type" in columns and rows > 100000:
        require(columns["late_type"].get("logical_type") == "STRING", "LATE_TYPE_TRUNCATION")


def chain(value: dict[str, Any], rows: int) -> None:
    fixture = value.get("fixture", {})
    require(fixture.get("rows") == rows, "CHAIN_FIXTURE_ROWS")
    expected = fixture.get("canonical_rows_sha256")
    digest(expected)
    intake, delivery = value.get("intake", {}), value.get("delivery", {})
    integrity(intake.get("accepted", {}), rows, expected)
    require(intake.get("metrics", {}).get("total_rows") == rows, "CHAIN_INTAKE_POPULATION")
    integrity(delivery.get("target", {}), rows, expected)
    require(delivery.get("target_rows") == rows and bool(delivery.get("receipt_artifact_id")), "CHAIN_SQL_RECEIPT_MISSING")
    require(delivery.get("source_version_id") == intake.get("output_version_id") and bool(intake.get("output_version_id")), "CHAIN_WRONG_VERSION")
    require(len(value.get("inbox", {}).get("notifications", [])) >= 3, "CHAIN_INBOX_MISSING")


def restore(value: dict[str, Any], version: str) -> None:
    passing(value, "RESTORE_NOT_PASS")
    require(value.get("source_destroyed_before_restore") is True, "RESTORE_SOURCE_NOT_DESTROYED")
    require(value.get("main_inventory") == "UNCHANGED", "RESTORE_MAIN_CHANGED")
    require(not value.get("cleanup_error_type") and not value.get("error_type"), "RESTORE_CLEANUP_OR_RUNTIME_ERROR")
    if version == "0.8.0":
        require(value.get("state_schema_version") == 8 and value.get("alembic_revision") == "0017_catalog_reports", "RESTORE_SCHEMA_MISMATCH")
        require(value.get("persistence_exact_comparison") == "PASS"
                and value.get("immutable_comparison_before_automatic_processes") == "PASS", "RESTORE_NOT_EXACT")
        passing(value.get("plaintext_backup_scan", {}))
        require(value.get("retained_projects") == [] and value.get("doctor") == "PASS", "RESTORE_NOT_CLEAN_OR_READY")
        require(value.get("verified_source_secrets") == 1 and value.get("verified_delivery_secrets") == 1
                and value.get("verified_artifacts", 0) > 0, "RESTORE_ARTIFACT_OR_KEY_FAMILY_MISSING")
        migration = value.get("postgres_migration", {})
        passing(migration)
        physical = migration.get("0016_0017_preservation", {}).get("physical_schema", {})
        require(physical.get("tables") == 55, "RESTORE_PHYSICAL_SCHEMA")
    else:
        require(value.get("source_version") == version and value.get("target_version") == "0.8.0", "HISTORICAL_RESTORE_VERSION")
        require(value.get("restore") == "STOPPED_VERIFIED" and value.get("automatic_processes_started") is False,
                "RESTORE_CONSUMERS_ACTIVATED")
        require(value.get("exact_historical_state") == "PASS", "HISTORICAL_STATE_NOT_EXACT")
        digest(value.get("source_state_sha256"))
        require(value.get("source_state_sha256") == value.get("restored_legacy_sha256"), "HISTORICAL_STATE_HASH_MISMATCH")
        if version == "0.6.1":
            require(value.get("native_tables") == 55 and value.get("new_catalog_tables_empty") is True, "LEGACY061_NEW_TABLES")
        else:
            require(value.get("migration") == "0017_catalog_reports" and value.get("state_schema_version") == 8,
                    "HISTORICAL_SCHEMA_MISMATCH")
            require(value.get("cleanup") == "PASS" and value.get("log_secret_scan") == "PASS", "HISTORICAL_CLEANUP_OR_SECRETS")


def native_fixture_restore(value: dict[str, Any]) -> None:
    """The native stopped restore used by XLSX/async has a distinct receipt."""
    passing(value, "NATIVE_FIXTURE_RESTORE_NOT_PASS")
    require(value.get("main_inventory") == "UNCHANGED" and value.get("automatic_processes_started") is False
            and value.get("restore") == "STOPPED_VERIFIED", "NATIVE_FIXTURE_RESTORE_CONSUMERS_OR_MAIN")
    require(value.get("revision") == "0017_catalog_reports" and value.get("state_schema_version") == 8
            and value.get("tables") == 55, "NATIVE_FIXTURE_RESTORE_SCHEMA")
    require(all(value.get(k) == "PASS" for k in ("multipart_integrity", "historical_bytes_and_hashes", "exact_state_comparison")),
            "NATIVE_FIXTURE_RESTORE_NOT_EXACT")
    digest(value.get("source_state_sha256"))
    require(value.get("source_state_sha256") == value.get("restored_state_sha256"), "NATIVE_FIXTURE_RESTORE_HASH_MISMATCH")
    digest(value.get("manifest_sha256"))
    require(value.get("verified_artifacts", 0) > 0 and value.get("verified_source_secrets", 0) > 0
            and value.get("verified_delivery_secrets", 0) > 0, "NATIVE_FIXTURE_RESTORE_ARTIFACT_OR_KEY_FAMILY")
    require(not any(value.get(k) for k in ("error_type", "cleanup_error_type", "inventory_error_type")), "NATIVE_FIXTURE_RESTORE_ERROR")


def catalog_population(value: dict[str, Any], rows: int) -> None:
    passing(value)
    require(value.get("rows_per_source") == rows, "CATALOG_POPULATION_REDUCED")
    sources = value.get("sources", [])
    require(len(sources) == 3 and {s.get("alias") for s in sources} == {"a", "b", "c"}, "CATALOG_SOURCE_COVERAGE")
    require(all(s.get("rows") == rows and s.get("status") == "PASS" for s in sources), "CATALOG_SOURCE_INCOMPLETE")
    joins = value.get("joins", [])
    require(len(joins) == 4 and {j.get("type") for j in joins} == {"INNER", "LEFT", "RIGHT", "FULL"}, "CATALOG_JOIN_COVERAGE")
    counts = {"INNER": rows // 2, "LEFT": rows, "RIGHT": rows, "FULL": rows * 3 // 2}
    for item in joins:
        passing(item)
        materialized = item.get("materialized", {})
        passing(materialized)
        require(materialized.get("rows") == counts[item["type"]] and materialized.get("parts", 0) > 0,
                "CATALOG_JOIN_TRUNCATED")
        digest(materialized.get("sha256"))
    triple = value.get("three_sources", {})
    passing(triple)
    integrity(triple, rows)
    for kind, limit in (("csv", 100000), ("xlsx", 50000)):
        exported = value.get(kind, {})
        passing(exported)
        require(exported.get("rows") == min(counts["INNER"], limit) and exported.get("max_rows") == limit,
                "CATALOG_EXPORT_POPULATION")
        require(exported.get("generation") == "COMPLETE" and exported.get("transmission") == "COMPLETE", "CATALOG_EXPORT_INTERRUPTED")
        digest(exported.get("sha256"))
        if rows > 200000:
            rejected = value.get(kind + "_above_limit", {})
            passing(rejected)
            require(rejected.get("requested_rows") == limit + 1 and rejected.get("execution_status") == "FAILED"
                    and rejected.get("error_code") == "REPORT_RESULT_LIMIT", "CATALOG_EXPORT_LIMIT_NEGATIVE_MISSING")
    require(value.get("parent_block_propagation") == "PASS", "DERIVED_AUTHORIZATION_MISSING")
    if rows == 120:
        passing(value.get("bounded_many_to_many", {}))
    else:
        passing(value.get("many_to_many_expansion_rejected", {}))


def valid_fingerprint(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"method", "rows", "sum_sha256", "xor_sha256", "sha256"}:
        return False
    if value["method"] != "CANONICAL_JSON_ROW_SHA256_COUNT_SUM_XOR_V1" or type(value["rows"]) is not int or not 0 <= value["rows"] < 2**64:
        return False
    if not all(isinstance(value[k], str) and DIGEST.fullmatch(value[k]) for k in ("sum_sha256", "xor_sha256", "sha256")):
        return False
    encoded = value["rows"].to_bytes(8, "big") + bytes.fromhex(value["sum_sha256"]) + bytes.fromhex(value["xor_sha256"])
    return hashlib.sha256(encoded).hexdigest() == value["sha256"]


def validate_content(spec: dict[str, Any], result: dict[str, Any]) -> None:
    """No profile succeeds on a standalone {status: PASS}."""
    require(isinstance(result, dict), "INVALID_CONTENT")
    documents = result.get("documents", {})
    profile = spec["validator"]
    rows = spec.get("rows")
    value = documents.get("result", result)
    if profile in {"backend-check", "frontend-check"}:
        checks = documents.get("checks", {}).get("checks", [])
        selected = [c for c in checks if c.get("name") == spec["check"]]
        require(len(selected) == 1 and selected[0].get("status") == "PASS" and selected[0].get("exit_code") == 0,
                "COMMAND_CHECK_MISSING_OR_FAILED")
        require(isinstance(selected[0].get("duration_seconds"), (int, float)) and selected[0]["duration_seconds"] >= 0,
                "COMMAND_TIMING_MISSING")
        if spec["check"] == "unit-tests":
            if profile == "backend-check":
                validate_junit(documents.get("junit", {}), minimum=1800, allow_spark_opt_in=True)
            else:
                unit = documents.get("unit-results", {})
                require(unit.get("success") is True and unit.get("numTotalTests", 0) > 0
                        and all(unit.get(k) == 0 for k in ("numFailedTests", "numPendingTests", "numTodoTests")), "FRONTEND_TESTS_INCOMPLETE")
        if spec["check"] == "runtime-isolation":
            probe = documents.get("runtime-probe", {})
            passing(probe)
            require(probe.get("runtime_exit_code") == 0 and probe.get("check") == "REPORT_NATIVE_RUNTIME_DIAGNOSTIC",
                    "RUNTIME_ISOLATION_FAILED")
    elif profile == "compose-clean":
        value = documents.get("clean-demo", {})
        passing(value)
        require(value.get("smoke") == "NOT_RUN_CLEAN_DEMO" and value.get("playwright") == "PASS"
                and value.get("migrations") == "PASS" and value.get("main_inventory") == "UNCHANGED", "CLEAN_DEMO_INCOMPLETE")
    elif profile == "compose-browser":
        passing(value)
        require(all(value.get(k) == "PASS" for k in ("smoke", "playwright", "migrations", "persistence_after_restart"))
                and value.get("main_inventory") == "UNCHANGED" and value.get("persistence_components_quiesced") is True,
                "COMPOSE_FLOW_INCOMPLETE")
        browser(documents.get("browser", {}))
        browser_selection(documents.get('browser', {}), spec)
        require(documents.get("before") and documents.get("before") == documents.get("after"), "RESTART_STATE_CHANGED")
        require(bool(documents.get("migrations")), "MIGRATION_HISTORY_MISSING")
    elif profile.startswith("xlsx-"):
        fixture = value.get("fixture", {})
        if profile == "xlsx-acquisition":
            passing(value)
            require(value.get("rows") == rows and value.get("strings") == spec["variant"] and fixture.get("rows") == rows,
                    "XLSX_VARIANT_OR_POPULATION")
            integrity(value.get("integrity", {}), rows, fixture.get("canonical_rows_sha256"), numbering=True)
            full_profile(value.get("profile", {}), fixture, rows)
            require(bool(value.get("acquisition", {}).get("output_version_id")), "XLSX_OUTPUT_MISSING")
        elif profile == "xlsx-limit":
            passing(value)
            require(value.get("rows_in_fixture") == rows and value.get("configured_maximum") == rows - 1
                    and value.get("attempt_status") == "FAILED" and value.get("no_partial_version") is True,
                    "XLSX_LIMIT_NOT_ENFORCED")
            require(value.get("diagnostic", {}).get("code") == "ACQUISITION_ROW_LIMIT"
                    and value.get("previous_version_ids") and value.get("new_diagnostic_kept_for_native_backup") is True,
                    "XLSX_LIMIT_HISTORY_MISSING")
            retained = value.get("retained_full_integrity", {})
            integrity(retained, rows - 1, numbering=True)
        elif profile == "xlsx-browser":
            browser(value.get("browser", {}), 1)
            require(fixture.get("rows") == rows, "XLSX_BROWSER_POPULATION")
            for field in ("source_integrity", "output_integrity"):
                integrity(value.get(field, {}), rows, fixture.get("canonical_rows_sha256"), numbering=True)
            integrity(value.get("target", {}), rows, fixture.get("canonical_rows_sha256"))
            require(value.get("ui", {}).get("source_version_id") and value.get("ui", {}).get("output_version_id"), "XLSX_BROWSER_UI_MISSING")
        elif profile == "xlsx-chain":
            chain(value, rows)
            require(value.get("unread", {}).get("read_unread_unread_read") == "PASS"
                    and value.get("unread", {}).get("counter_delta") == 1, "UNREAD_IDEMPOTENCE_MISSING")
        elif profile == "xlsx-dispatch":
            passing(value)
            dispatch = value.get("dispatch", {})
            occurrences = value.get("occurrences", [])
            require(dispatch.get("distinct_source_versions") == 2 and dispatch.get("distinct_datasets") == 2
                    and len(occurrences) == 4 and all(o.get("row_count", 0) >= rows for o in occurrences),
                    "DISPATCH_TWO_DATASET_PROOF_MISSING")
            busy = value.get("busy", {})
            require(busy.get("before_dispatch", {}).get("live_worker_lease") is True
                    and busy.get("after_dispatch", {}).get("live_worker_lease") is True
                    and busy.get("terminal") == {"status": "SUCCESS", "decision": "COMMITTED", "rows_written": rows},
                    "DISPATCH_REAL_BUSY_WORKER_MISSING")
            require(not any(value.get("cleanup", {}).get(k) for k in ("cancel_failures", "pause_failures", "validation_cancel_failures", "run_discovery_failed")),
                    "DISPATCH_SCOPED_CLEANUP_FAILED")
        elif profile == "xlsx-unread":
            passing(value)
            require(value.get("read_at") is None and type(value.get("unread_count")) is int
                    and value.get("kept_unread_for_native_backup") is True, "UNREAD_RESTART_INCOMPLETE")
        elif profile == "xlsx-cancel":
            require(value.get("status") == "CANCELLED" and value.get("versions") == 0
                    and value.get("observed_records_before_cancel", 0) >= 5000, "CANCEL_PARTIAL_OUTPUT_OR_UNOBSERVED")
            require(fixture.get("rows") == rows and fixture.get("strings") == "shared", "CANCEL_VARIANT_OR_POPULATION")
        elif profile == "xlsx-crash":
            require(value.get("attempts", 0) >= 2 and value.get("versions") == 1, "CRASH_LEASE_RECOVERY_INCOMPLETE")
            require(fixture.get("rows") == rows and fixture.get("strings") == "shared", "CRASH_VARIANT_OR_POPULATION")
            integrity(value.get("full_integrity", {}), rows, fixture.get("canonical_rows_sha256"), numbering=True)
        else:
            native_restore = value.get("native_recovery_report", value)
            native_fixture_restore(native_restore)
            require(value.get("unread_restart", {}).get("native_state_fingerprint_comparison") == "PASS",
                    "XLSX_NATIVE_UNREAD_NOT_PRESERVED")
    elif profile == "identity":
        passing(value)
        browser(documents.get("browser", value.get("playwright", {})))
        require(all(value.get(k) == "PASS" for k in ("restart", "temporary_password_expiration", "one_time_issue_and_regeneration",
                 "plaintext_database_and_log_scan", "no_notification_records_created", "cleanup"))
                and value.get("main_inventory") == "UNCHANGED", "IDENTITY_SECURITY_OR_RESTART_INCOMPLETE")
    elif profile == "connections":
        passing(value)
        browser(documents.get("browser", {}))
        browser_selection(documents.get('browser', {}), spec)
        sources = value.get("sources", [])
        require(len(sources) == 2 and {s.get("source_type") for s in sources} == {"POSTGRESQL", "SQLSERVER"}
                and value.get("playwright") == "PASS" and value.get("regression_smoke") == "PASS", "CONNECTOR_COVERAGE_INCOMPLETE")
        streaming = value.get("sqlserver_streaming_probe", {})
        passing(streaming)
        require(streaming.get("rows") == 30000 and streaming.get("cancellation", {}).get("status") == "PASS"
                and bool(value.get("temporal_regressions")) and bool(value.get("checks")), "CONNECTOR_STREAMING_OR_TEMPORAL_MISSING")
    elif profile == "delivery":
        passing(value)
        browser(documents.get("browser", {}))
        destinations = value.get("destinations", [])
        require(len(destinations) == 2 and {d.get("sink_type") for d in destinations} == {"POSTGRESQL", "SQLSERVER"}, "DELIVERY_ENGINE_COVERAGE")
        for destination in destinations:
            passing(destination)
            require(len(destination.get("runs", [])) >= 4 and destination.get("audit_columns", {}).get("status") == "PASS",
                    "DELIVERY_STRATEGY_OR_AUDIT_MISSING")
        passing(value.get("controlled_ack_loss", {}))
        require(value.get("controlled_ack_loss", {}).get("automatic_replay") is False and value.get("playwright") == "PASS"
                and value.get("postgres_metrics_matrix"), "DELIVERY_UNKNOWN_OR_METRICS_MISSING")
    elif profile == "restore":
        restore(documents.get(spec["source"], {}), spec["version"])
    elif profile.startswith("catalog-"):
        passing(value)
        require(value.get("main_unchanged") is True and value.get("source_tree_dirty") is False, "CATALOG_MAIN_OR_DIRTY_TREE")
        if profile == "catalog-population":
            tiers = [t for t in value.get("tiers", []) if t.get("rows_per_source") == rows]
            require(len(tiers) == 1, "CATALOG_TIER_MISSING_OR_DUPLICATE")
            catalog_population(tiers[0], rows)
        elif profile == "catalog-browser":
            browser(value.get("browser", {}))
        elif profile == "catalog-snapshot":
            snapshot = value.get("postgres_joint_snapshot", {})
            passing(snapshot)
            require(snapshot.get("database") == "POSTGRESQL" and snapshot.get("read_isolation") == "REPEATABLE READ"
                    and snapshot.get("source_count") == 2 and snapshot.get("real_new_intake_approvals") == 2,
                    "JOINT_SNAPSHOT_REAL_CONCURRENCY_MISSING")
            require(all(snapshot.get(k) is True for k in ("new_approvals_committed_before_second_source_read",
                    "frozen_context_has_no_mixed_snapshot", "next_context_selects_both_new_outputs", "isolated_schema_and_storage_cleaned"))
                    and snapshot.get("existing_application_tables_and_artifacts_touched") is False,
                    "JOINT_SNAPSHOT_INCONSISTENT_OR_NOT_ISOLATED")
        elif profile == "catalog-http":
            observation = value.get("ephemeral_http_observation", {})
            passing(observation)
            cases = observation.get("cases", [])
            require(len(cases) == 10 and len({c.get("name") for c in cases}) == 10 and observation.get("main_unchanged") is True,
                    "EPHEMERAL_CASES_MISSING")
            for case in cases:
                passing(case)
                require(case.get("storage_and_artifact_metadata_unchanged") is True
                        and case.get("active_report_children_after") == 0, "EPHEMERAL_TEMPORALS_OR_CHILDREN")
        else:
            recovery = value.get("recovery", {})
            passing(recovery)
            recovered = recovery.get(spec["mode"], {})
            passing(recovered)
            require(recovered.get("tables_after") == 55 and recovered.get("automatic_processes_started") is False
                    and recovered.get("exact_state_comparison") == "PASS", "CATALOG_RECOVERY_INCOMPLETE")
            digest(recovered.get("source_state_sha256"))
            require(recovered.get("source_state_sha256") == recovered.get("restored_projection_sha256"), "CATALOG_RECOVERY_HASH_MISMATCH")
            if spec["mode"] == "legacy":
                require(recovered.get("source_version") == "0.7.0" and recovered.get("tables_before") == 42
                        and recovered.get("new_tables_empty") is True, "AUTHENTIC070_RECOVERY_NOT_AUTHENTIC")
            else:
                before, after = recovered.get("new_entity_counts_before", {}), recovered.get("new_entity_counts_after", {})
                require(recovered.get("all_thirteen_new_entities_populated") is True
                        and recovered.get("restore") == "STOPPED_VERIFIED"
                        and recovered.get("source_final_state") == "STOPPED_QUIESCENT"
                        and recovered.get("functional_restored_bindings") == "DEFINITION_REVISIONS_EXECUTION_PROFILE_LINEAGE_CURRENT_BLOCK_PASS",
                        "CATALOG_ENTITIES_NOT_PRESERVED")
                require(recovered.get("restored_restrictions", {}).get("current_block_new_resolution") == "PASS",
                        "CATALOG_RESTORED_BLOCK_NOT_ENFORCED")
                if before or after:
                    require(len(before) == 13 and all(n > 0 for n in before.values()) and before == after, "CATALOG_ENTITIES_NOT_PRESERVED")
    elif profile == "benchmark":
        passing(value)
        require(value.get("requested", {}).get("rows") == rows and value.get("requested", {}).get("file_only") is True
                and value.get("certification") is False and value.get("timings") and value.get("throughput"), "BENCHMARK_SMOKE_NOT_MEASURED")
    elif profile == "delivery-benchmark":
        passing(value)
        cases = value.get("cases", [])
        expected = {(engine, strategy) for engine in ("POSTGRESQL", "SQLSERVER")
                    for strategy in ("CREATE_AND_LOAD", "APPEND", "UPSERT", "OVERWRITE")}
        require(len(cases) == 8 and {(c.get("engine"), c.get("strategy")) for c in cases} == expected, "DELIVERY_BENCHMARK_CASES_MISSING")
        for case in cases:
            passing(case)
            require(case.get("remote_commit") == "COMMITTED" and case.get("destination_check") and case.get("timings"), "DELIVERY_BENCHMARK_SQL_MISSING")
    elif profile.startswith("spark-"):
        passing(value)
        require(value.get("deployment_mode") == spec["mode"] and value.get("parity_only") is False
                and value.get("cleanup_finished") is True and value.get("protected_main_inventory_unchanged") is True,
                "SPARK_RUNTIME_OR_CLEANUP_INCOMPLETE")
        if profile == "spark-parity":
            parity = documents.get("parity", {})
            validate_junit(parity, minimum=35)
            observed = {(case.get("classname", "").split(".")[-1], case.get("name")) for case in parity["cases"]}
            require(SPARK_REAL_CASES <= observed, "SPARK_DEDICATED_OPT_IN_COVERAGE_MISSING")
        else:
            measured = documents.get("million", {})
            passing(measured)
            require(measured.get("input_population_rows") == rows and measured.get("complete_value_verification") == "PASS", "SPARK_POPULATION_TRUNCATED")
            outcomes, expected = measured.get("outcomes", {}), measured.get("expected_logical_fingerprints", {})
            require(set(outcomes) == {"intake", "recon", "sentinel"}
                    and set(expected) == {"intake", "intake_accepted", "recon", "sentinel"}
                    and all(valid_fingerprint(f) for f in expected.values()), "SPARK_ORACLE_MISSING")
            for module, outcome in outcomes.items():
                passing(outcome)
                require(outcome.get("runtime", {}).get("deployment_mode") == spec["mode"]
                        and outcome.get("results", {}).get("logical_fingerprint") == expected[module], "SPARK_VALUES_OR_RUNTIME_MISMATCH")
                if spec["mode"] == "STANDALONE_CLIENT":
                    require(outcome["runtime"].get("executor_memory_status_entries", 0) >= 3, "SPARK_TWO_EXECUTORS_MISSING")
            require(outcomes["intake"].get("accepted", {}).get("logical_fingerprint") == expected["intake_accepted"], "SPARK_ACCEPTED_VALUES_MISMATCH")
    elif profile.startswith("async-"):
        passing(value)
        require(value.get("tier_mib") == spec["tier_mib"] and value.get("rows") == rows
                and value.get("main_unchanged") == "PASS" and value.get("cleanup") == "PASS", "ASYNC_TIER_OR_CLEANUP_INCOMPLETE")
        tiers = documents.get("volume", {}).get("tiers", [])
        require(len(tiers) == 1 and tiers[0].get("target_mib") == spec["tier_mib"], "ASYNC_VOLUME_MISSING")
        tier = tiers[0]
        passing(tier)
        if profile == "async-population":
            require(set(tier.get("formats", {})) == {"CSV", "JSONL", "PARQUET"}, "ASYNC_FORMAT_COVERAGE")
            integrity(tier["formats"][spec["format"]], rows, tier.get("fixture", {}).get("canonical_rows_sha256"))
            full_profile(tier["formats"][spec["format"]].get("profile", {}), tier.get("fixture", {}), rows)
            chain(tier, rows)
            require(tier.get("recovery", {}).get("attempts", 0) >= 2 and tier.get("corruption", {}).get("status") == "FAILED", "ASYNC_NEGATIVES_MISSING")
        elif profile == "async-automation":
            automation = documents.get("automation", {})
            passing(automation)
            require(all(automation.get(k) == "PASS" for k in ("request_idempotency", "lease_recovery", "target_concurrency",
                    "cross_user_read_denied", "cursor_occurrence_unique", "no_repeat", "deliberate_repeat"))
                    and automation.get("sql_rows") == 6 and automation.get("chain_decisions"), "AUTOMATION_SECURITY_OR_DEDUPE_INCOMPLETE")
        elif profile == "async-browser":
            browser(documents.get("browser", {}), 2)
            verified = documents.get("browser-integrity", {})
            passing(verified)
            require(value.get("browser") == "PASS", "ASYNC_BROWSER_INCOMPLETE")
            expected = tier.get("fixture", {}).get("canonical_rows_sha256")
            for field in ("source", "accepted", "target"):
                integrity(verified.get(field, {}), rows, expected)
        else:
            require(value.get("native_recovery") == "PASS", "ASYNC_NATIVE_MISSING")
            native = documents.get("recovery", {})
            native_fixture_restore(native)
    else:
        raise EvidenceError("UNKNOWN_CONTENT_VALIDATOR")
