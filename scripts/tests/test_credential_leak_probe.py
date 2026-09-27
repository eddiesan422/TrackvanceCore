"""The privacy probe reads only isolated resources and never prints secrets."""
import io
import json

import credential_leak_probe as probe
import pytest


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-core", "../trackvance-e2e-x"])
def test_rejects_primary_or_unsafe_projects(project):
    with pytest.raises(ValueError, match="desechable"):
        probe.validated_project(project)


@pytest.mark.parametrize("surface", ["database", "logs", "artifacts", "browser", "none"])
def test_checks_persistent_surfaces_without_recording_secret(monkeypatch, tmp_path, surface):
    secret = "synthetic-one-time-password-never-published"
    project = "trackvance-identity-e2e-test"
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    def execute(args, *, input_text=None):
        if args[1] == "ps":
            return "api-id api\npostgres-id postgres\n"
        if args[1] == "logs":
            return secret if surface == "logs" else "normal logs"
        if "sh" in args:
            return secret if surface == "database" else "INSERT INTO users VALUES ('argon2-hash');"
        assert json.loads(input_text)["secrets"] == [secret]
        return json.dumps({"status": "FAIL" if surface == "artifacts" else "PASS", "files_scanned": 2})
    monkeypatch.setattr(probe, "execute", execute)
    if surface == "browser":
        output = tmp_path/".codex-local/browser-results"/project
        output.mkdir(parents=True)
        (output/"error-context.md").write_text(secret)
    if surface == "none":
        result = probe.scan(project, [secret])
        assert result["status"] == "PASS"
        assert secret not in json.dumps(result)
    else:
        with pytest.raises(RuntimeError) as failure:
            probe.scan(project, [secret])
        assert secret not in str(failure.value)


def test_cli_does_not_echo_subprocess_exception_or_stdin(monkeypatch, capsys):
    secret = "synthetic-one-time-password-never-published"
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--project", "trackvance-ci"])
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO(json.dumps({"secrets": [secret]})))
    def fail(*_):
        raise RuntimeError(secret)
    monkeypatch.setattr(probe, "scan", fail)
    assert probe.main() == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert json.loads(output.out)["status"] == "FAIL"
