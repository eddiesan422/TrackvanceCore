"""Authentic Catalog historical builds use durable ownership and finite builders."""
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_reports_recovery as recovery
from ci import local_resources as resources
from ci import run_suite
from test_ci_local_image_registry import BASELINE, EXECUTION, OWNED, PROJECT, SimulatedDocker


def controlled_environment(directory, version):
    return {"TRACKVANCE_LOCAL_EXECUTION_ID": EXECUTION,
        "TRACKVANCE_LOCAL_IMAGE_REGISTRY": str(directory / "owned-images.json"),
        "TRACKVANCE_LOCAL_BUILDER_REGISTRY": str(directory / "owned-builders.json"),
        "TRACKVANCE_LOCAL_MAX_MEMORY_BYTES": str(8 * 1024**3),
        "TRACKVANCE_LOCAL_MAX_CPUS": "2",
        "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA": resources.HISTORICAL_SOURCES[version],
        "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION": version}


@pytest.mark.parametrize("version", ["0.7.0", "0.8.0"])
@pytest.mark.parametrize("failure", [False, True])
def test_bounded_historical_build_records_both_oci_intents_before_builder_and_cleans_partial_load(tmp_path, monkeypatch, version, failure):
    context = tmp_path / "authentic"
    context.mkdir()
    env = controlled_environment(tmp_path, version)
    source = resources.HISTORICAL_SOURCES[version]
    docker, phases, removals = SimulatedDocker(), [], []
    # A conflicting ambient budget must not override this invocation's cap.
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_MEMORY_BYTES", str(16 * 1024**3))
    monkeypatch.setenv("TRACKVANCE_LOCAL_MAX_CPUS", "8")
    resolved = {"services": {service: {"image": f"{PROJECT}:{role}",
        "build": {"context": str(context.resolve()), "dockerfile": dockerfile}}
        for service, role, dockerfile in [("api", "backend", "backend/Dockerfile"),
                                        ("web", "web", "deploy/docker/frontend.Dockerfile")]}}

    def simulated_run(arguments, **kwargs):
        assert arguments[0] == "docker", "No real Docker command is permitted in this test"
        if "config" in arguments:
            return SimpleNamespace(stdout=json.dumps(resolved), returncode=0)
        if arguments[1:3] == ["buildx", "rm"]:
            removals.append(arguments[3])
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(stdout=docker(*arguments[1:]), returncode=0)

    monkeypatch.setattr(subprocess, "run", simulated_run)
    monkeypatch.setattr(resources, "historical_source_proof", lambda *_args: {
        "source_sha": source, "source_version": version, "tree_sha": "a" * 40,
        "tracked_blobs_sha256": "b" * 64})

    def simulated_execute(arguments, directory, phase, timeout, **options):
        phases.append(phase)
        registry = json.loads(Path(env["TRACKVANCE_LOCAL_IMAGE_REGISTRY"]).read_text())
        assert len(registry["builds"]) == 2
        assert all(row["status"] == "BUILD_PENDING" and row["source_sha"] == source
                   and row["version"] == version for row in registry["builds"])
        ownership = json.loads((tmp_path / "local-legacy-image-ownership.json").read_text())
        for service, role in [("api", "backend"), ("web", "web")]:
            labels = ownership["services"][service]["build"]["labels"]
            assert labels["org.opencontainers.image.revision"] == source
            assert labels["org.opencontainers.image.version"] == version
            assert labels["io.trackvance.local-execution"] == EXECUTION
            assert labels["io.trackvance.local-project"] == PROJECT
            assert labels["io.trackvance.local-role"] == role
        if phase == "local-builder-create":
            assert "memory=3221225472" in arguments and "memory-swap=3221225472" in arguments
            assert "cpu-quota=100000" in arguments and "cpu-period=100000" in arguments
            assert "restart-policy=no" in arguments and "docker-container" in arguments
        else:
            assert phase == "local-authentic-source-build" and "--builder" in arguments
            assert options["cwd"] == context
            assert arguments[-2:] == ["api", "web"] and "up" not in arguments
            docker.install(OWNED, [], registry["builds"][0]["expected_labels"])
            if failure:
                raise subprocess.CalledProcessError(23, ["simulated-historical-build"])
        return {"status": "PASS", "exit_code": 0}

    monkeypatch.setattr(run_suite, "execute", simulated_execute)
    compose = ["docker", "compose", "-p", PROJECT, "-f", str(context / "compose.yml")]
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            resources.build_legacy(compose, tmp_path, PROJECT, env, source_context=context)
    else:
        resources.build_legacy(compose, tmp_path, PROJECT, env, source_context=context)
    assert phases == ["local-builder-create", "local-authentic-source-build"]
    assert removals == json.loads(Path(env["TRACKVANCE_LOCAL_BUILDER_REGISTRY"]).read_text())
    registry = json.loads(Path(env["TRACKVANCE_LOCAL_IMAGE_REGISTRY"]).read_text())
    assert [row["status"] for row in registry["builds"]] == ["OWNED", "NOT_LOADED"]
    assert registry["builds"][0]["source_tree_sha"] == "a" * 40
    cleanup = resources.cleanup_registered_images(Path(env["TRACKVANCE_LOCAL_IMAGE_REGISTRY"]), EXECUTION, command=docker)
    assert [(row["source_sha"], row["version"]) for row in cleanup["removed"]] == [(source, version)]
    assert set(docker.images) == {BASELINE}


@pytest.mark.parametrize("version", ["0.7.0", "0.8.0"])
def test_catalog_connection_passes_exact_source_and_authentic_context_to_guard(tmp_path, monkeypatch, version):
    env = controlled_environment(tmp_path, version)
    env["POSTGRES_PASSWORD"] = "own-private-fixture"
    captured = []
    monkeypatch.setattr(resources, "build_legacy", lambda *args, **options: captured.append((args, options)))
    baseline = tmp_path / "authentic"
    recovery.build_historical_source(tmp_path, {"project": PROJECT, "compose_base": str(baseline / "compose.yml")}, baseline, version, env)
    assert len(captured) == 1
    args, options = captured[0]
    assert options == {"source_context": baseline}
    assert args[1:3] == (tmp_path, PROJECT)
    assert args[3]["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA"] == resources.HISTORICAL_SOURCES[version]
    assert args[3]["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION"] == version
    assert args[0][args[0].index("-f") + 1] == str(baseline / "compose.yml")


@pytest.mark.parametrize("problem", ["invalid_sha", "current_version", "outside_context", "invalid_cpu"])
def test_invalid_source_or_budget_rejected_before_any_docker_read(tmp_path, monkeypatch, problem):
    env = controlled_environment(tmp_path, "0.7.0")
    context = tmp_path / "authentic"
    if problem == "invalid_sha":
        env["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA"] = "0" * 40
    elif problem == "current_version":
        env["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION"] = "0.8.5"
    elif problem == "outside_context":
        context = tmp_path.parent / "foreign"
    else:
        env["TRACKVANCE_LOCAL_MAX_CPUS"] = "nan"
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Invalid ownership must not reach Docker/Git"))
    with pytest.raises(ValueError):
        resources.build_legacy(["docker", "compose", "-f", str(context / "compose.yml")], tmp_path, PROJECT, env, source_context=context)


def test_catalog_missing_controlled_run_never_falls_back_to_plain_compose_build(tmp_path, monkeypatch):
    for name in list(recovery.os.environ):
        if name.startswith("TRACKVANCE_LOCAL_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(resources, "build_legacy", lambda *_args, **_options: pytest.fail("Unbudgeted build must never run"))
    with pytest.raises(ValueError, match="run_local"):
        recovery.build_historical_source(tmp_path, {"project": PROJECT}, tmp_path / "authentic", "0.7.0", {})


@pytest.mark.parametrize("version", ["0.7.0", "0.8.0"])
def test_new_authentic_sources_validate_real_git_archive_and_reject_changed_blob(tmp_path, version):
    source = resources.HISTORICAL_SOURCES[version]
    repository = Path(resources.__file__).resolve().parents[2]
    archive = subprocess.run(["git", "-C", str(repository), "archive", "--format=zip", source],
                             check=True, capture_output=True, timeout=90).stdout
    context = tmp_path / "authentic"
    context.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        package.extractall(context)
    proof = resources.historical_source_proof(repository, context, source, version)
    assert proof["source_sha"] == source and proof["source_version"] == version
    assert proof["tracked_files"] > 100 and len(proof["build_definition_blobs"]) == 3
    target = context / "backend/Dockerfile"
    target.write_bytes(target.read_bytes() + b"\n# changed owned test fixture\n")
    with pytest.raises(ValueError, match="committed blob"):
        resources.historical_source_proof(repository, context, source, version)
