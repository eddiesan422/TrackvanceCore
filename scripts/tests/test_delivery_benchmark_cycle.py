import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("delivery_benchmark_cycle.py")
spec = importlib.util.spec_from_file_location("delivery_benchmark_cycle", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-core",
    "trackvance-delivery-e2e-test", "trackvance-delivery-bench", "trackvance-delivery-bench-TEST"])
def test_rejects_any_non_benchmark_project(project):
    with pytest.raises(ValueError, match="aislado"):
        runner.validated_project_name(project)


def test_policy_does_not_increase_product_defaults_or_label_skips_pass():
    mib, gib = runner.bench.MIB, runner.bench.GIB
    assert runner.resource_policy(8 * mib, 20_000, 16 * gib, 100 * gib)["status"] == "PLANNED"
    for size, rows, memory, disk in ((100 * mib, 20_000, 16 * gib, 100 * gib),
                                   (mib, 100_001, 16 * gib, 100 * gib),
                                   (mib, 1000, 4 * gib, 100 * gib),
                                   (mib, 1000, 16 * gib, gib)):
        result = runner.resource_policy(size, rows, memory, disk)
        assert result["status"] == "NOT_RUN_RESOURCE_LIMIT"
        assert result["reasons"]
        assert result["max_upload_bytes"] == 10 * mib


def test_mapping_uses_varied_fixture_types_and_bounded_string_lengths():
    columns = runner.mappings(420)
    assert [item["target_type"] for item in columns] == ["STRING", "DECIMAL", "DATE", "STRING"]
    assert (columns[1]["precision"], columns[1]["scale"]) == (18, 0)
    assert columns[-1]["length"] == 420
    assert [item["ordinal"] for item in columns] == [0, 1, 2, 3]


def test_resource_delta_separates_cumulative_memory_and_measured_cpu_io():
    before = {"api": {"cpu_usage_usec": 1_000_000, "cgroup_io": {"wbytes": 10}}}
    after = {"api": {"cpu_usage_usec": 3_000_000, "cgroup_io": {"wbytes": 50},
                     "cgroup_peak_memory_bytes": 123, "oom_killed": False}, "missing": {}}
    delta = runner.resource_delta(before, after)
    assert delta["api"]["cpu_seconds"] == 2
    assert delta["api"]["io_delta"] == {"wbytes": 40}
    assert delta["api"]["memory_peak_since_container_start_bytes"] == 123
    assert delta["missing"]["cpu_seconds"] is None
    assert delta["missing"]["io_delta"] is None


def test_benchmark_overlay_only_sets_resource_ceilings():
    overlay = (runner.ROOT / "deploy/docker/compose.delivery-benchmark.yml").read_text()
    assert "MAX_UPLOAD_BYTES" not in overlay
    assert "TRACKVANCE_MAX_ROWS" not in overlay
    assert "environment:" not in overlay
    assert "mem_limit:" in overlay


def test_preexisting_project_is_never_cleaned(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--project", "trackvance-delivery-bench-existing",
        "--smoke", "--evidence-dir", str(tmp_path / "evidence")])
    monkeypatch.setattr(runner.bench, "docker_capacity", lambda: {
        "docker": {"memory_bytes": 16 * runner.bench.GIB},
        "host": {"disk_free_bytes": 100 * runner.bench.GIB},
    })
    monkeypatch.setattr(runner.bench.docker_state, "inventory", lambda project: {
        "containers": [{"id": "user-container"}], "volumes": [], "networks": []})
    cleaned = []
    monkeypatch.setattr(runner.bench, "cleanup", lambda *args: cleaned.append(args))
    assert runner.main() == 1
    assert cleaned == []


def test_ingestion_schema_and_hash_are_checked_without_type_coercion():
    fixture = {"rows": 10, "payload_bytes": 32, "sha256": "source-sha"}
    version = {"row_count": 10, "sha256": "source-sha", "schema": [
        {"name": item["source_name"], "logical_type": item["target_type"]}
        for item in runner.mappings(32)]}
    assert runner.validate_source(version, fixture)["amount"] == "DECIMAL"
    version["schema"][1]["logical_type"] = "INT64"
    with pytest.raises(RuntimeError, match="tipos"):
        runner.validate_source(version, fixture)
    version["schema"][1]["logical_type"] = "DECIMAL"
    version["sha256"] = "other-sha"
    with pytest.raises(RuntimeError, match="hash"):
        runner.validate_source(version, fixture)


def test_generated_csv_matches_real_reader_profile_without_overrides(tmp_path):
    from trackvance.dataset_readers import read_dataset
    from trackvance.processing import profile_frame

    path = tmp_path / "benchmark.csv"
    fixture = runner.bench.generate_fixture(path, 64 * 1024, 1000, "varied")
    source = read_dataset(path)
    schema, _, _ = profile_frame(source.frame, native_types=source.native_schema)
    version = {"schema": schema, "row_count": source.frame.height, "sha256": fixture["sha256"]}
    assert runner.validate_source(version, fixture) == {
        "record_key": "STRING", "amount": "DECIMAL", "event_date": "DATE", "payload": "STRING"}


def evidence_case(engine="POSTGRESQL", strategy="UPSERT"):
    inserted, updated = ((None, None) if engine == "POSTGRESQL" and strategy == "UPSERT"
                         else (5, 5) if strategy == "UPSERT" else (10, 0))
    counts = {"rows_attempted": 10, "rows_written": 10, "rows_inserted": inserted,
              "rows_updated": updated, "bytes_sent": 100}
    metrics = {**counts, "preflight_seconds": 0.25, "write_seconds": 0.75,
               "metric_semantics": {"version": 1, "rows_written": "SOURCE_ROWS"}}
    completed = {"id": "run", "metrics": metrics}
    attempt = {"id": "attempt", **counts}
    receipt = {**metrics, "kind": "DELIVERY_RECEIPT", "result": "COMMITTED",
               "run_id": "run", "delivery_attempt_id": "attempt", "dataset_version_id": "source",
               "source_sha256": "source-sha", "destination_version_id": "destination",
               "write_strategy": strategy}
    manifest = {"schema_version": 2, "module": "DELIVERY", "run_id": "run",
                "metrics": dict(metrics), "delivery": {"attempt": {"id": "attempt"}}}
    version = {"id": "source", "sha256": "source-sha"}
    destination = {"destination_version_id": "destination", "sink_type": engine}
    return completed, attempt, receipt, manifest, version, destination, strategy, 10


@pytest.mark.parametrize("engine", runner.ENGINES)
@pytest.mark.parametrize("strategy", runner.STRATEGIES)
def test_receipt_manifest_and_attempt_are_consistent(engine, strategy):
    runner.validate_evidence(*evidence_case(engine, strategy))


@pytest.mark.parametrize("mutation", ["identity", "missing_count", "invented_zero", "timing", "semantics"])
def test_evidence_mismatch_fails_closed(mutation):
    args = evidence_case()
    if mutation == "identity":
        args[2]["run_id"] = "other"
    elif mutation == "missing_count":
        del args[2]["rows_inserted"]
    elif mutation == "invented_zero":
        args[2]["rows_inserted"] = 0
    elif mutation == "timing":
        args[2]["write_seconds"] = 42
    else:
        args[2]["metric_semantics"] = None
    with pytest.raises(RuntimeError):
        runner.validate_evidence(*args)


@pytest.mark.parametrize("value", [None, 0, -1, True, float("nan"), float("inf")])
def test_write_duration_is_real_positive_and_finite(value):
    args = evidence_case()
    for item in (args[0]["metrics"], args[2], args[3]["metrics"]):
        item["write_seconds"] = value
    with pytest.raises(RuntimeError, match="duración"):
        runner.validate_evidence(*args)


@pytest.mark.parametrize("engine", runner.ENGINES)
def test_independent_destination_check_catches_upsert_that_did_not_update(monkeypatch, engine):
    fixture = {"rows": 10, "payload_bytes": 32}
    monkeypatch.setattr(runner.delivery, "target_scalar", lambda *args: "10|10|65|32|32")
    assert runner.verify_destination(None, engine, "UPSERT", fixture)["amount_sum"] == 65
    monkeypatch.setattr(runner.delivery, "target_scalar", lambda *args: "10|10|45|32|32")
    with pytest.raises(RuntimeError, match="independiente"):
        runner.verify_destination(None, engine, "UPSERT", fixture)


def test_unreached_cases_never_claim_pass():
    cases = [{"engine": "POSTGRESQL", "strategy": "CREATE_AND_LOAD", "status": "PASS"}]
    active = {"engine": "POSTGRESQL", "strategy": "APPEND"}
    remaining = runner.remaining_cases(cases, active, True)
    assert len(remaining) == 6
    assert {item["status"] for item in remaining} == {"NOT_RUN_RESOURCE_LIMIT"}
    assert {item["status"] for item in runner.remaining_cases(cases, active, False)} == {"NOT_RUN_AFTER_FAILURE"}


def test_resource_skip_creates_no_docker_resources_and_reports_all_cases(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--project", "trackvance-delivery-bench-skipped",
        "--smoke", "--evidence-dir", str(evidence)])
    monkeypatch.setattr(runner.bench, "docker_capacity", lambda: {
        "docker": {"memory_bytes": 4 * runner.bench.GIB},
        "host": {"disk_free_bytes": 100 * runner.bench.GIB},
    })
    touched = []
    monkeypatch.setattr(runner.bench.docker_state, "inventory", lambda *args: touched.append("inventory"))
    monkeypatch.setattr(runner.bench, "cleanup", lambda *args: touched.append("cleanup"))
    assert runner.main() == 0
    assert touched == []
    result = json.loads((evidence / "result.json").read_text())
    assert result["status"] == "NOT_RUN_RESOURCE_LIMIT"
    assert len(result["not_run_cases"]) == 8
    assert all(item["status"] == "NOT_RUN_RESOURCE_LIMIT" for item in result["larger_tiers"])


def test_cleanup_failure_after_fixture_limit_is_not_masked_by_skip_exit_code(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--project", "trackvance-delivery-bench-oversize",
        "--smoke", "--evidence-dir", str(evidence)])
    monkeypatch.setattr(runner.bench, "docker_capacity", lambda: {
        "docker": {"memory_bytes": 16 * runner.bench.GIB},
        "host": {"disk_free_bytes": 100 * runner.bench.GIB},
    })
    monkeypatch.setattr(runner.bench.docker_state, "inventory", lambda *args: {
        "containers": [], "volumes": [], "networks": []})
    monkeypatch.setattr(runner.bench, "generate_fixture", lambda *args: {
        "actual_bytes": 11 * runner.bench.MIB})

    def failed_cleanup(*args):
        raise RuntimeError("Cleanup verification failed")

    monkeypatch.setattr(runner.bench, "cleanup", failed_cleanup)
    assert runner.main() == 1
    result = json.loads((evidence / "result.json").read_text())
    assert result["status"] == "FAIL"
    assert result["cleanup"] == "FAIL"
    assert result["stop_reason"] == "ACTUAL_CSV_EXCEEDS_DEFAULT_UPLOAD"
