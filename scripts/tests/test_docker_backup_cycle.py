import hashlib
import importlib.util
import io
import json
import subprocess
import urllib.error
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("docker_backup_cycle.py")
spec = importlib.util.spec_from_file_location("docker_backup_cycle", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize(
    "project",
    ["trackvance-core", "trackvance-recovery", "trackvance-recovery-src", "other"],
)
def test_recovery_runner_rejects_non_isolated_projects(project):
    with pytest.raises(ValueError, match="aislado"):
        runner.validated_project_name(project)


def test_cleanup_uses_explicit_plan_and_literal_confirmation(monkeypatch, tmp_path):
    project = "trackvance-recovery-src-unit"
    state = {
        "containers": [{"id": "one"}],
        "volumes": [{"name": "two"}],
        "networks": [{"id": "three"}],
    }
    monkeypatch.setattr(runner.docker_state, "inventory", lambda value: state)
    plan = {"plan_sha256": "abcdef0123456789"}
    observed = {}
    monkeypatch.setattr(
        runner.docker_state,
        "create_reset_plan",
        lambda value, path, ttl_minutes: plan,
    )
    monkeypatch.setattr(
        runner.docker_state,
        "reset",
        lambda path, confirmation: observed.update({"confirmation": confirmation}),
    )
    runner.cleanup(project, tmp_path)
    assert observed["confirmation"] == f"RESET:{project}:abcdef012345"


def test_database_fixture_requires_its_own_prefix():
    assert runner.validated_project_name("trackvance-recovery-db-unit", database=True)
    with pytest.raises(ValueError):
        runner.validated_project_name("trackvance-recovery-src-unit", database=True)
    with pytest.raises(ValueError):
        runner.validated_project_name("trackvance-recovery-db-unit")


def test_fixture_never_persists_credentials_or_publishes_database_port():
    fixture = runner.fixture_compose()
    database = fixture["services"][runner.DATABASE_SERVICE]
    assert database["environment"]["POSTGRES_PASSWORD"].startswith("${RECOVERY_SOURCE_PASSWORD:")
    assert "ports" not in database
    assert "external" not in fixture["volumes"]["source_data"]
    assert database["restart"] == "no"


def test_source_bootstrap_password_only_in_stdin_and_reader_is_select_only(monkeypatch):
    observed = {}
    password = "b" * 48
    monkeypatch.setattr(runner, "execute", lambda args, env, **kwargs: observed.update(
        {"args": args, "env": env, **kwargs}))
    runner.initialize_source(["docker", "compose"], {}, (password,), password)
    assert password not in " ".join(observed["args"])
    assert password in observed["input_text"]
    assert "GRANT SELECT ON ALL TABLES" in observed["input_text"]
    assert "CREATE ROLE tv_recovery_writer" in observed["input_text"]
    assert "CREATE SCHEMA recovery_delivery" in observed["input_text"]
    assert "GRANT ALL" not in observed["input_text"]
    assert "CREATE VIEW" in observed["input_text"]
    with pytest.raises(RuntimeError):
        runner.initialize_source([], {}, (), "unsafe'password")


@pytest.mark.parametrize("secret_location", ["stdout", "stderr", "argv"])
def test_execute_detects_credentials_without_rendering_them(monkeypatch, capsys, secret_location):
    password = "private-disposable-password"
    result = subprocess.CompletedProcess(["docker"], 0,
        stdout=password if secret_location == "stdout" else "ok",
        stderr=password if secret_location == "stderr" else "")
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: result)
    args = ["docker", password] if secret_location == "argv" else ["docker", "logs"]
    with pytest.raises(RuntimeError) as caught:
        runner.execute(args, {}, credentials=(password,))
    assert password not in str(caught.value)
    assert password not in capsys.readouterr().out


def test_execute_transports_migration_and_review_unicode_as_utf8(monkeypatch):
    observed = {}

    def run(arguments, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, stdout="ok", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run)
    source = "0008→0009; revisión de confirmación"
    assert runner.execute(["docker", "exec", "fixture", "python", "-"], {}, input_text=source) == "ok"
    assert observed["encoding"] == "utf-8" and observed["input"] == source


def test_http_error_never_exposes_response_body():
    api = object.__new__(runner.RecoveryApi)
    api.base_url, api.credentials, api.csrf = "http://localhost/api/v1", (), ""
    error = urllib.error.HTTPError(api.base_url, 500, "Internal", {}, io.BytesIO(b"private-driver-error"))

    def fail(*args, **kwargs):
        raise error

    api.opener = SimpleNamespace(open=fail)
    with pytest.raises(RuntimeError) as caught:
        api.request("GET", "/connections")
    assert "HTTP 500" in str(caught.value)
    assert "private-driver-error" not in str(caught.value)


def test_run_precondition_failure_is_terminal_without_polling_again():
    requests = []

    def response(method, path, *args, **kwargs):
        requests.append((method, path))
        return {"run_id": "blocked"} if method == "POST" else {"status": "FAILED_PRECONDITION"}

    with pytest.raises(RuntimeError, match="error técnico"):
        runner.wait_run(SimpleNamespace(json=response), "contract", "version")
    assert requests == [("POST", "/intake/runs"), ("GET", "/runs/blocked")]


def test_artifact_download_uses_manifest_artifact_identity_and_checks_hash():
    content = b"canonical-parquet-fixture"
    sha256 = hashlib.sha256(content).hexdigest()
    version = {"canonical_artifact_id": "canonical", "artifacts": [
        {"artifact_id": "canonical", "sha256": sha256, "kind": "CANONICAL_PARQUET"}]}
    calls = []

    def download(method, path):
        calls.append((method, path))
        return content

    assert runner.artifact_hash(SimpleNamespace(request=download), version) == sha256
    assert calls == [("GET", "/artifacts/canonical/download")]
    with pytest.raises(RuntimeError, match="hash"):
        runner.artifact_hash(SimpleNamespace(request=lambda *args: b"tampered"), version)


def test_attachment_upload_has_version_and_binary_body_in_multipart():
    api = object.__new__(runner.RecoveryApi)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return b'{"id":"case","version":5}'

    api.request = request
    assert api.upload_attachment({"id": "case", "version": 4}, b"proof\x00\xff") == {"id": "case", "version": 5}
    method, path, sent = calls[0]
    assert (method, path) == ("POST", "/exceptions/case/attachments")
    assert sent["expected"] == 201
    assert sent["content_type"].startswith("multipart/form-data; boundary=")
    assert b'name="version"\r\n\r\n4\r\n' in sent["raw"]
    assert b'filename="recovery-evidence.txt"' in sent["raw"]
    assert b"proof\x00\xff" in sent["raw"]


def test_attachment_download_verifies_artifact_identity_size_and_bytes():
    content = b"stored attachment"
    case = {"id": "case", "attachments": [{"id": "attachment", "artifact_id": "artifact",
        "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}]}
    paths = []

    def request(method, path):
        paths.append(path)
        return content

    assert runner.attachment_hash(SimpleNamespace(request=request), case) == case["attachments"][0]["sha256"]
    assert paths == ["/exceptions/case/attachments/attachment/download"]
    case["attachments"][0]["size_bytes"] += 1
    with pytest.raises(RuntimeError, match="hash y tamaño"):
        runner.attachment_hash(SimpleNamespace(request=request), case)


def test_scheduler_fixture_pauses_dispatch_before_waiting_for_completion(monkeypatch):
    calls = []
    run = {"id": "scheduled", "dataset_version_id": "snapshot", "decision": "ALERT",
           "initiated_by": {"type": "SYSTEM"}}

    def request(method, path, payload=None, **kwargs):
        calls.append((method, path, payload))
        if path == "/monitors":
            return {"id": "monitor"}
        if method == "POST" and path.endswith("/schedule"):
            return {"id": "schedule", "enabled": payload["enabled"], "version": 1 if payload["enabled"] else 2}
        if path.endswith("/occurrences"):
            return {"items": [{"id": "occurrence", "run_id": "scheduled"}], "total": 1}
        if path.endswith("/series"):
            return {"items": [], "sample_count": 1}
        if path.endswith("/alerts"):
            return {"items": [{"id": "alert"}]}
        raise AssertionError(path)

    def completed(api, run_id):
        assert run_id == "scheduled"
        assert any(method == "POST" and path.endswith("/schedule") and payload["enabled"] is False
                   for method, path, payload in calls)
        return run

    monkeypatch.setattr(runner, "wait_existing_run", completed)
    prepared = runner.prepare_scheduled_monitor(SimpleNamespace(json=request, request=lambda *args: b"{}"), "dataset", "snapshot")
    assert prepared["schedule"]["version"] == 2
    assert prepared["schedule"]["enabled"] is False
    assert prepared["run"] == run


@pytest.mark.parametrize("changed", ["schedule", "occurrences", "series", "alerts"])
def test_monitor_recovery_rejects_mutated_paused_state_or_history(changed):
    original = {
        "monitor_id": "monitor", "schedule": {"id": "schedule", "enabled": False, "version": 2},
        "occurrences": {"items": [{"id": "occurrence"}]}, "series": {"sample_count": 1},
        "alerts": {"items": [{"id": "alert"}]}, "run": {"id": "run", "dataset_version_id": "snapshot"},
        "evidence_sha256": hashlib.sha256(b"{}").hexdigest(),
    }

    def response(method, path):
        key = path.rsplit("/", 1)[-1]
        if path == "/runs/run":
            return original["run"]
        return {"tampered": True} if key == changed else original[key]

    with pytest.raises(RuntimeError, match="estado restaurado"):
        runner.validate_monitor_restoration(SimpleNamespace(json=response), original)


@pytest.mark.parametrize("auto_closed", [False, True])
def test_corrected_case_requires_technical_validation_but_cannot_close_automatically(auto_closed):
    content = b"restored proof"
    historical = {
        "id": "case", "origin_run_id": "origin", "configuration_id": "config", "finding_id": "finding",
        "assigned_user_id": "actor", "priority": "CRITICAL", "sla_hours": 24, "due_at": "future",
        "attachments": [{"id": "attachment", "artifact_id": "artifact", "sha256": hashlib.sha256(content).hexdigest(),
                         "size_bytes": len(content)}], "events": [{"event_type": "COMMENT_ADDED"}],
    }
    current = {**deepcopy(historical), "state": "RESOLVED" if auto_closed else "PENDING_VALIDATION",
               "auto_resolve_enabled": False, "validation_run_id": "corrected",
               "technical_validation": {"validated": True},
               "events": [*historical["events"], {"event_type": "TECHNICAL_VALIDATION"}]}
    api = SimpleNamespace(json=lambda *args: current, request=lambda *args: content)
    if auto_closed:
        with pytest.raises(RuntimeError, match="no debe cerrar"):
            runner.validate_case_after_correction(api, historical, "corrected")
    else:
        validated = runner.validate_case_after_correction(api, historical, "corrected")
        assert validated["state"] == "PENDING_VALIDATION"
        assert validated["technical_validation"] == "VALIDATED"
        assert validated["historical_timeline_preserved"] is True


def test_destroy_verifies_source_absent_and_external_database_alive(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "cleanup", lambda project, path: calls.append(("destroy", project)))
    monkeypatch.setattr(runner, "assert_fresh", lambda project: calls.append(("empty", project)))
    monkeypatch.setattr(runner.docker_state, "inventory", lambda project: {
        "containers": [{"running": True}]})
    runner.destroy_before_restore("trackvance-recovery-src-unit", "trackvance-recovery-db-unit", tmp_path)
    assert calls == [("destroy", "trackvance-recovery-src-unit"),
                     ("empty", "trackvance-recovery-src-unit")]
    monkeypatch.setattr(runner.docker_state, "inventory", lambda project: {"containers": []})
    with pytest.raises(RuntimeError, match="sobrevivir"):
        runner.destroy_before_restore("trackvance-recovery-src-unit", "trackvance-recovery-db-unit", tmp_path)


@pytest.mark.parametrize("changed", [
    None, "committed_run", "unknown_run", "committed_attempts", "unknown_attempts",
    "reviews", "receipt", "manifest", "repair", "remote_rows",
])
def test_operational_recovery_preserves_unknown_reviews_hashes_and_no_remote_replay(monkeypatch, changed):
    receipt, manifest = b"committed receipt", b"committed manifest"
    saved = {
        "committed_run": {"id": "committed", "status": "SUCCESS", "decision": "COMMITTED"},
        "unknown_run": {"id": "unknown", "status": "UNKNOWN", "decision": "UNKNOWN"},
        "committed_attempts": {"items": [{"id": "attempt-c", "status": "COMMITTED"}], "total": 1},
        "unknown_attempts": {"items": [{"id": "attempt-u", "status": "UNKNOWN"}], "total": 1},
        "reviews": {"items": [{"id": "review", "outcome": "INCONCLUSIVE"}], "total": 1},
        "review": {"id": "review"}, "unknown_fixture": "SIMULATED_UNKNOWN_NO_REMOTE_IO",
        "repair": {"run_id": "committed", "status": "REPAIRED",
                   "receipt_artifact_id": "receipt", "manifest_artifact_id": "manifest"},
        "concurrent_repair_statuses": ["ALREADY_VALID", "REPAIRED"],
        "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(), "remote_rows": 4,
    }
    expected = {
        "/runs/committed": "committed_run", "/runs/unknown": "unknown_run",
        "/delivery/runs/committed/attempts": "committed_attempts",
        "/delivery/runs/unknown/attempts": "unknown_attempts",
        "/delivery/runs/unknown/reviews": "reviews",
    }
    calls = []

    def request_json(method, path, payload=None):
        calls.append((method, path))
        if method == "POST":
            assert path == "/delivery/runs/committed/repair-evidence"
            return {**saved["repair"], "status": "REPAIRED" if changed == "repair" else "ALREADY_VALID"}
        key = expected[path]
        return {"changed": True} if changed == key else deepcopy(saved[key])

    def request_bytes(method, path):
        assert method == "GET"
        key = "receipt" if path.endswith("/receipt") else "manifest"
        return b"changed" if changed == key else receipt if key == "receipt" else manifest

    monkeypatch.setattr(runner, "operational_remote_count",
                        lambda *args: 8 if changed == "remote_rows" else 4)
    api = SimpleNamespace(json=request_json, request=request_bytes)
    if changed is not None:
        with pytest.raises(RuntimeError):
            runner.validate_operational_delivery_restoration(api, saved, [], {}, ())
    else:
        report = runner.validate_operational_delivery_restoration(api, saved, [], {}, ())
        assert report["historical_unknown_unchanged"]
        assert report["review_exactly_preserved"]
        assert report["repaired_evidence_exactly_preserved"]
        assert report["remote_replay"] is False
        assert report["remote_rows_before_and_after"] == 4
        assert report["unknown_fixture"] == "SIMULATED_UNKNOWN_NO_REMOTE_IO"
        assert [path for method, path in calls if method == "POST"] == [
            "/delivery/runs/committed/repair-evidence"]


def test_delivery_operational_target_is_separate_from_post_restore_credential_test():
    original = {"version": {"id": "snapshot"}, "destination": {
        "id": "destination", "destination_version_id": "revision"}}
    operational = runner.delivery_draft(original, "operational_records")
    restored = runner.delivery_draft(original, "records")
    assert operational["target"]["table_name"] == "operational_records"
    assert restored["target"]["table_name"] == "records"
    assert operational["write_strategy"] == restored["write_strategy"] == "APPEND"
    assert operational["dataset_version_id"] == restored["dataset_version_id"] == "snapshot"
    assert operational["columns"] == restored["columns"]


def setup_main(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    source, target, database = (f"trackvance-recovery-{part}-unit" for part in ("src", "dst", "db"))
    monkeypatch.setattr(runner.sys, "argv", [str(SCRIPT), "--source-project", source,
        "--target-project", target, "--database-project", database,
        "--source-port", "3101", "--target-port", "3102", "--skip-build",
        "--evidence-dir", str(evidence)])
    return evidence, source, target, database


@pytest.mark.parametrize("existing_role", ["src", "dst", "db"])
def test_existing_project_is_never_cleaned_when_guard_fails(monkeypatch, tmp_path, existing_role):
    evidence, _, _, _ = setup_main(monkeypatch, tmp_path)
    cleaned, executed = [], []

    def existing(project):
        if project == f"trackvance-recovery-{existing_role}-unit":
            raise RuntimeError("Existing project")

    monkeypatch.setattr(runner, "assert_fresh", existing)
    monkeypatch.setattr(runner, "cleanup", lambda *args: cleaned.append(args))
    monkeypatch.setattr(runner, "execute", lambda *args, **kwargs: executed.append(args))
    assert runner.main() == 1
    assert cleaned == executed == []
    assert json.loads((evidence / "result.json").read_text())["failed_stage"] == "freshness_guards"


@pytest.mark.parametrize("keep", [False, True])
def test_orchestration_destroys_source_before_restore_and_reports_html_only(monkeypatch, tmp_path, keep):
    evidence, source, target, database = setup_main(monkeypatch, tmp_path)
    if keep:
        runner.sys.argv.append("--keep")
    calls = []
    monkeypatch.setattr(runner, "assert_fresh", lambda project: calls.append(("fresh", project)))
    monkeypatch.setattr(runner, "cleanup", lambda project, path: calls.append(("cleanup", project)))
    monkeypatch.setattr(runner, "initialize_source", lambda *args: None)
    monkeypatch.setattr(runner, "attach_external_network", lambda *args: None)
    monkeypatch.setattr(runner, "RecoveryApi", lambda *args: object())
    monkeypatch.setattr(runner, "capture_original", lambda *args: {"real_connection": True})
    monkeypatch.setattr(runner, "prepare_delivery_operations", lambda *args: None)
    monkeypatch.setattr(runner, "destroy_before_restore", lambda *args: calls.append(("destroy", source)))
    monkeypatch.setattr(runner, "validate_restored", lambda *args: {"restored_credential_used": True})
    monkeypatch.setattr(runner.docker_state, "digest", lambda path: "a" * 64)

    def execute(args, env, **kwargs):
        if args[-3:] == ["api", "python", "-"]:
            assert "seed_delivery_baseline" in kwargs["input_text"]
            return json.dumps({"status": "PASS"})
        if "backup" in args:
            (evidence / "backup").mkdir()
            (evidence / "backup" / "state.json").write_text(json.dumps({
                "verified_secrets": 2, "verified_source_secrets": 1,
                "verified_delivery_secrets": 1,
                "verified_artifacts": 4, "validated_relationships": 8,
                "tables": {name: {"one": "hash", **({"two": "hash"} if name == "monitor_schedule_versions" else {})}
                           for name in ("exceptions", "exception_attachments", "monitor_schedules",
                                        "monitor_schedule_versions", "monitor_occurrences", "metric_history",
                                        "delivery_attempts", "delivery_reviews")}}))
            calls.append(("backup", source))
        if "restore" in args:
            assert ("destroy", source) in calls
            calls.append(("restore", target))
        return ""

    monkeypatch.setattr(runner, "execute", execute)

    class HtmlResponse:
        status = 200

        def read(self, limit):
            return b"<!doctype html>"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda *args, **kwargs: HtmlResponse())
    assert runner.main() == 0
    result = json.loads((evidence / "result.json").read_text())
    assert result["source_destroyed_before_restore"] is True
    assert result["recovery"]["restored_credential_used"] is True
    assert result["ui_validation"] == {"html_http_status": 200, "playwright": "NOT_RUN_IN_THIS_DRILL"}
    assert calls.index(("backup", source)) < calls.index(("destroy", source)) < calls.index(("restore", target))
    if keep:
        assert result["retained_projects"] == [database, target]
        assert not any(event == "cleanup" for event, _ in calls)
    else:
        assert result["retained_projects"] == []
        assert [project for event, project in calls if event == "cleanup"] == [target, source, database]
