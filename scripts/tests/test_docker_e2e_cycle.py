"""Guard the isolated runner against deleting application or pre-existing resources."""

import importlib.util
import json
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


@pytest.mark.parametrize("existing_kind", ["container", "volume", "network"])
def test_existing_project_is_never_started_or_deleted(monkeypatch, tmp_path, existing_kind):
    calls = []

    def execute(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[-1] == 'label=com.docker.compose.project=trackvance-certification':
            return ''
        if arguments[1:3] == ["ps", "-aq"] and existing_kind == "container":
            return "existing-container"
        if arguments[1:3] == ["volume", "ls"] and existing_kind == "volume":
            return "existing-volume"
        if arguments[1:3] == ["network", "ls"] and existing_kind == "network":
            return "existing-network"
        return ""

    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.sys, "argv", [
        "docker_e2e_cycle.py", "--project", "trackvance-v070-test-e2e-existing-0123456789ab",
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

    project = "trackvance-v070-test-e2e-new-0123456789ab"
    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.sys, "argv", [
        "docker_e2e_cycle.py", "--project", project,
        "--port", "3200", "--evidence-dir", str(tmp_path),
    ])
    assert runner.main() == 1
    assert start_environment["DEMO_ACCESS_ENABLED"] == "true"
    assert start_environment["DEMO_SEED_ENABLED"] == "true"
    down = next(command for command in calls if 'down' in command)
    assert down[-3:] == ['down', '-v', '--remove-orphans']
    assert down[down.index('-p') + 1] == project
    assert down[2:4] == ['--env-file', str(tmp_path / 'private.empty.env')]
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "FAIL" and result["cleanup"] == "PASS"


def test_storage_snapshot_does_not_depend_on_an_old_container_file(monkeypatch):
    submissions = []

    def execute(arguments, **kwargs):
        assert arguments[-2:] == ["-", "snapshot"]
        assert kwargs["capture"]
        assert "physical_schema_guard.py" in kwargs["input_text"] and "verify_storage.py" in kwargs["input_text"]
        compile(kwargs["input_text"], "<storage-verifier>", "exec")
        submissions.append(kwargs["input_text"])
        return '{"tables": {}}'

    monkeypatch.setattr(runner, "execute", execute)
    for _ in range(2):
        assert runner.storage_snapshot(["docker", "compose"], {}) == '{"tables": {}}'
    assert len(submissions) == 2


def test_ci_browser_upload_matches_guarded_runner_reports_and_excludes_private_files(tmp_path):
    workflow = (runner.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    artifact = workflow.split("name: ci-evidence-${{ matrix.group }}", 1)[1].split("include-hidden-files:", 1)[0]
    patterns = [line.strip() for line in artifact.splitlines() if line.strip().startswith(".codex-local/")]
    report_names = {"result.json", "browser-summary.json", "before.json", "after.json", "migrations.json"}
    evidence = tmp_path / ".codex-local" / "ci" / 'evidence' / 'attachments'
    evidence.mkdir(parents=True)
    for name in report_names | {"private.env", "server.log", "trace.zip"}:
        (evidence / name).write_text("{}", encoding="utf-8")

    uploaded = {path.name for pattern in patterns for candidate in tmp_path.glob(pattern)
                for path in ([candidate] if candidate.is_file() else candidate.rglob('*')) if path.is_file()}
    assert uploaded == report_names


def test_clean_demo_starts_without_seeding_and_uses_only_the_clean_browser_scenario(monkeypatch, tmp_path):
    calls, browser_arguments = [], []

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if "up" in arguments:
            assert kwargs["environment"]["DEMO_SEED_ENABLED"] == "false"
            assert kwargs["environment"]["TV_EXPECT_CLEAN_DEMO"] == "true"
        if "inspect" in arguments:
            return "no"
        if arguments[-2:] == ["ps", "-aq"]:
            return "isolated-api"
        return "{}" if kwargs.get("capture") and "snapshot" in arguments else ""

    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "pnpm")
    monkeypatch.setattr(runner, "run_browser", lambda _pnpm, arguments, **_kwargs:
                        browser_arguments.extend(arguments) or {"status": "PASS", "expected": 1})
    monkeypatch.setattr(runner.sys, "argv", ["docker_e2e_cycle.py", "--clean-demo",
                        "--project", "trackvance-v070-test-e2e-clean-0123456789ab", "--port", "3200",
                        "--evidence-dir", str(tmp_path)])
    assert runner.main() == 0
    assert browser_arguments == ["tests-e2e/demo-access-clean.spec.ts"]
    assert not any("scripts/smoke_test.py" in command for command in calls)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "PASS" and result["cleanup"] == "PASS"


def test_compose_isolates_source_and_delivery_secrets_by_worker_lane():
    compose = (runner.ROOT / "compose.yml").read_text(encoding="utf-8")
    shared, services = compose.split("services:", 1)
    api, remaining = services.split("  api:", 1)[1].split("  worker:", 1)
    worker, remaining = remaining.split("  delivery-worker:", 1)
    delivery_worker, remaining = remaining.split("  acquisition-worker:", 1)
    acquisition_worker, dispatchers = remaining.split("  scheduler:", 1)
    dispatchers = dispatchers.split("  web:", 1)[0]

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
    assert "- connection_credentials:/var/lib/trackvance-credentials" in acquisition_worker
    assert "- connection_keys:/var/lib/trackvance-keys" in acquisition_worker
    assert "delivery_credentials:/var/lib/trackvance-delivery-credentials" not in acquisition_worker
    assert "delivery_keys:/var/lib/trackvance-delivery-keys" not in acquisition_worker
    assert "TRACKVANCE_WORKER_LANE: ACQUISITION" in acquisition_worker
    for private_mount in (
        "connection_credentials:/var/lib/trackvance-credentials",
        "connection_keys:/var/lib/trackvance-keys",
        "delivery_credentials:/var/lib/trackvance-delivery-credentials",
        "delivery_keys:/var/lib/trackvance-delivery-keys",
    ):
        assert private_mount not in dispatchers


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
