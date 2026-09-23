"""Guard the isolated runner against deleting application or pre-existing resources."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "docker_e2e_cycle", Path(__file__).with_name("docker_e2e_cycle.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("name", ["trackvance-certification", "trackvance-core", "other-project"])
def test_rejects_application_project_names(name):
    with pytest.raises(ValueError, match="proyecto aislado"):
        runner.validated_project_name(name)


@pytest.mark.parametrize("existing_kind", ["container", "volume"])
def test_existing_project_is_never_started_or_deleted(monkeypatch, tmp_path, existing_kind):
    calls = []

    def execute(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["ps", "-aq"] and existing_kind == "container":
            return "existing-container"
        if arguments[1:3] == ["volume", "ls"] and existing_kind == "volume":
            return "existing-volume"
        return ""

    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.sys, "argv", [
        "docker_e2e_cycle.py", "--project", "trackvance-e2e-existing",
        "--port", "3200", "--evidence-dir", str(tmp_path),
    ])
    assert runner.main() == 1
    assert not any("up" in command or "down" in command for command in calls)


def test_failed_start_cleans_only_its_new_isolated_project(monkeypatch, tmp_path):
    calls = []
    start_environment = {}

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if "up" in arguments:
            start_environment.update(kwargs["environment"])
            raise RuntimeError("Container did not become healthy")
        return ""

    project = "trackvance-e2e-new-test"
    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.sys, "argv", [
        "docker_e2e_cycle.py", "--project", project,
        "--port", "3200", "--evidence-dir", str(tmp_path),
    ])
    assert runner.main() == 1
    assert start_environment["DEMO_ACCESS_ENABLED"] == "true"
    assert start_environment["DEMO_SEED_ENABLED"] == "true"
    assert calls[-1] == ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"]


def test_compose_isolates_source_and_delivery_secrets_by_worker_lane():
    compose = (runner.ROOT / "compose.yml").read_text(encoding="utf-8")
    shared, services = compose.split("services:", 1)
    api, remaining = services.split("  api:", 1)[1].split("  worker:", 1)
    worker, remaining = remaining.split("  delivery-worker:", 1)
    delivery_worker = remaining.split("  web:", 1)[0]

    assert "- trackvance_data:/var/lib/trackvance" in shared
    assert "connection_credentials:/var/lib/trackvance-credentials" not in shared
    assert "connection_keys:/var/lib/trackvance-keys" not in shared
    assert "delivery_credentials:/var/lib/trackvance-delivery-credentials" not in shared
    assert "delivery_keys:/var/lib/trackvance-delivery-keys" not in shared
    assert "- trackvance_data:/var/lib/trackvance" in api
    assert "- connection_credentials:/var/lib/trackvance-credentials" in api
    assert "- connection_keys:/var/lib/trackvance-keys" in api
    assert "- delivery_credentials:/var/lib/trackvance-delivery-credentials" in api
    assert "- delivery_keys:/var/lib/trackvance-delivery-keys" in api
    assert "connection_credentials:/var/lib/trackvance-credentials" not in worker
    assert "connection_keys:/var/lib/trackvance-keys" not in worker
    assert "delivery_credentials:/var/lib/trackvance-delivery-credentials" not in worker
    assert "delivery_keys:/var/lib/trackvance-delivery-keys" not in worker
    assert "TRACKVANCE_WORKER_LANE: DEFAULT" in worker
    assert "- trackvance_data:/var/lib/trackvance" in delivery_worker
    assert "connection_credentials:/var/lib/trackvance-credentials" not in delivery_worker
    assert "connection_keys:/var/lib/trackvance-keys" not in delivery_worker
    assert (
        "- delivery_credentials:/var/lib/trackvance-delivery-credentials"
        in delivery_worker
    )
    assert "- delivery_keys:/var/lib/trackvance-delivery-keys" in delivery_worker
    assert "TRACKVANCE_WORKER_LANE: DELIVERY" in delivery_worker


def test_direct_start_isolates_source_secrets_from_delivery_worker():
    script = (runner.ROOT / "scripts" / "start-local.ps1").read_text(
        encoding="utf-8"
    )
    delivery_setup = script.split(
        "$env:TRACKVANCE_WORKER_LANE = 'DELIVERY'", 1
    )[1].split("Start-TrackvanceProcess 'delivery-worker'", 1)[0]

    assert "$env:TRACKVANCE_SECRETS_DIR = $isolatedSourceSecretsDir" in delivery_setup
    assert (
        "$env:TRACKVANCE_SECRET_KEY_FILE = $isolatedSourceSecretKeyFile"
        in delivery_setup
    )
