"""The observer rejects transient writes that an after-only listing would miss."""
from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
SPEC = importlib.util.spec_from_file_location("reports_ephemeral_http", Path(__file__).with_name("reports_ephemeral_http.py"))
assert SPEC and SPEC.loader
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)

from scripts.ci.common import EvidenceError
from scripts.ci.validators import ephemeral_http_observation


def test_real_descriptor_annotations_allow_only_ipc_and_read_only_files():
    report = observer.analyze_trace('''1710000000.1 openat(AT_FDCWD, "/selected.parquet", O_RDONLY|O_CLOEXEC) = 8</selected.parquet>
1710000000.2 write(3<pipe:[1234]>, ""..., 1000) = 1000
1710000000.3 writev(9<TCP:[127.0.0.1:80->127.0.0.1:4321]>, [...], 1) = 1000
1710000000.4 openat(AT_FDCWD, "/dev/null", O_RDWR|O_CLOEXEC) = 7</dev/null>
1710000000.5 mmap(NULL, 1000, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0) = 0x777
''')
    assert report["status"] == "PASS" and not report["violations"]
    assert report["observed_syscalls"] == 5
    assert "/selected" not in str(report) and report["raw_trace_published"] is False


@pytest.mark.parametrize("line,reason", [
    ('openat(AT_FDCWD, "/tmp/removed", O_WRONLY|O_CREAT|O_TRUNC, 0600) = 8</tmp/removed>', "writable_open"),
    ('openat(AT_FDCWD, "/var/cache/nginx/proxy_temp/123", O_RDWR|O_CREAT, 0600) = -1 EACCES', "writable_open"),
    ('write(8</tmp/removed (deleted)>, ""..., 1000) = 1000', "regular_or_unclassified_write"),
    ('write(8, ""..., 1000) = 1000', "regular_or_unclassified_write"),
    ('unlink("/tmp/removed") = 0', "filesystem_mutation"),
    ('mmap(NULL, 1000, PROT_READ|PROT_WRITE, MAP_SHARED, 8</tmp/removed>, 0) = 0x777', "shared_file_mapping"),
])
def test_during_execution_rejects_attempts_even_denied_or_deleted_later(line, reason):
    report = observer.analyze_trace("1710000000.2 " + line)
    assert report["status"] == "FAIL" and report["violations"][reason] == 1


def test_an_empty_trace_cannot_certify_a_flow():
    assert observer.analyze_trace("")["status"] == "FAIL"


def test_late_serialization_fixture_uses_final_case_parameter_without_mutating_sources():
    from trackvance.report_query import compile_draft

    source = {"alias": "a", "input_dataset_id": "dataset", "contract_id": "contract"}
    draft = observer.late_serialization_draft([source])
    assert draft["sources"] == [source]
    assert draft["parameters"] == [
        {"name": "last_key", "type": "TEXT", "value": observer.cycle.source_key(119)},
        {"name": "bad_text", "type": "TEXT", "value": "invalid\ufffe"},
    ]
    schemas = {"a": [{"name": "key", "logical_type": "STRING"}, {"name": "value", "logical_type": "STRING"}]}
    plan = compile_draft(draft, schemas)
    assert "CASE" in plan["sql"] and plan["parameters"]["bad_text"] == "invalid\ufffe"
    assert "invalid\ufffe" not in plan["sql"]


def test_private_cell_fixture_is_admitted_by_acquisition_and_canonical_scan_at_exact_utf8_boundary(tmp_path):
    import csv

    import polars as pl

    from trackvance.batch_readers import FileBatchReader
    from trackvance.dataset_scans import profile_paths
    from trackvance.report_query import compile_draft

    path = tmp_path / "boundary.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["key", "value"])
        for index in range(120):
            writer.writerow([observer.cycle.source_key(index), observer.cell_source_value("c", index)])
    batches = list(FileBatchReader(path, path.name))
    assert sum(batch.frame.height for batch in batches) == 120
    parquet = tmp_path / "boundary.parquet"
    population = pl.concat([batch.frame for batch in batches])
    population.write_parquet(parquet)
    value = population["value"][-1]
    assert value == "é" * 32768 and len(value.encode("utf-8")) == observer.CELL_FIXTURE_BYTES == 65536
    assert len(value.encode("utf-8")) == observer.HTTP_MAX_CELL_BYTES + 1
    schema, profile, _ = profile_paths([parquet], temporary_parent=tmp_path)
    assert profile["row_count"] == 120
    plan = compile_draft({"mode": "SQL", "sources": [{"alias": "c"}], "columns": [], "joins": [], "order_by": [],
                          "sql": "SELECT c.key AS c_key,c.value AS c_value FROM c ORDER BY c.key ASC", "parameters": []}, {"c": schema})
    assert plan["projection_schema"] == {"c_key": {"logical_type": "STRING", "semantic_tag": None},
                                         "c_value": {"logical_type": "STRING", "semantic_tag": None}}


def observation_receipt():
    trace = {"status": "PASS", "violations": {}, "observed_syscalls": 1, "syscall_counts": {"write": 1},
             "sha256": "a" * 64, "raw_trace_published": False}
    cases = []
    for name in sorted(observer.HTTP_CASES):
        case = {"name": name, "status": "PASS", "storage_and_artifact_metadata_unchanged": True,
                "active_report_children_after": 0, "source_metadata_sha256_after": "c" * 64,
                "during_execution": {"api": deepcopy(trace), "proxy": deepcopy(trace)}}
        if name in {"CSV_SUCCESS", "XLSX_SUCCESS"}:
            case.update(execution_status="SUCCESS", generation="COMPLETE", transmission="COMPLETE", rows=180,
                        bytes=1048577, population_sha256="b" * 64)
        elif name in {"CSV_RESOURCE_FAILURE", "XLSX_RESOURCE_FAILURE"}:
            case.update(execution_status="FAILED", generation="FAILED", transmission="NOT_STARTED", http_status=422,
                        bytes_delivered=0, error_code="REPORT_RESULT_LIMIT", failure_stage="FINAL_COUNT_BEFORE_HEADERS")
        elif name in {"CSV_CONSUMER_DISCONNECT", "XLSX_CONSUMER_DISCONNECT"}:
            case.update(execution_status="INTERRUPTED", transmission="INTERRUPTED")
        elif name == "XLSX_LATE_SERIALIZATION_FAILURE":
            case.update(execution_status="FAILED", generation="FAILED", transmission="INTERRUPTED", http_status=200,
                        bytes_delivered=1000, error_code="REPORT_XLSX_CHARACTER", failure_stage="SERIALIZATION_AFTER_HEADERS",
                        incomplete_zip=True, original_error_preserved=True)
        elif name in observer.STREAM_FAILURE_CODES:
            case.update(execution_status="FAILED", generation="FAILED", transmission="INTERRUPTED", http_status=200,
                        bytes_delivered=1000, error_code=observer.STREAM_FAILURE_CODES[name], failure_stage="DURING_STREAM",
                        original_error_preserved=True,
                        fixture_source_metadata_restored=True,
                        fault_injection="PERSISTED_DEADLINE_EXPIRED" if name == "CSV_DEADLINE_EXPIRED" else
                            "OWNED_SYNTHETIC_METADATA" if name in {"CSV_APPROVAL_REVOKED", "CSV_FROZEN_VERSION_DRIFT"} else "REAL_OUTPUT_BUDGET")
            if name == "CSV_CELL_BYTES":
                case.update(max_cell_bytes=65535, input_cell_bytes=65536, cell_limit_scope="PRIVATE_REDUCED_OUTPUT_BUDGET")
        cases.append(case)
    return {"status": "PASS", "version": "0.8.5", "requirement": "R085-01", "cases": cases,
            "main_unchanged": True, "trace_scope": "REAL_NGINX_API_AND_CONFINED_CHILDREN", "privileges_added": False,
            "raw_traces_published": False,
            "private_max_cell_bytes": 65535, "input_cell_bytes": 65536, "default_max_cell_bytes": 65536,
            "cell_limit_scope": "PRIVATE_REDUCED_OUTPUT_BUDGET",
            "effective_api_limits": {profile: {"max_cell_bytes": 65536 if profile == "DATASET" else 65535}
                                     for profile in ("PREVIEW", "DOWNLOAD", "DATASET", "XLSX")},
            "metadata_storage_baseline": {"protected_source_metadata_sha256": "c" * 64}}


@pytest.mark.parametrize("name", sorted(observer.HTTP_CASES))
def test_http_gate_preserves_all_old_cases_and_requires_late_serialization(name):
    receipt = observation_receipt()
    ephemeral_http_observation(receipt)
    receipt["cases"] = [case for case in receipt["cases"] if case["name"] != name]
    with pytest.raises(EvidenceError, match="EPHEMERAL_CASES_MISSING"):
        ephemeral_http_observation(receipt)


@pytest.mark.parametrize("damage,code", [
    ("late-no-bytes", "EPHEMERAL_LATE_FAILURE_MISSING"), ("late-success", "EPHEMERAL_LATE_FAILURE_MISSING"),
    ("late-lost-cause", "EPHEMERAL_LATE_FAILURE_MISSING"), ("preflight-started", "EPHEMERAL_PREFLIGHT_FAILURE_MISSING"),
    ("proxy-missing", "EPHEMERAL_TRACES_MISSING"), ("empty-trace", "EPHEMERAL_TRACE_INCOMPLETE"),
    ("transient-write", "EPHEMERAL_TRACE_INCOMPLETE"), ("child-left", "EPHEMERAL_TEMPORALS_OR_CHILDREN"),
    ("sample-only", "EPHEMERAL_COMPLETE_POPULATION_MISSING"), ("raw-private-trace", "EPHEMERAL_OBSERVATION_SCOPE"),
])
def test_http_gate_rejects_missing_failure_semantics_population_and_syscall_observation(damage, code):
    receipt = observation_receipt()
    ephemeral_http_observation(receipt)
    late = next(case for case in receipt["cases"] if case["name"] == "XLSX_LATE_SERIALIZATION_FAILURE")
    preflight = next(case for case in receipt["cases"] if case["name"] == "CSV_RESOURCE_FAILURE")
    success = next(case for case in receipt["cases"] if case["name"] == "CSV_SUCCESS")
    if damage == "late-no-bytes":
        late["bytes_delivered"] = 0
    elif damage == "late-success":
        late["execution_status"] = "SUCCESS"
    elif damage == "late-lost-cause":
        late["error_code"] = "REPORT_TRANSFER_INTERRUPTED"
    elif damage == "preflight-started":
        preflight["transmission"] = "INTERRUPTED"
    elif damage == "proxy-missing":
        late["during_execution"].pop("proxy")
    elif damage == "empty-trace":
        late["during_execution"]["api"]["observed_syscalls"] = 0
    elif damage == "transient-write":
        late["during_execution"]["proxy"]["violations"] = {"writable_open": 1}
    elif damage == "child-left":
        late["active_report_children_after"] = 1
    elif damage == "sample-only":
        success["rows"] = 10
    else:
        receipt["raw_traces_published"] = True
    with pytest.raises(EvidenceError, match=code):
        ephemeral_http_observation(receipt)


@pytest.mark.parametrize("name", sorted(observer.STREAM_FAILURE_CODES))
@pytest.mark.parametrize("damage", ["status", "error_code", "bytes", "cause"])
def test_stream_failure_gate_requires_real_partial_transport_and_original_cause(name, damage):
    receipt = observation_receipt()
    case = next(item for item in receipt["cases"] if item["name"] == name)
    if damage == "status":
        case["execution_status"] = "SUCCESS"
    elif damage == "error_code":
        case["error_code"] = "REPORT_TRANSFER_INTERRUPTED"
    elif damage == "bytes":
        case["bytes_delivered"] = 0
    else:
        case["original_error_preserved"] = False
    with pytest.raises(EvidenceError, match="EPHEMERAL_STREAM_FAILURE_MISSING"):
        ephemeral_http_observation(receipt)


def test_source_metadata_restore_must_match_exact_original_rows():
    receipt = observation_receipt()
    receipt["cases"][-1]["source_metadata_sha256_after"] = "d" * 64
    with pytest.raises(EvidenceError, match="EPHEMERAL_SOURCE_METADATA_CHANGED"):
        ephemeral_http_observation(receipt)


@pytest.mark.parametrize("name", ["CSV_APPROVAL_REVOKED", "CSV_FROZEN_VERSION_DRIFT"])
@pytest.mark.parametrize("damage", ["missing-injection", "missing-restore"])
def test_source_fault_gate_requires_owned_injection_and_restore(name, damage):
    receipt = observation_receipt()
    case = next(item for item in receipt["cases"] if item["name"] == name)
    case["fault_injection" if damage == "missing-injection" else "fixture_source_metadata_restored"] = "NOT_RUN" if damage == "missing-injection" else False
    with pytest.raises(EvidenceError, match="EPHEMERAL_SOURCE_FAULT_PROOF_MISSING"):
        ephemeral_http_observation(receipt)


@pytest.mark.parametrize("damage", ["desired-only", "effective-default", "dataset-changed", "case-default-claim", "input-over-acquisition"])
def test_private_cell_receipt_requires_actual_api_budget_without_claiming_default_limit_failure(damage):
    receipt = observation_receipt()
    if damage == "desired-only":
        del receipt["effective_api_limits"]
    elif damage == "effective-default":
        receipt["effective_api_limits"]["DOWNLOAD"]["max_cell_bytes"] = 65536
    elif damage == "dataset-changed":
        receipt["effective_api_limits"]["DATASET"]["max_cell_bytes"] = 65535
    elif damage == "case-default-claim":
        next(item for item in receipt["cases"] if item["name"] == "CSV_CELL_BYTES")["max_cell_bytes"] = 65536
    else:
        receipt["input_cell_bytes"] = 65538
    with pytest.raises(EvidenceError, match="EPHEMERAL_(?:EFFECTIVE|PRIVATE)_CELL_BUDGET_MISSING"):
        ephemeral_http_observation(receipt)
