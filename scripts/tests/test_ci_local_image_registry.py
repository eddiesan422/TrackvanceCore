"""Independent failure-window and final-inspection guards; Docker is simulated."""
import copy
import json
import subprocess
from types import SimpleNamespace

import pytest
from ci import local_resources as resources
from ci import run_local

EXECUTION = "local-" + "1" * 32
SOURCE = "2" * 40
PROJECT = "trackvance-v080-test-registry-" + "3" * 12
OWNED = "sha256:" + "4" * 64
BASELINE = "sha256:" + "5" * 64
REFERENCE = PROJECT + ":backend"


class SimulatedDocker:
    def __init__(self):
        self.images = {}
        self.containers = []
        self.calls = []
        self.before_consumer_read = None
        self.install(BASELINE, ["trackvance-ci:backend"], {})

    def install(self, identifier, tags, labels):
        self.images[identifier] = {"Id": identifier, "RepoTags": list(tags), "RepoDigests": [],
            "Config": {"Labels": copy.deepcopy(labels), "Env": ["PRIVATE_FIXTURE=value"]},
            "RootFS": {"Type": "layers", "Layers": ["sha256:" + "6" * 64]},
            "Created": "2026-10-07T00:00:00Z", "Size": 2048}

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("image", "ls"):
            rows = list(self.images.values())
            if "--filter" in args:
                expressions = [args[n + 1].removeprefix("label=") for n, arg in enumerate(args) if arg == "--filter"]
                rows = [row for row in rows if all(row["Config"]["Labels"].get(key) == value
                    for key, value in (expression.split("=", 1) for expression in expressions))]
            elif len(args) == 5:
                rows = [row for row in rows if args[-1] in row["RepoTags"]]
            return "\n".join(row["Id"] for row in rows)
        if args[:2] == ("image", "inspect"):
            return json.dumps([copy.deepcopy(self.images[i]) for i in args[2:]])
        if args[:3] == ("ps", "-aq", "--no-trunc"):
            if self.before_consumer_read:
                callback, self.before_consumer_read = self.before_consumer_read, None
                callback()
            return "\n".join(row["Id"] for row in self.containers)
        if args[0] == "inspect":
            return json.dumps([copy.deepcopy(row) for row in self.containers if row["Id"] in args[1:]])
        if args[:2] == ("buildx", "ls"):
            return "default\ndesktop-linux"
        assert args[:2] == ("image", "rm") and len(args) == 3 and args[2].startswith("sha256:")
        assert not any(row["Image"] == args[2] for row in self.containers), "Stopped consumers prohibit non-forced deletion"
        del self.images[args[2]]
        return ""


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("No real Docker or shell process in registry tests"))


def registered(tmp_path):
    docker, path = SimulatedDocker(), tmp_path / "owned-images.json"
    labels = resources.begin_image_build(path, EXECUTION, PROJECT, SOURCE, "0.8.0",
        "backend", REFERENCE, docker)
    docker.install(OWNED, [REFERENCE], labels)
    resources.observe_image_builds(path, EXECUTION, docker)
    return docker, path


@pytest.mark.parametrize("change", ["tag", "digest", "config", "rootfs", "size", "created"])
def test_late_identity_change_between_observation_and_final_inspection_is_preserved(tmp_path, change):
    docker, path = registered(tmp_path)
    original = json.loads(path.read_text())["builds"][0]["image"]
    def mutate():
        row = docker.images[OWNED]
        if change == "tag":
            row["RepoTags"].append("bikerwash:shared")
        elif change == "digest":
            row["RepoDigests"].append("registry/foreign@sha256:" + "7" * 64)
        elif change == "config":
            row["Config"]["Env"].append("LATE_PRIVATE_FIXTURE=changed")
        elif change == "rootfs":
            row["RootFS"]["Layers"].append("sha256:" + "8" * 64)
        elif change == "size":
            row["Size"] += 1
        else:
            row["Created"] = "2026-10-07T01:00:00Z"
    docker.before_consumer_read = mutate
    report = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert report["removed"] == [] and report["skipped"][0]["reason"] == "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"
    assert json.loads(path.read_text())["builds"][0]["image"] == original
    assert set(docker.images) == {BASELINE, OWNED}
    assert not any(call[:2] == ("image", "rm") for call in docker.calls)


@pytest.mark.parametrize("by_reference", [False, True])
def test_stopped_consumer_appearing_after_observation_prevents_deletion(tmp_path, by_reference):
    docker, path = registered(tmp_path)
    def consume():
        docker.containers.append({"Id": "stopped-foreign-container", "Image": BASELINE if by_reference else OWNED,
            "Config": {"Image": REFERENCE if by_reference else OWNED,
                       "Labels": {"com.docker.compose.project": "bikerwash"}}, "State": {"Status": "exited"}})
    docker.before_consumer_read = consume
    report = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert report["removed"] == [] and report["skipped"][0]["reason"] == "CONTAINER_CONSUMER"
    assert report["skipped"][0]["consumers"][0]["status"] == "exited"
    assert set(docker.images) == {BASELINE, OWNED}


def test_late_foreign_reference_exception_is_durable_after_that_reference_disappears(tmp_path):
    docker, path = registered(tmp_path)
    docker.before_consumer_read = lambda: docker.images[OWNED]["RepoTags"].append("foreign/shared:stable")
    first = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert first["removed"] == [] and first["skipped"][0]["reason"] == "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"
    docker.images[OWNED]["RepoTags"] = [REFERENCE]
    resumed = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert resumed["removed"] == [] and resumed["skipped"], "A previously observed ownership exception needs explicit review"
    assert set(docker.images) == {BASELINE, OWNED}


def test_own_digest_added_at_final_inspection_is_never_adopted_even_if_it_disappears(tmp_path):
    docker, path = registered(tmp_path)
    digest = PROJECT + "@" + OWNED
    def add_digest():
        docker.images[OWNED]["RepoDigests"].append(digest)
    docker.before_consumer_read = add_digest
    report = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert report["removed"] == [] and report["skipped"][0]["reason"] == "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"
    record = json.loads(path.read_text())["builds"][0]
    assert record["registered_digests"] == [] and record["image"]["digests"] == []
    assert record["observed_change"]["digests"] == [digest]
    docker.images[OWNED]["RepoDigests"] = []
    resumed = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert resumed["removed"] == [] and resumed["skipped"][0]["reason"] == "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"
    assert set(docker.images) == {BASELINE, OWNED}
    assert not any(call[:2] == ("image", "rm") for call in docker.calls)


def test_registry_resume_and_project_filter_never_claim_other_execution_or_project(tmp_path):
    docker, path = registered(tmp_path)
    snapshot = path.read_bytes()
    calls = len(docker.calls)
    with pytest.raises(ValueError, match="another execution"):
        resources.cleanup_registered_images(path, "local-" + "9" * 32, command=docker)
    assert len(docker.calls) == calls and path.read_bytes() == snapshot
    assert resources.cleanup_registered_images(path, EXECUTION, projects={"another-project"}, command=docker)["removed"] == []
    report = resources.cleanup_registered_images(path, EXECUTION, projects={PROJECT}, command=docker)
    assert report["removed"] == [{"image_id": OWNED, "reference": REFERENCE, "project": PROJECT,
        "execution_id": EXECUTION, "source_sha": SOURCE, "version": "0.8.0"}]
    assert set(docker.images) == {BASELINE}
    assert [call for call in docker.calls if call[:2] == ("image", "rm")] == [("image", "rm", OWNED)]


@pytest.mark.parametrize("failure", ["failure", "timeout", "cancelled"])
@pytest.mark.parametrize("partial_load", ["not_loaded", "tagged", "tag_lost"])
def test_prepare_failure_keeps_durable_intent_and_recovers_only_actual_partial_load(
        tmp_path, monkeypatch, failure, partial_load):
    directory = tmp_path / EXECUTION
    directory.mkdir()
    path = directory / "owned-images.json"
    docker = SimulatedDocker()
    environment = {"TRACKVANCE_LOCAL_EXECUTION_ID": EXECUTION, "TRACKVANCE_LOCAL_IMAGE_REGISTRY": str(path)}
    phases = []
    fault = {"failure": subprocess.CalledProcessError(23, ["simulated-build"]),
             "timeout": subprocess.TimeoutExpired(["simulated-build"], 1),
             "cancelled": KeyboardInterrupt()}[failure]
    def execute(arguments, _directory, phase, _timeout, **kwargs):
        phases.append(phase)
        if phase == "build-backend":
            pending = json.loads(path.read_text())
            record = pending["builds"][0]
            assert record["status"] == "BUILD_PENDING" and record["image"] is None
            assert record["execution_id"] == EXECUTION and record["source_sha"] == SOURCE and record["version"] == "0.8.5"
            assert record["expected_labels"]["io.trackvance.local-project"] == "trackvance-v070-test-images-" + EXECUTION[-12:]
            assert "--load" in arguments
            if partial_load != "not_loaded":
                docker.install(OWNED, [record["reference"]] if partial_load == "tagged" else [], record["expected_labels"])
            raise fault
        return {"status": "PASS", "exit_code": 0}
    monkeypatch.setattr(run_local, "command", lambda executable, *args: docker(*args))
    monkeypatch.setattr(run_local, "execute", execute)
    monkeypatch.setattr(run_local, "inspect_image", lambda *args: pytest.fail("Failed build cannot issue a successful image proof"))
    with pytest.raises(type(fault)):
        run_local.prepare_images(directory, SOURCE, True, max_memory_bytes=2 * 1024**3,
                                 max_cpus=1, environment=environment)
    assert phases == ["build-builder-create", "build-backend", "build-builder-cleanup"]
    assert not (directory / "local-images.json").exists()
    record = json.loads(path.read_text())["builds"][0]
    assert record["status"] == ("NOT_LOADED" if partial_load == "not_loaded" else "OWNED")
    assert "PRIVATE_FIXTURE=value" not in path.read_text()
    report = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert [row["image_id"] for row in report["removed"]] == ([] if partial_load == "not_loaded" else [OWNED])
    assert set(docker.images) == {BASELINE}
    assert all(call == ("image", "rm", OWNED) for call in docker.calls if call[:2] == ("image", "rm"))


@pytest.mark.parametrize("failure", ["failure", "timeout", "cancelled"])
def test_authentic_legacy_partial_load_finally_records_ids_and_removes_builder(tmp_path, monkeypatch, failure):
    from ci import run_suite
    directory = tmp_path / "legacy-build"
    context = directory / "baseline"
    context.mkdir(parents=True)
    path = directory / "owned-images.json"
    builders = directory / "owned-builders.json"
    source = resources.HISTORICAL_SOURCES["0.6.1"]
    environment = {"TRACKVANCE_LOCAL_EXECUTION_ID": EXECUTION, "TRACKVANCE_LOCAL_IMAGE_REGISTRY": str(path),
        "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA": source, "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION": "0.6.1",
        "TRACKVANCE_LOCAL_BUILDER_REGISTRY": str(builders), "TRACKVANCE_LOCAL_MAX_MEMORY_BYTES": str(2 * 1024**3),
        "TRACKVANCE_LOCAL_MAX_CPUS": "1"}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    docker, phases, builder_removals = SimulatedDocker(), [], []
    resolved = {"services": {service: {"image": f"{PROJECT}:{role}",
        "build": {"context": str(context.resolve()), "dockerfile": dockerfile}}
        for service, role, dockerfile in [("api", "backend", "backend/Dockerfile"),
                                        ("web", "web", "deploy/docker/frontend.Dockerfile")]}}
    def run(arguments, **kwargs):
        assert arguments[0] == "docker", "All subprocess operations are explicitly simulated"
        if "config" in arguments:
            return SimpleNamespace(stdout=json.dumps(resolved), returncode=0)
        if arguments[1:3] == ["buildx", "rm"]:
            builder_removals.append(arguments[3])
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(stdout=docker(*arguments[1:]), returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(resources, "historical_source_proof", lambda *_args: {
        "repository": "eddiesan422/TrackvanceCore", "source_sha": source, "source_version": "0.6.1",
        "tree_sha": "a" * 40, "tracked_blobs_sha256": "b" * 64})
    fault = {"failure": subprocess.CalledProcessError(23, ["simulated-legacy-build"]),
             "timeout": subprocess.TimeoutExpired(["simulated-legacy-build"], 1),
             "cancelled": KeyboardInterrupt()}[failure]
    def execute(arguments, _directory, phase, _timeout, **kwargs):
        phases.append(phase)
        durable = json.loads(path.read_text())
        assert len(durable["builds"]) == 2 and all(row["status"] == "BUILD_PENDING" for row in durable["builds"])
        assert all(row["source_sha"] == source and row["version"] == "0.6.1" for row in durable["builds"])
        if phase == "local-authentic-source-build":
            record = durable["builds"][0]
            assert kwargs["cwd"] == context and "--builder" in arguments
            docker.install(OWNED, [], record["expected_labels"])  # Load succeeded, tag was lost before command failed.
            raise fault
        return {"status": "PASS", "exit_code": 0}
    monkeypatch.setattr(run_suite, "execute", execute)
    compose = ["docker", "compose", "-p", PROJECT, "-f", str(context / "compose.yml")]
    with pytest.raises(type(fault)):
        resources.build_legacy(compose, directory, PROJECT, environment)
    assert phases == ["local-builder-create", "local-authentic-source-build"]
    assert builder_removals == json.loads(builders.read_text()) and len(builder_removals) == 1
    durable = json.loads(path.read_text())
    assert [row["status"] for row in durable["builds"]] == ["OWNED", "NOT_LOADED"]
    assert durable["builds"][0]["image"]["image_id"] == OWNED
    assert durable["builds"][0]["source_tree_sha"] == "a" * 40
    report = resources.cleanup_registered_images(path, EXECUTION, command=docker)
    assert [row["source_sha"] for row in report["removed"]] == [source]
    assert [row["version"] for row in report["removed"]] == ["0.6.1"]
    assert set(docker.images) == {BASELINE}
    assert [call for call in docker.calls if call[:2] == ("image", "rm")] == [("image", "rm", OWNED)]
