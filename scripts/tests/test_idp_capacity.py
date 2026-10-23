"""Measured-rate sizing and bounded benchmark identity checks, without workloads."""
import copy
from types import SimpleNamespace

import pytest
from idp_benchmark import authorize_workload, bytes_value, safety_reason, summary
from idp_capacity import capacity


def measured(seconds=60, count=10):
    return {"mode": "paddle-qwen", "client_concurrency": 1, "full_corpus": False,
        "summary": {"documents": count, "processed": count, "failed": 0, "elapsed_seconds": seconds,
            "cell_accuracy": .8, "critical_errors": 2, "correct_auto_verified": 0},
        "results": [{"engine": {"inference_skipped": False}}]}


def estimate(results, **kwargs):
    return capacity(results, documents_per_user_per_day=5, window_hours=8,
        target_utilization=.5, retention_days=30, artifact_multiplier=2, **kwargs)


def test_capacity_uses_slowest_complete_measurement_and_preserves_quality_failure():
    failed = measured(1)
    failed["summary"]["failed"] = 1
    report = estimate([measured(30), measured(60), failed])
    assert report["reference"]["measured_documents_per_minute"] == 10
    assert report["reference"]["cell_accuracy"] == .8 and report["reference"]["critical_errors"] == 2
    assert report["reference"]["all_supplied_runs_eligible"] is False
    assert [row["users"] for row in report["scenarios"]] == [10, 50, 100, 500, 1000]
    assert report["scenarios"][-1]["estimated_equivalent_replicas"] == 3
    assert report["scenarios"][-1]["estimated_cpu_cores"] is None
    with pytest.raises(ValueError, match="NO_COMPLETE_MEASURED_RUN"):
        estimate([failed])


def test_capacity_resources_and_storage_depend_on_observations_and_explicit_assumptions():
    telemetry = {"summary": {"aggregate_cpu_cores_peak": 2, "aggregate_ram_bytes_peak": 5 * 1024**3,
        "device_vram_used_mib_peak": 10 * 1024}}
    report = estimate([measured()], telemetry=telemetry, average_original_bytes=1024**2)
    row = report["scenarios"][0]
    assert row["estimated_cpu_cores"] == 3 and row["estimated_ram_gib"] == 7
    assert row["observed_device_vram_gib_per_gpu"] == 10
    assert row["estimated_retained_storage_gib"] == 2.93


def test_benchmark_rejects_protected_workloads_missing_caps_and_wrong_output_bind(tmp_path):
    row = {"Name": "/owned-idp", "Config": {"Labels": {}}, "Image": "sha256:example",
        "State": {"Running": True}, "HostConfig": {"Memory": 1024, "PidsLimit": 32, "NanoCpus": 1_000_000_000},
        "Mounts": [{"Type": "bind", "RW": True, "Source": str(tmp_path), "Destination": "/private-output"}]}
    authorize_workload(row, "sha256:example", tmp_path, "/private-output")
    for name in ("trackvance-codex", "trackvance-certification", "bikerwash"):
        changed = copy.deepcopy(row)
        changed["Name"] = "/" + name
        with pytest.raises(ValueError, match="IDENTITY_OR_LIMITS_INVALID"):
            authorize_workload(changed, "sha256:example", tmp_path, "/private-output")
    changed = copy.deepcopy(row)
    changed["Config"]["Labels"]["io.trackvance.permanent"] = "false"
    with pytest.raises(ValueError):
        authorize_workload(changed, "sha256:example", tmp_path, "/private-output")
    with pytest.raises(ValueError, match="BIND_REQUIRED"):
        authorize_workload(row, "sha256:example", tmp_path / "different", "/private-output")


def test_telemetry_units_aggregate_samples_and_abort_reserve():
    assert bytes_value("1.5GiB") == int(1.5 * 1024**3)
    value = {"containers": [{"cpu_cores": .5, "ram_bytes": 100, "ram_limit_bytes": 1000}],
        "gpus": [{"vram_used_mib": 12000, "vram_total_mib": 16000, "utilization_percent": 90, "temperature_c": 60}],
        "host_available_bytes": 4 * 1024**3, "errors": []}
    report = summary([value])
    assert report["aggregate_cpu_cores_peak"] == .5 and report["device_vram_used_mib_peak"] == 12000
    args = SimpleNamespace(minimum_host_free_mib=2048, minimum_vram_free_mib=512, maximum_gpu_temperature_c=85, mode="paddle-qwen")
    assert safety_reason(value, args) is None
    value["host_available_bytes"] = 100 * 1024**2
    assert safety_reason(value, args) == "HOST_MEMORY_RESERVE"
