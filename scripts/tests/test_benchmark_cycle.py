import gzip
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("benchmark_cycle.py")
spec = importlib.util.spec_from_file_location("benchmark_cycle", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


def test_streaming_fixture_meets_target_and_is_deterministic(tmp_path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    a = benchmark.generate_fixture(first, 2 * benchmark.MIB, 2_000)
    b = benchmark.generate_fixture(second, 2 * benchmark.MIB, 2_000)
    assert a == b
    assert a["actual_bytes"] >= 2 * benchmark.MIB
    assert sum(1 for _ in first.open("rb")) == 2_001
    assert a["fixture_mode"] == "varied"
    assert a["payload_method"] == "SHAKE256_URLSAFE_V1"


def test_varied_fixture_is_materially_less_compressible(tmp_path):
    varied = tmp_path / "varied.csv"
    repeated = tmp_path / "repeated.csv"
    benchmark.generate_fixture(varied, benchmark.MIB, 1_000, "varied")
    benchmark.generate_fixture(repeated, benchmark.MIB, 1_000, "compressible")
    varied_size = len(gzip.compress(varied.read_bytes()))
    repeated_size = len(gzip.compress(repeated.read_bytes()))
    assert varied_size > repeated_size * 20


def test_fixture_rejects_cell_or_row_limits_before_writing():
    with pytest.raises(ValueError, match="celda"):
        benchmark.fixture_plan(5 * benchmark.GIB, 10_000)
    with pytest.raises(ValueError, match="acotados"):
        benchmark.fixture_plan(benchmark.MIB, 100_001)


def test_tier_policy_distinguishes_required_baseline_from_resource_skips():
    tiers = benchmark.tier_policy(16 * benchmark.GIB, 100 * benchmark.GIB)
    assert tiers[0]["status"] == "PLANNED"
    assert [item["status"] for item in tiers[1:]] == ["NOT_RUN_RESOURCE_LIMIT"] * 4
    assert tiers[1]["estimated_recon_working_set_bytes"] == 24_000 * benchmark.MIB
    assert [item["size_mib"] for item in tiers] == [100, 500, 1024, 2048, 5120]


def test_tier_policy_does_not_call_resource_skip_a_failure():
    tiers = benchmark.tier_policy(6 * benchmark.GIB, 100 * benchmark.GIB)
    assert tiers[0]["status"] == "NOT_RUN_RESOURCE_LIMIT"
    assert all(item["status"] != "FAIL" for item in tiers)


@pytest.mark.parametrize("name", ["trackvance-core", "benchmark-test", "trackvance-bench"])
def test_benchmark_project_guard(name):
    with pytest.raises(ValueError, match="aislado"):
        benchmark.validated_project_name(name)


def test_stats_units_and_secret_redaction():
    assert benchmark.parse_size("1.5GiB") == int(1.5 * benchmark.GIB)
    assert benchmark.parse_size("200MB") == 200_000_000
    assert benchmark.redact("before private after", ["private"]) == "before [REDACTED] after"


def test_benchmark_overlay_keeps_large_limits_isolated():
    overlay = (benchmark.ROOT / "deploy/docker/compose.benchmark.yml").read_text()
    base = (benchmark.ROOT / "compose.yml").read_text()
    assert "TRACKVANCE_MAX_SNAPSHOT_BYTES: '167772160'" in overlay
    assert "TRACKVANCE_WORKER_MEMORY_SOFT_BYTES: '6442450944'" in overlay
    assert "167772160" not in base
    assert "6442450944" not in base


def test_existing_project_is_never_cleaned(monkeypatch, tmp_path):
    options = SimpleNamespace(
        project="trackvance-bench-existing",
        port=32123,
        target_mib=1,
        rows=1_000,
        fixture_mode="varied",
        file_only=True,
        skip_build=True,
        keep=False,
        max_wall_seconds=600,
        evidence_dir=tmp_path / "evidence",
    )
    monkeypatch.setattr(benchmark, "parse_arguments", lambda: options)
    monkeypatch.setattr(
        benchmark,
        "docker_capacity",
        lambda: {
            "host": {"disk_free_bytes": 100 * benchmark.GIB},
            "docker": {"memory_bytes": 16 * benchmark.GIB},
        },
    )
    monkeypatch.setattr(
        benchmark.docker_state,
        "inventory",
        lambda _project: {
            "containers": [{"id": "preexisting"}],
            "volumes": [],
            "networks": [],
        },
    )
    cleanup_calls = []
    monkeypatch.setattr(
        benchmark, "cleanup", lambda *arguments: cleanup_calls.append(arguments)
    )

    assert benchmark.main() == 1
    assert cleanup_calls == []


def test_500_mib_request_is_skipped_before_fixture_or_docker_claim(monkeypatch, tmp_path):
    options = SimpleNamespace(
        project="trackvance-bench-resource-skip",
        port=32124,
        target_mib=500,
        rows=50_000,
        fixture_mode="varied",
        file_only=False,
        skip_build=True,
        keep=False,
        max_wall_seconds=600,
        evidence_dir=tmp_path / "resource-skip",
    )
    monkeypatch.setattr(benchmark, "parse_arguments", lambda: options)
    monkeypatch.setattr(
        benchmark,
        "docker_capacity",
        lambda: {
            "host": {"disk_free_bytes": 100 * benchmark.GIB},
            "docker": {"memory_bytes": 16 * benchmark.GIB},
        },
    )
    monkeypatch.setattr(
        benchmark,
        "generate_fixture",
        lambda *_args, **_kwargs: pytest.fail("fixture must not be materialized"),
    )
    monkeypatch.setattr(
        benchmark.docker_state,
        "inventory",
        lambda _project: pytest.fail("Docker inventory must not be claimed"),
    )
    cleanup_calls = []
    monkeypatch.setattr(
        benchmark, "cleanup", lambda *arguments: cleanup_calls.append(arguments)
    )

    assert benchmark.main() == 0
    report = (options.evidence_dir / "result.json").read_text()
    assert "NOT_RUN_RESOURCE_LIMIT" in report
    assert cleanup_calls == []
