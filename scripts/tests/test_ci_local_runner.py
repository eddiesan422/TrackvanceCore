"""Local provenance, resource limits and historical profiles cannot conflate CI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.common import load_manifest, profile_groups
from ci.evidence import ci_context, write_receipt
from ci.local_resources import apply_limits
from ci.run_local import capacity, main, plan, retain_reports
from ci.validators import validate_content


def test_deep_plan_preserves_historical_sizes_and_does_not_touch_docker(monkeypatch, capsys):
    monkeypatch.setattr("ci.run_local.command", lambda *_a: pytest.fail("Plan must not invoke Docker or Git"))
    assert main(["--all", "--plan"]) == 0
    result = json.loads(capsys.readouterr().out)
    rows = {s.get("rows") for g in result["groups"] for s in g["scenarios"]}
    tiers = {s.get("tier_mib") for g in result["groups"] for s in g["scenarios"]}
    assert {400000, 1000000, 1000001} <= rows and {100, 500, 1024} <= tiers
    assert len(result["groups"]) == 19 and result["origin"] == "local"


def test_local_identity_is_explicit_and_has_no_github_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKVANCE_LOCAL_EXECUTION_ID", "local-" + "a" * 32)
    monkeypatch.setenv("TRACKVANCE_LOCAL_GROUP", "benchmark-smoke")
    identity = ci_context()
    path = write_receipt(tmp_path, group="benchmark-smoke", scenario_id="benchmark-real-smoke",
        result={"status": "PASS"}, source_sha="b" * 40, ci=identity)
    record = json.loads(path.read_text())
    assert record["ci"] is None and record["execution"] == identity
    assert not ({"run_id", "run_attempt", "job_id"} & record["execution"].keys())


def test_aggregate_budget_and_ownership_apply_only_to_generated_local_resources(tmp_path, monkeypatch):
    registry = tmp_path / "projects.json"
    project = "trackvance-v070-test-core-" + "a" * 12
    monkeypatch.setenv("TRACKVANCE_LOCAL_EXECUTION_ID", "local-" + "b" * 32)
    monkeypatch.setenv("TRACKVANCE_LOCAL_PROJECT_REGISTRY", str(registry))
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_CPUS", "2")
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_MEMORY_BYTES", str(4 * 1024**3))
    override = {"services": {"api": {"cpus": 1, "mem_limit": "1g"}, "worker": {"cpus": 2, "mem_limit": "6g"}}}
    apply_limits(override, project)
    assert sum(s["cpus"] for s in override["services"].values()) <= 2
    assert sum(s["mem_limit"] for s in override["services"].values()) <= 4 * 1024**3
    assert json.loads(registry.read_text()) == [project]


def test_unexecuted_capacity_cannot_be_mistaken_for_smaller_historical_fixture():
    manifest = load_manifest()
    group = plan(manifest, ["async-volume-1024"])[0]
    assert group["minimum_capacity"]["memory_bytes"] > 4 * 1024**3
    assert all(s.get("rows") == 1000000 for s in group["scenarios"])
    assert capacity("spark-standalone")["cpus"] == 4.5
    assert set(profile_groups(manifest, "deep")) - set(profile_groups(manifest, "functional"))


def test_small_cancel_requires_real_controlled_observation_and_no_partial_version():
    spec = {"validator": "xlsx-cancel", "rows": 120, "variant": "shared", "min_observed_rows": 20}
    value = {"status": "CANCELLED", "versions": 0, "observed_records_before_cancel": 20,
        "controlled_checkpoint": True, "fixture": {"rows": 120, "strings": "shared"}}
    validate_content(spec, value)
    for field, altered in (("controlled_checkpoint", False), ("versions", 1), ("observed_records_before_cancel", 0)):
        with pytest.raises(ValueError):
            validate_content(spec, value | {field: altered})


def test_retention_never_deletes_unknown_or_current_evidence(tmp_path):
    owned = []
    for character in "abc":
        path = tmp_path / ("local-" + character * 32)
        path.mkdir()
        (path / "local-summary.json").write_text("{}")
        owned.append(path)
    unknown = tmp_path / "trackvance-certification"
    unknown.mkdir()
    retain_reports(tmp_path, owned[-1], 1)
    assert owned[-1].exists() and unknown.exists()


def test_local_build_is_bounded_unique_and_labels_the_version_before_verification(tmp_path, monkeypatch):
    from ci import run_local
    directory = tmp_path / ("local-" + "a" * 32)
    directory.mkdir()
    observed = []
    monkeypatch.setattr(run_local, "command", lambda *_a: "")
    def execute(command, *_a, **_k):
        observed.append(command)
        return {"status": "PASS"}
    monkeypatch.setattr(run_local, "execute", execute)
    def inspect(reference, _sha):
        assert reference.startswith("trackvance-local-proof:")
        build = next(row for row in observed if reference in row)
        assert "org.opencontainers.image.version=0.8.0" in build
        assert "--builder" in build and "--load" in build
        return {"Id": "sha256:" + "b" * 64}
    monkeypatch.setattr(run_local, "inspect_image", inspect)
    proof = run_local.prepare_images(directory, "c" * 40, True, max_memory_bytes=3 * 1024**3, max_cpus=1)
    create = observed[0]
    assert "docker-container" in create and "cpu-quota=100000" in create and "memory=3221225472" in create
    assert observed[-1][:3] == ["docker", "buildx", "rm"]
    assert json.loads(proof.read_text())["status"] == "PASS"


def test_one_cpu_one_gib_representative_runs_frontend_only_without_images_or_stacks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ci import owned_cleanup, run_local
    executed = []
    protected = {"trackvance-certification": [{"memory_limit_bytes": 15 * run_local.GIB}]}
    monkeypatch.setattr(run_local, "ROOT", tmp_path)
    monkeypatch.setattr(run_local, "protected_inventory", lambda: protected)
    monkeypatch.setattr(owned_cleanup, "snapshot", lambda: {"containers": set(), "networks": set(), "volumes": set()})
    monkeypatch.setattr(run_local, "prepare_images", lambda *_a, **_k: pytest.fail("Pending groups must not prepare Docker images"))
    def command(*args):
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "a" * 40
        if args[:2] == ("git", "status"):
            return ""
        if args[:2] == ("docker", "info"):
            return json.dumps({"ServerVersion": "test", "MemTotal": 16 * run_local.GIB})
        if args[:3] == ("docker", "context", "show"):
            return "test-owned-context"
        if args[:3] == ("docker", "image", "ls"):
            return ""
        pytest.fail("Unexpected Docker operation: " + " ".join(args[:3]))
    monkeypatch.setattr(run_local, "command", command)
    monkeypatch.setattr(run_local, "ResourceMonitor", lambda _registry: SimpleNamespace(
        thread=SimpleNamespace(start=lambda: None), finish=lambda: {"status": "TEST_DOUBLE"}))
    def execute(arguments, *_a, **kwargs):
        executed.append(arguments)
        assert kwargs["environment"]["NODE_OPTIONS"] == "--max-old-space-size=384"
        return {"status": "PASS"}
    monkeypatch.setattr(run_local, "execute", execute)
    assert run_local.main(["--all", "--max-cpus", "1", "--max-memory-gib", "1"]) == 1
    assert len(executed) == 1 and executed[0][executed[0].index("--group") + 1] == "frontend"
    summary = json.loads(next((tmp_path / ".codex-local/local-deep").glob("*/local-summary.json")).read_text())
    assert summary["status"] == "INCOMPLETE" and summary["closure"] == "PARTIAL_DEEP_EXECUTION"
    assert len(summary["groups"]) == 19
    assert sum(row["status"] == "PENDING_CAPACITY" for row in summary["groups"]) == 18
    assert summary["protected_inventory_unchanged"] is True
