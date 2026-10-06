"""Browser failures must never publish request bodies or disposable passwords."""
import json
import subprocess
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


@pytest.mark.parametrize("outcome", ["PASS", "TIMEOUT", "INTERRUPTED", "WRAPPER_TERM"])
def test_bounded_browser_reaps_only_its_own_session_on_success_timeout_and_interrupt(monkeypatch, tmp_path, outcome):
    events, handlers = [], {}
    class Child:
        pid = 23456
        returncode = None
        def communicate(self, *, timeout):
            events.append(("communicate", timeout))
            if timeout == 60:
                if outcome == "TIMEOUT":
                    raise subprocess.TimeoutExpired("browser", 60, output="synthetic-private-body")
                if outcome == "INTERRUPTED":
                    raise KeyboardInterrupt()
                if outcome == "WRAPPER_TERM":
                    handlers[browser_evidence.signal.SIGTERM]()
                self.returncode = 0
            else:
                self.returncode = -9 if self.returncode is None else self.returncode
            return ("{}", "")
        def poll(self):
            return self.returncode
        def kill(self):
            events.append(("kill", self.pid))
            self.returncode = -9
    def popen(_args, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        events.append(("spawn", Child.pid))
        return Child()
    def handler(which, callback):
        previous = handlers.get(which)
        handlers[which] = callback
        return previous
    monkeypatch.setattr(browser_evidence, "os", SimpleNamespace(name="posix",
        killpg=lambda pid, sig: events.append(("killpg", pid, sig))))
    monkeypatch.setattr(browser_evidence.subprocess, "Popen", popen)
    monkeypatch.setattr(browser_evidence.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(browser_evidence.signal, "signal", handler)
    errors = {"TIMEOUT": subprocess.TimeoutExpired, "INTERRUPTED": KeyboardInterrupt, "WRAPPER_TERM": InterruptedError}
    if outcome == "PASS":
        assert browser_evidence.bounded_browser_command(["pnpm"], cwd=tmp_path, environment={}, timeout_seconds=60).returncode == 0
    else:
        with pytest.raises(errors[outcome]):
            browser_evidence.bounded_browser_command(["pnpm"], cwd=tmp_path, environment={}, timeout_seconds=60)
    assert ("killpg", Child.pid, browser_evidence.signal.SIGKILL) in events
    assert events[-1] == ("communicate", 15)
    assert handlers[browser_evidence.signal.SIGTERM] is None


def test_browser_deadline_failure_summary_omits_child_payload(monkeypatch, tmp_path):
    secret = "synthetic-password-rows-cookie"
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("browser", 60, output=secret, stderr=secret)
    monkeypatch.setattr(browser_evidence, "bounded_browser_command", timeout)
    evidence = tmp_path / "published"
    evidence.mkdir()
    with pytest.raises(subprocess.TimeoutExpired):
        browser_evidence.run_browser("pnpm", [], root=tmp_path, project="only-own-uuid",
            environment={}, evidence=evidence, timeout_seconds=60)
    content = (evidence / "browser-summary.json").read_text()
    assert secret not in content
    assert json.loads(content) == {"status": "FAIL", "error_type": "TimeoutExpired",
        "raw_artifacts_published": False, "deadline_seconds": 60}


@pytest.fixture
def real_spec_names(tmp_path):
    spec_dir = tmp_path / "frontend/tests-e2e"
    spec_dir.mkdir(parents=True)
    names = sorted(browser_evidence.CI_OPT_IN_SPECS)
    names += ["tests-e2e/catalog-reports.spec.ts", "tests-e2e/core.spec.ts", "tests-e2e/new/deep-feature.spec.ts"]
    for name in names:
        path = tmp_path / "frontend" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// filename-only pure infrastructure fixture\n")
    return tmp_path, sorted(names)


def test_ci_selection_includes_every_core_and_new_spec_and_explains_all_eight_opt_ins(real_spec_names):
    root, names = real_spec_names
    environment = {"TRACKVANCE_CI_IMAGE_MANIFEST": "private/images.json"}
    selected, metadata = browser_evidence.ci_spec_selection([], root, environment)
    assert selected == [name for name in names if name not in browser_evidence.CI_OPT_IN_SPECS]
    assert metadata["selected_spec_files"] == selected
    assert metadata["excluded_opt_in_specs"] == [
        {"file": name, "required_flag": flag, "covered_by_group": group}
        for name, (flag, group) in sorted(browser_evidence.CI_OPT_IN_SPECS.items())]
    assert len(metadata["excluded_opt_in_specs"]) == 8


@pytest.mark.parametrize("flag", sorted({flag for flag, _ in browser_evidence.CI_OPT_IN_SPECS.values()}))
def test_active_opt_in_flag_selects_all_its_specs_and_does_not_exclude_them(real_spec_names, flag):
    root, _ = real_spec_names
    selected, metadata = browser_evidence.ci_spec_selection([], root,
        {"TRACKVANCE_CI_IMAGE_MANIFEST": "private/images.json", flag: "true"})
    activated = {name for name, value in browser_evidence.CI_OPT_IN_SPECS.items() if value[0] == flag}
    assert activated <= set(selected)
    assert not activated & {entry["file"] for entry in metadata["excluded_opt_in_specs"]}


@pytest.mark.parametrize("value", ["false", "True", "TRUE", "1", "true ", ""])
def test_opt_in_values_follow_existing_spec_exact_true_condition(real_spec_names, value):
    root, _ = real_spec_names
    selected, _ = browser_evidence.ci_spec_selection([], root,
        {"TRACKVANCE_CI_IMAGE_MANIFEST": "private/images.json", "TV_VOLUME_E2E": value})
    assert "tests-e2e/volume.spec.ts" not in selected


@pytest.mark.parametrize("arguments,environment", [
    ([], {}), ([], {"TRACKVANCE_CI_IMAGE_MANIFEST": ""}),
    (["tests-e2e/volume.spec.ts"], {"TRACKVANCE_CI_IMAGE_MANIFEST": "private/images.json"}),
    (["--grep", "local test"], {"TRACKVANCE_CI_IMAGE_MANIFEST": "private/images.json"}),
])
def test_explicit_arguments_and_non_ci_discovery_remain_unchanged(tmp_path, arguments, environment):
    selected, metadata = browser_evidence.ci_spec_selection(arguments, tmp_path, environment)
    assert selected is arguments
    assert metadata is None


def test_empty_ci_selection_cannot_fall_back_to_unrestricted_discovery(tmp_path):
    with pytest.raises(RuntimeError, match="CI_BROWSER_SELECTION_EMPTY"):
        browser_evidence.ci_spec_selection([], tmp_path, {"TRACKVANCE_CI_IMAGE_MANIFEST": "private/images.json"})


@pytest.mark.parametrize("skipped", [0, 1])
def test_ci_browser_publishes_exact_selection_and_keeps_unexpected_skip_failure(monkeypatch, real_spec_names, skipped):
    root, names = real_spec_names
    commands = []
    def run(arguments, **_kwargs):
        commands.append(arguments)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"stats": {
            "expected": 3, "unexpected": 0, "flaky": 0, "skipped": skipped}}), stderr="")
    monkeypatch.setattr(browser_evidence.subprocess, "run", run)
    evidence = root / "published"
    evidence.mkdir()
    kwargs = {"root": root, "project": "own-browser",
              "environment": {"TRACKVANCE_CI_IMAGE_MANIFEST": "private/images.json"}, "evidence": evidence}
    if skipped:
        with pytest.raises(RuntimeError, match="CI_BROWSER_UNEXPECTED_SKIP"):
            browser_evidence.run_browser("pnpm", [], **kwargs)
    else:
        browser_evidence.run_browser("pnpm", [], **kwargs)
    summary = json.loads((evidence / "browser-summary.json").read_text())
    expected = [name for name in names if name not in browser_evidence.CI_OPT_IN_SPECS]
    assert commands[0][4:commands[0].index("--reporter=json")] == expected
    assert summary["selected_spec_files"] == expected
    assert summary["skipped"] == skipped
    assert summary["status"] == ("FAIL" if skipped else "PASS")
    assert len(summary["excluded_opt_in_specs"]) == 8
