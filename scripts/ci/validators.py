"""Content checks for real suite evidence, independent of the receipt PASS flag."""
from __future__ import annotations

import hashlib
import re
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
        "test_r08004_real_spark_effective_coverage_union_parity[overlap-2]",
        "test_r08004_real_spark_effective_coverage_union_parity[complementary-3]",
        "test_r08004_real_spark_effective_coverage_union_parity[none-0]",
        "test_r08004_real_spark_effective_coverage_union_parity[ignore-2]",
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
} | {("test_spark_service_e2e", "test_real_local_spark_run_publishes_complete_registered_lineage"),
     ("test_spark_service_e2e", "test_r08503_real_spark_publication_preserves_declared_schema_and_identifiers")}


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


def validate_junit(data: dict[str, Any], *, minimum: int = 1, allow_spark_opt_in: bool = False,
                   require_spark_real: bool = False) -> None:
    cases = data.get("cases", [])
    require(isinstance(cases, list) and len(cases) >= minimum, "INSUFFICIENT_TEST_COVERAGE")
    require(len({c.get("id") for c in cases}) == len(cases), "DUPLICATE_TEST_CASE")
    if require_spark_real:
        observed = {(case.get("classname", "").split(".")[-1], case.get("name")) for case in cases}
        require(SPARK_REAL_CASES <= observed, "SPARK_FUNCTIONAL_COVERAGE_MISSING")
    for case in cases:
        require(case.get("failure") is False and case.get("error") is False, "TEST_FAILURE_OR_ERROR")
        if require_spark_real and (case.get("classname", "").split(".")[-1], case.get("name")) in SPARK_REAL_CASES:
            require(case.get("skip") is None, "SPARK_FUNCTIONAL_CASE_SKIPPED")
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


def ephemeral_http_observation(value: dict[str, Any]) -> None:
    """Observe real syscalls for success, before/after-header failure and disconnect."""
    passing(value)
    names = {"PREVIEW_SUCCESS", "PREVIEW_LARGE_CONTEXT_REQUEST", "PREVIEW_RESOURCE_FAILURE", "PREVIEW_CONSUMER_DISCONNECT",
             "CSV_SUCCESS", "CSV_RESOURCE_FAILURE", "CSV_CONSUMER_DISCONNECT", "XLSX_SUCCESS", "XLSX_RESOURCE_FAILURE",
             "XLSX_CONSUMER_DISCONNECT", "XLSX_LATE_SERIALIZATION_FAILURE", "XLSX_CELL_LIMIT", "CSV_CELL_BYTES",
             "CSV_SERIALIZED_BYTES", "CSV_DEADLINE_EXPIRED", "CSV_APPROVAL_REVOKED", "CSV_FROZEN_VERSION_DRIFT"}
    stream_errors = {"XLSX_CELL_LIMIT": "REPORT_XLSX_CELL_LIMIT", "CSV_CELL_BYTES": "REPORT_CELL_LIMIT",
                     "CSV_SERIALIZED_BYTES": "REPORT_DOWNLOAD_BYTES", "CSV_DEADLINE_EXPIRED": "REPORT_TIMEOUT",
                     "CSV_APPROVAL_REVOKED": "REPORT_SOURCE_REVOKED", "CSV_FROZEN_VERSION_DRIFT": "REPORT_SOURCE_CHANGED"}
    cases = value.get("cases", [])
    require(len(cases) == len(names) and {case.get("name") for case in cases} == names and value.get("main_unchanged") is True
            and value.get("version") == "0.8.5" and value.get("requirement") == "R085-01", "EPHEMERAL_CASES_MISSING")
    require(value.get("trace_scope") == "REAL_NGINX_API_AND_CONFINED_CHILDREN" and value.get("privileges_added") is False
            and value.get("raw_traces_published") is False, "EPHEMERAL_OBSERVATION_SCOPE")
    source_metadata = value.get("metadata_storage_baseline", {}).get("protected_source_metadata_sha256")
    digest(source_metadata)
    for case in cases:
        passing(case)
        require(case.get("storage_and_artifact_metadata_unchanged") is True and case.get("active_report_children_after") == 0,
                "EPHEMERAL_TEMPORALS_OR_CHILDREN")
        require(case.get("source_metadata_sha256_after") == source_metadata, "EPHEMERAL_SOURCE_METADATA_CHANGED")
        traces = case.get("during_execution", {})
        require(set(traces) == {"api", "proxy"}, "EPHEMERAL_TRACES_MISSING")
        for trace in traces.values():
            passing(trace, "EPHEMERAL_TRACE_NOT_PASS")
            require(trace.get("violations") == {} and trace.get("raw_trace_published") is False
                    and type(trace.get("observed_syscalls")) is int and trace["observed_syscalls"] > 0
                    and bool(trace.get("syscall_counts")), "EPHEMERAL_TRACE_INCOMPLETE")
            digest(trace.get("sha256"))
        if case["name"] in {"CSV_RESOURCE_FAILURE", "XLSX_RESOURCE_FAILURE"}:
            require(case.get("execution_status") == "FAILED" and case.get("generation") == "FAILED"
                    and case.get("transmission") == "NOT_STARTED" and case.get("http_status") == 422
                    and case.get("bytes_delivered") == 0 and case.get("error_code") == "REPORT_RESULT_LIMIT"
                    and case.get("failure_stage") == "FINAL_COUNT_BEFORE_HEADERS", "EPHEMERAL_PREFLIGHT_FAILURE_MISSING")
        elif case["name"] == "XLSX_LATE_SERIALIZATION_FAILURE":
            require(case.get("execution_status") == "FAILED" and case.get("generation") == "FAILED"
                    and case.get("transmission") == "INTERRUPTED" and case.get("http_status") == 200
                    and case.get("bytes_delivered", 0) > 0 and case.get("error_code") == "REPORT_XLSX_CHARACTER"
                    and case.get("incomplete_zip") is True and case.get("original_error_preserved") is True
                    and case.get("failure_stage") == "SERIALIZATION_AFTER_HEADERS", "EPHEMERAL_LATE_FAILURE_MISSING")
        elif case["name"] in {"CSV_SUCCESS", "XLSX_SUCCESS"}:
            require(case.get("execution_status") == "SUCCESS" and case.get("generation") == "COMPLETE"
                    and case.get("transmission") == "COMPLETE" and case.get("rows") == 180 and case.get("bytes", 0) > 1048576,
                    "EPHEMERAL_COMPLETE_POPULATION_MISSING")
            digest(case.get("population_sha256"))
        elif case["name"] in {"CSV_CONSUMER_DISCONNECT", "XLSX_CONSUMER_DISCONNECT"}:
            require(case.get("execution_status") == "INTERRUPTED" and case.get("transmission") == "INTERRUPTED",
                    "EPHEMERAL_DISCONNECT_MISSING")
        elif case["name"] in stream_errors:
            require(case.get("execution_status") == "FAILED" and case.get("generation") == "FAILED"
                    and case.get("transmission") == "INTERRUPTED" and case.get("http_status") == 200
                    and case.get("bytes_delivered", 0) > 0 and case.get("error_code") == stream_errors[case["name"]]
                    and case.get("original_error_preserved") is True and case.get("failure_stage") == "DURING_STREAM",
                    "EPHEMERAL_STREAM_FAILURE_MISSING")
            if case["name"] == "CSV_DEADLINE_EXPIRED":
                require(case.get("fault_injection") == "PERSISTED_DEADLINE_EXPIRED", "EPHEMERAL_DEADLINE_PROOF_MISSING")
            elif case["name"] in {"CSV_APPROVAL_REVOKED", "CSV_FROZEN_VERSION_DRIFT"}:
                require(case.get("fault_injection") == "OWNED_SYNTHETIC_METADATA"
                        and case.get("fixture_source_metadata_restored") is True, "EPHEMERAL_SOURCE_FAULT_PROOF_MISSING")


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
        'reports-download-opfs.spec.ts': ('TV_REPORT_DOWNLOAD_OPFS', 'catalog-reports'),
    }

    required = spec['browser_required_specs']
    selected = value.get('selected_spec_files')
    require(isinstance(selected, list) and sorted(selected) == sorted(required)
            and len(set(selected)) == len(selected), 'BROWSER_APPLICABLE_SPEC_SELECTION')
    if spec.get('browser_selection_mode') == 'explicit':
        require(value.get('browser_selection_mode') == 'explicit'
                and all((ROOT / 'frontend' / file).is_file() for file in required),
                'BROWSER_EXPLICIT_SELECTION_PROOF')
        return
    inventory = {'tests-e2e/' + path.name for path in (ROOT / 'frontend/tests-e2e').glob('*.spec.ts')}
    expected_exclusions = [{'file': 'tests-e2e/' + name, 'required_flag': flag, 'covered_by_group': group}
                           for name, (flag, group) in sorted(opt_ins.items()) if 'tests-e2e/' + name not in required]
    require(value.get('excluded_opt_in_specs') == expected_exclusions
            and inventory == set(required) | {row['file'] for row in expected_exclusions},
            'BROWSER_UNEXPECTED_SPEC_OMISSION')


def selective_cleanup_receipt(value: dict, source_sha: str) -> None:
    """Require the real PostgreSQL owned-copy trial, including restore and crash recovery."""
    passing(value)
    require(value.get("scope") == "OWNED_RESTORED_POSTGRES_COPY" and value.get("database") == "POSTGRESQL"
            and bool(re.fullmatch(r"trackvance-v080-test-restore085-[a-f0-9]{12}", value.get("project", "")))
            and value.get("source_sha") == source_sha, "SELECTIVE_CLEANUP_NATIVE_SCOPE_OR_COMMIT")
    require(value.get("schema_version") == 9 and value.get("migration") == "0019_governance_people"
            and value.get("tables") == 56, "SELECTIVE_CLEANUP_NATIVE_SCHEMA")
    require(value.get("verified_restore") == "STOPPED_VERIFIED"
            and bool(re.fullmatch(r"trackvance-v080-test-cleanuprestore085-[a-f0-9]{12}", value.get("verified_restore_project", "")))
            and value.get("verified_restore_project") != value.get("project"), "SELECTIVE_CLEANUP_REAL_RESTORE_MISSING")
    for field in ("backup_manifest_sha256", "verified_state_sha256", "plan_sha256"):
        digest(value.get(field))
    require(all(value.get(field) == "PASS" for field in ("dry_plan_read_only", "fault_rollback", "abrupt_fault_recovery",
        "metadata_transaction", "sql_relationships", "protected_hashes", "protected_secret_bytes", "unknown_barrier_preserved", "files_quarantined", "reset_audit")),
        "SELECTIVE_CLEANUP_FAULT_PROTECTION_OR_SQL_MISSING")
    require(value.get("external_destinations_touched") is False and value.get("backup_retained") is True
            and value.get("usual_inventory") == "UNCHANGED" and value.get("reset_audit_added") == 1,
            "SELECTIVE_CLEANUP_EXTERNAL_BACKUP_OR_AUDIT")
    protected = {"users", "roles", "role_permissions", "sessions", "external_identities", "oidc_login_attempts",
        "external_connections", "external_connection_versions", "delivery_destinations", "delivery_destination_versions",
        "delivery_target_guards", "delivery_target_policies", "delivery_target_decisions", "macro_domains", "data_domains",
        "glossary_terms", "governance_people", "dataset_blocks", "dataset_security_dependencies"}
    removed = value.get("removed_counts", {})
    require(set(value.get("protected_tables", [])) == protected and all(removed.get(name) == 0 for name in protected | {"audit_events"})
            and removed.get("datasets") == 2 and all(removed.get(name, 0) > 0 for name in ("dataset_versions", "runs", "jobs",
                "configurations", "monitor_schedules", "monitor_schedule_versions", "report_definitions", "report_revisions",
                "report_contexts", "report_executions", "strict_approvals", "outbox_events", "event_consumptions", "internal_notifications"))
            and value.get("quarantined_files", 0) > 0 and value.get("verified_surviving_artifacts", 0) > 0
            and value.get("protected_secret_volumes") == 4 and value.get("unknown_run_id") and value.get("protected_dataset_id"),
            "SELECTIVE_CLEANUP_IDS_DEPENDENCIES_OR_FILES")

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
    if version == "0.8.5":
        require(value.get("state_schema_version") == 9 and value.get("alembic_revision") == "0019_governance_people", "RESTORE_SCHEMA_MISMATCH")
        require(value.get("persistence_exact_comparison") == "PASS"
                and value.get("immutable_comparison_before_automatic_processes") == "PASS", "RESTORE_NOT_EXACT")
        passing(value.get("plaintext_backup_scan", {}))
        require(value.get("retained_projects") == [] and value.get("doctor") == "PASS", "RESTORE_NOT_CLEAN_OR_READY")
        require(value.get("verified_source_secrets") == 1 and value.get("verified_delivery_secrets") == 1
                and value.get("verified_artifacts", 0) > 0, "RESTORE_ARTIFACT_OR_KEY_FAMILY_MISSING")
        migration = value.get("postgres_migration", {})
        passing(migration)
        physical = migration.get("0016_0017_preservation", {}).get("physical_schema", {})
        require(physical.get("tables") == 56, "RESTORE_PHYSICAL_SCHEMA")
    else:
        require(value.get("source_version") == version and value.get("target_version") == "0.8.5", "HISTORICAL_RESTORE_VERSION")
        require(value.get("restore") == "STOPPED_VERIFIED" and value.get("automatic_processes_started") is False,
                "RESTORE_CONSUMERS_ACTIVATED")
        require(value.get("exact_historical_state") == "PASS", "HISTORICAL_STATE_NOT_EXACT")
        digest(value.get("source_state_sha256"))
        require(value.get("source_state_sha256") == value.get("restored_legacy_sha256"), "HISTORICAL_STATE_HASH_MISMATCH")
        if version == "0.6.1":
            require(value.get("native_tables") == 56 and value.get("new_catalog_tables_empty") is True, "LEGACY061_NEW_TABLES")
        else:
            require(value.get("migration") == "0019_governance_people" and value.get("state_schema_version") == 9,
                    "HISTORICAL_SCHEMA_MISMATCH")
            require(value.get("cleanup") == "PASS" and value.get("log_secret_scan") == "PASS", "HISTORICAL_CLEANUP_OR_SECRETS")


def native_fixture_restore(value: dict[str, Any]) -> None:
    """The native stopped restore used by XLSX/async has a distinct receipt."""
    passing(value, "NATIVE_FIXTURE_RESTORE_NOT_PASS")
    require(value.get("main_inventory") == "UNCHANGED" and value.get("automatic_processes_started") is False
            and value.get("restore") == "STOPPED_VERIFIED", "NATIVE_FIXTURE_RESTORE_CONSUMERS_OR_MAIN")
    require(value.get("revision") == "0019_governance_people" and value.get("state_schema_version") == 9
            and value.get("tables") == 56, "NATIVE_FIXTURE_RESTORE_SCHEMA")
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
        require(exported.get("rows") == min(counts["INNER"], limit) and exported.get("query_limit") == limit
                and exported.get("max_rows") in {120, 1000000},
                "CATALOG_EXPORT_POPULATION")
        require(exported.get("generation") == "COMPLETE" and exported.get("transmission") == "COMPLETE", "CATALOG_EXPORT_INTERRUPTED")
        digest(exported.get("sha256"))
    require(value.get("above_limit_gate") == "HOST_DOWNLOAD_085_REQUIRED", "CATALOG_FINAL_LIMIT_GATE_MISSING")
    require(value.get("parent_block_propagation") == "PASS", "DERIVED_AUTHORIZATION_MISSING")
    if rows == 120:
        passing(value.get("bounded_many_to_many", {}))
    else:
        passing(value.get("many_to_many_expansion_rejected", {}))


def catalog_download_receipt(value: dict[str, Any], expected_rows: list[int], source_sha: str) -> None:
    """Require actual host transfers, full oracles and rejected final cap+1."""
    passing(value, "CATALOG_HOST_DOWNLOAD_RECEIPT_MISSING")
    limit = 120 if expected_rows == [120] else 1000000
    require(value.get("source_sha") == source_sha and value.get("main_unchanged") is True,
            "CATALOG_HOST_DOWNLOAD_SOURCE_OR_MAIN")
    require(value.get("requested_result_rows") == expected_rows and value.get("product_row_limit_under_test") == limit
            and value.get("customers") == min(100000, limit // 10)
            and value.get("transactions_input") == limit + 1, "CATALOG_HOST_DOWNLOAD_POPULATION")
    limits = value.get("effective_limits", {}).get("profiles", {})
    require(limits.get("DOWNLOAD", {}).get("max_rows") == limits.get("XLSX", {}).get("max_rows") == limit
            and limits.get("PREVIEW", {}).get("max_rows") == 10, "CATALOG_HOST_EFFECTIVE_LIMITS")
    require(value.get("client_result_storage") == "PRIVATE_HOST_ONLY" and value.get("output_data_published") is False,
            "CATALOG_HOST_DOWNLOAD_CLIENT_STORAGE")
    resources = value.get("client_resources", {})
    passing(resources, "CATALOG_HOST_RESOURCES_UNAVAILABLE")
    require(0 < resources.get("samples", 0) and 0 < resources.get("sampled_peak_rss_bytes", 0)
            <= resources.get("budget_bytes", 0), "CATALOG_HOST_RESOURCE_BUDGET")
    sources = value.get("sources", [])
    require(len(sources) == 2 and {item.get("alias") for item in sources} == {"t", "c"}, "CATALOG_HOST_SOURCE_COVERAGE")
    require(all(item.get("status") == "PASS" and item.get("approval_id") and item.get("output_version_id") for item in sources),
            "CATALOG_HOST_SOURCE_APPROVAL")
    for item in sources:
        require(item.get("rows") == (limit + 1 if item["alias"] == "t" else min(100000, limit // 10)),
                "CATALOG_HOST_SOURCE_POPULATION")
        digest(item.get("input_fixture_sha256"))
        require(item.get("input_fixture_bytes", 0) > 0, "CATALOG_HOST_INPUT_BYTES")
    downloads = value.get("downloads", [])
    require(len(downloads) == len(expected_rows) * 2, "CATALOG_HOST_DOWNLOAD_FORMAT_COVERAGE")
    expected = {(rows, kind) for rows in expected_rows for kind in ("CSV", "XLSX")}
    observed = set()
    for download in downloads:
        passing(download)
        kind = download.get("format")
        oracle_key = "independent_csv" if kind == "CSV" else "independent_xml"
        oracle = download.get(oracle_key, {})
        rows = oracle.get("rows")
        require((rows, kind) in expected and (rows, kind) not in observed, "CATALOG_HOST_DOWNLOAD_ORACLE_COVERAGE")
        observed.add((rows, kind))
        require(oracle == value.get("oracles", {}).get(str(rows)), "CATALOG_HOST_DOWNLOAD_ORACLE_MISMATCH")
        digest(oracle.get("ordered_logical_sha256"))
        digest(download.get("file_sha256"))
        require(download.get("terminal_status") == "SUCCESS" and download.get("generation_status") == "COMPLETE"
                and download.get("transmission_status") == "COMPLETE" and download.get("bytes_received", 0) > 0
                and download.get("server_metrics", {}).get("serialized_bytes") == download["bytes_received"]
                and download.get("server_metrics", {}).get("rows") == rows
                and download.get("server_metrics", {}).get("serialization_complete") is True, "CATALOG_HOST_DOWNLOAD_TERMINAL")
        if kind == "XLSX":
            passing(download.get("ooxml", {}))
            require(download.get("independent_openpyxl") == oracle and download.get("physical_rows") == rows + 1
                    and download["ooxml"].get("crc") == "ALL_ENTRIES_PASS", "CATALOG_HOST_XLSX_READER")
    excess = value.get("excess", [])
    require(len(excess) == 2 and {item.get("format") for item in excess} == {"CSV", "XLSX"}, "CATALOG_HOST_EXCESS_COVERAGE")
    for item in excess:
        passing(item)
        require(item.get("http_status") == 422 and item.get("result_bytes_received") == 0
                and item.get("error_code") == "REPORT_RESULT_LIMIT" and item.get("terminal_status") == "FAILED"
                and item.get("generation_status") == "FAILED" and item.get("transmission_status") == "NOT_STARTED"
                and item.get("output_version_id") is None and item.get("execution_id"),
                "CATALOG_HOST_EXCESS_BEFORE_TRANSFER")
    final = value.get("final_limit", {})
    passing(final)
    require(final.get("independent_csv") == value.get("oracles", {}).get(str(expected_rows[0])),
            "CATALOG_HOST_FINAL_LIMIT_NOT_INTERMEDIATE")
    if any(rows > 120 for rows in expected_rows) or value.get("browser_streaming_requested") is True:
        streamed = value.get("browser_streaming", {})
        passing(streamed, "CATALOG_HOST_BROWSER_STREAMING_MISSING")
        require(streamed.get("method") == "REAL_OPFS_FILE_HANDLE" and streamed.get("rows") == max(expected_rows)
                and streamed.get("independent_xml") == streamed.get("independent_openpyxl")
                == value.get("oracles", {}).get(str(max(expected_rows)))
                and streamed.get("physical_rows") == max(expected_rows) + 1
                and streamed.get("generation_status") == streamed.get("transmission_status") == "COMPLETE"
                and streamed.get("bounded_memory_fallback_used") is False, "CATALOG_HOST_BROWSER_STREAMING_ORACLE")
        browser(streamed.get("browser", {}), 1)


def valid_fingerprint(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"method", "rows", "sum_sha256", "xor_sha256", "sha256"}:
        return False
    if value["method"] != "CANONICAL_JSON_ROW_SHA256_COUNT_SUM_XOR_V1" or type(value["rows"]) is not int or not 0 <= value["rows"] < 2**64:
        return False
    if not all(isinstance(value[k], str) and DIGEST.fullmatch(value[k]) for k in ("sum_sha256", "xor_sha256", "sha256")):
        return False
    encoded = value["rows"].to_bytes(8, "big") + bytes.fromhex(value["sum_sha256"]) + bytes.fromhex(value["xor_sha256"])
    return hashlib.sha256(encoded).hexdigest() == value["sha256"]


def validate_content(spec: dict[str, Any], result: dict[str, Any], *, execution_profile: str | None = None) -> None:
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
                validate_junit(documents.get("junit", {}), minimum=1800, allow_spark_opt_in=True,
                               require_spark_real=execution_profile == "functional")
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
                    and value.get("observed_records_before_cancel", 0) >= spec.get("min_observed_rows", 5000), "CANCEL_PARTIAL_OUTPUT_OR_UNOBSERVED")
            if spec.get("min_observed_rows"):
                require(value.get("controlled_checkpoint") is True, "FUNCTIONAL_CANCEL_SYNCHRONIZATION_MISSING")
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
    elif profile == "automation-functional":
        passing(value)
        require(all(value.get(k) == "PASS" for k in ("request_idempotency", "lease_recovery", "target_concurrency",
            "cross_user_read_denied", "cursor_occurrence_unique", "no_repeat", "deliberate_repeat"))
            and value.get("sql_rows") == 6 and value.get("chain_decisions"), "AUTOMATION_SECURITY_OR_DEDUPE_INCOMPLETE")
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
        require(streaming.get("rows") == spec.get("streaming_rows", 30000) and streaming.get("cancellation", {}).get("status") == "PASS"
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
            primary = destination.get("primary_keys", {})
            passing(primary)
            cases = primary.get("cases", [])
            require(primary.get("requirement") == "R085-02"
                    and {case.get("table") for case in cases if case.get("table")} == {"pk_simple_085", "pk_composite_085"}
                    and all(case.get("status") == "PASS" and case.get("backing_indexes") == 1 and case.get("rows") == 3
                            and case.get("not_null") is True and case.get("existing_strategies_preserve_pk") is True
                            and case.get("primary_key_columns") == ({"pk_simple_085": ["quantity"],
                                 "pk_composite_085": ["transaction_code", "quantity"]}.get(case.get("table")))
                            for case in cases if case.get("table"))
                    and {"NULL_BEYOND_PREVIEW", "REPEATED_POPULATION", "NATIVE_KEY_BYTES"} <= {
                        case.get("case") for case in cases if case.get("status") == "REJECTED_PREFLIGHT"}
                    and any(case.get("case") == "EXPLICIT_NONE" and case.get("status") == "PASS"
                            and case.get("primary_key_mode") == "NONE" for case in cases),
                    "DELIVERY_PRIMARY_KEY_COVERAGE_MISSING")
            if destination.get("sink_type") == "SQLSERVER":
                require(any(case.get("case") == "COLLATION" and case.get("status") == "REJECTED_PREFLIGHT" for case in cases),
                        "DELIVERY_PRIMARY_KEY_COLLATION_MISSING")
        integral = value.get("integral", {})
        passing(integral)
        require(integral.get("scope") == "R085_INTEGRAL"
                and integral.get("remote_sql", {}).get("rows") == 3
                and integral.get("remote_sql", {}).get("backing_indexes") == 1
                and integral.get("remote_sql", {}).get("client_codes") == "001,002,003"
                and integral.get("browser", {}).get("person_id")
                and integral.get("browser", {}).get("excel", {}).get("crc") == "PASS"
                and integral.get("browser", {}).get("delivery", {}).get("decision") == "COMMITTED",
                "DELIVERY_INTEGRAL_085_MISSING")
        passing(value.get("controlled_ack_loss", {}))
        require(value.get("controlled_ack_loss", {}).get("automatic_replay") is False and value.get("playwright") == "PASS"
                and value.get("postgres_metrics_matrix"), "DELIVERY_UNKNOWN_OR_METRICS_MISSING")
    elif profile == "restore":
        restore(documents.get(spec["source"], {}), spec["version"])
    elif profile.startswith("catalog-"):
        passing(value)
        require(value.get("main_unchanged") is True and value.get("source_tree_dirty") is False, "CATALOG_MAIN_OR_DIRTY_TREE")
        catalog_download_receipt(value.get("host_download_085", {}),
                                 [tier["rows_per_source"] for tier in value.get("tiers", [])], value.get("source_sha", ""))
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
            ephemeral_http_observation(value.get("ephemeral_http_observation", {}))
        elif profile == "catalog-cleanup":
            passing(value.get("recovery", {}))
            selective_cleanup_receipt(value.get("recovery", {}).get("native", {}).get("selective_cleanup", {}), value.get("source_sha", ""))
        else:
            recovery = value.get("recovery", {})
            passing(recovery)
            recovered = recovery.get(spec["mode"], {})
            passing(recovered)
            require(recovered.get("tables_after") == 56 and recovered.get("automatic_processes_started") is False
                    and recovered.get("exact_state_comparison") == "PASS", "CATALOG_RECOVERY_INCOMPLETE")
            digest(recovered.get("source_state_sha256"))
            require(recovered.get("source_state_sha256") == recovered.get("restored_projection_sha256"), "CATALOG_RECOVERY_HASH_MISMATCH")
            if spec["mode"] == "legacy":
                require(recovered.get("source_version") == "0.7.0" and recovered.get("tables_before") == 42
                        and recovered.get("new_tables_empty") is True, "AUTHENTIC070_RECOVERY_NOT_AUTHENTIC")
            elif spec["mode"] == "legacy080":
                require(recovered.get("source_version") == "0.8.0" and recovered.get("target_version") == "0.8.5"
                        and recovered.get("tables_before") == 55 and recovered.get("source_destroyed_before_restore") is True
                        and recovered.get("baseline_commit") == "4eaaeb774878bca62d7d6f758157107f0557512e"
                        and recovered.get("functional_restored_bindings") == "DEFINITION_REVISIONS_EXECUTION_PROFILE_LINEAGE_CURRENT_BLOCK_PASS",
                        "AUTHENTIC080_RECOVERY_NOT_AUTHENTIC")
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
                selective_cleanup_receipt(recovered.get("selective_cleanup", {}), value.get("source_sha", ""))
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
