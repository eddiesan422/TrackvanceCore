"""Browser failures must never publish request bodies or disposable passwords."""
import json
from types import SimpleNamespace

import browser_evidence
import pytest


def probe_report(value):
    return {"suites": [{"suites": [{"specs": [{"tests": [{"results": [{"stdout": [
        {"text": "unrelated output\nCREDENTIAL_PRIVACY_PROBE " + json.dumps(value) + "\n"}
    ]}]}]}]}]}]}


def test_privacy_probe_whitelists_only_statuses_and_counts():
    aggregate = {"status": "PASS", "database": "PASS", "container_logs": "PASS", "artifacts": "PASS",
                 "secrets_scanned": 12, "artifact_files_scanned": 2, "browser_files_scanned": 1}
    assert browser_evidence.credential_privacy_reports(probe_report(aggregate)) == [aggregate]


@pytest.mark.parametrize("change", [
    {"password": "synthetic-sensitive-value"}, {"database": "synthetic-sensitive-value"},
    {"secrets_scanned": "synthetic-sensitive-value"}, {"secrets_scanned": True},
    {"artifact_files_scanned": -1}, {"browser_files_scanned": 1.2},
    {"status": ["PASS"]}, {"status": {"password": "synthetic-sensitive-value"}},
])
def test_privacy_probe_rejects_arbitrary_keys_values_and_non_integer_counts(change):
    aggregate = {"status": "PASS", "database": "PASS", "container_logs": "PASS", "artifacts": "PASS",
                 "secrets_scanned": 12, "artifact_files_scanned": 2, "browser_files_scanned": 1, **change}
    with pytest.raises(RuntimeError, match="contenido omitido") as failure:
        browser_evidence.credential_privacy_reports(probe_report(aggregate))
    assert "synthetic-sensitive-value" not in str(failure.value)


def test_failed_probe_cannot_publish_a_pass_summary(monkeypatch, tmp_path):
    aggregate = {"status": "FAIL", "database": "FAIL", "container_logs": "PASS", "artifacts": "PASS",
                 "secrets_scanned": 12, "artifact_files_scanned": 2, "browser_files_scanned": 1}
    report = probe_report(aggregate)
    report["stats"] = {"expected": 1, "unexpected": 0}
    monkeypatch.setattr(browser_evidence.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr=""))
    evidence = tmp_path / "published"
    evidence.mkdir()
    with pytest.raises(RuntimeError, match="privacidad de credenciales falló"):
        browser_evidence.run_browser("pnpm", [], root=tmp_path, project="isolated-test",
                                     environment={}, evidence=evidence)
    assert json.loads((evidence / "browser-summary.json").read_text())["status"] == "FAIL"


def test_failed_assertion_publishes_only_location_and_counts(monkeypatch, tmp_path, capsys):
    secret = "disposable-password-for-redaction-test"
    report = {"stats": {"expected": 0, "unexpected": 1}, "suites": [{"specs": [{
        "title": "local login", "tests": [{"status": "unexpected", "results": [{
            "status": "failed", "error": {"message": secret, "stack": secret,
                                           "location": {"file": "identity.spec.ts", "line": 10}}
        }]}]
    }]}]}
    monkeypatch.setattr(browser_evidence.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1, stdout=json.dumps(report), stderr=secret))
    evidence = tmp_path / "published"
    evidence.mkdir()
    with pytest.raises(RuntimeError, match="Playwright falló"):
        browser_evidence.run_browser("pnpm", [], root=tmp_path, project="isolated-test",
                                     environment={"TV_CONNECTIONS_PASSWORD": secret}, evidence=evidence)
    published = (evidence / "browser-summary.json").read_text()
    assert secret not in published + capsys.readouterr().out
    assert json.loads(published)["failures"][0]["location"]["line"] == 10
    assert list(evidence.iterdir()) == [evidence / "browser-summary.json"]


def test_raw_artifact_containing_password_blocks_publication(monkeypatch, tmp_path):
    secret = "disposable-password-for-artifact-test"
    def run(*_args, **_kwargs):
        artifact = tmp_path / ".codex-local" / "browser-results" / "isolated-test" / "error-context.md"
        artifact.write_text(secret)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"stats": {"expected": 1}}), stderr="")
    monkeypatch.setattr(browser_evidence.subprocess, "run", run)
    evidence = tmp_path / "published"
    evidence.mkdir()
    with pytest.raises(RuntimeError, match="credencial en un artefacto"):
        browser_evidence.run_browser("pnpm", [], root=tmp_path, project="isolated-test",
                                     environment={"TV_CONNECTIONS_PASSWORD": secret}, evidence=evidence)
    assert not list(evidence.iterdir())
