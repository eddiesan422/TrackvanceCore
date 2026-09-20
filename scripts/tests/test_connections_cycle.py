"""Only disposable connection test resources may be provisioned or deleted."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("connections_cycle", Path(__file__).with_name("connections_cycle.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-core", "trackvance-e2e-other", "other"])
def test_connections_runner_rejects_nonisolated_projects(project):
    with pytest.raises(ValueError, match="proyecto aislado"):
        runner.validated_project_name(project)


@pytest.mark.parametrize("kind", ["container", "volume"])
def test_connections_runner_never_mutates_existing_resources(monkeypatch, tmp_path, kind):
    calls = []

    def execute(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["ps", "-aq"] and kind == "container":
            return "existing-container"
        if arguments[1:3] == ["volume", "ls"] and kind == "volume":
            return "existing-volume"
        return ""

    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.sys, "argv", ["connections_cycle.py", "--project", "trackvance-connections-e2e-existing", "--port", "3200", "--evidence-dir", str(tmp_path)])
    assert runner.main() == 1
    assert not any("up" in command or "down" in command for command in calls)


def test_connections_runner_failure_cleans_only_new_project_and_redacts_secrets(monkeypatch, tmp_path):
    calls = []
    project = "trackvance-connections-e2e-new"

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if "up" in arguments:
            raise RuntimeError("Cannot start: " + kwargs["environment"]["SOURCE_POSTGRES_PASSWORD"])
        return ""

    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _length: "never-persist-this-credential")
    monkeypatch.setattr(runner.sys, "argv", ["connections_cycle.py", "--project", project, "--port", "3200", "--evidence-dir", str(tmp_path)])
    assert runner.main() == 1
    assert calls[-1] == ["docker", "compose", "-p", project, "-f", "compose.yml", "-f", "deploy/docker/compose.connections-test.yml", "down", "-v", "--remove-orphans"]
    evidence = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "never-persist-this-credential" not in evidence
    assert "[REDACTED]" in evidence
