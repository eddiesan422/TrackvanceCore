"""Authentic source versions and post-restore credentials retain strict privacy."""
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import identity_legacy_restore_cycle as runner
import pytest


def test_historical_sources_are_fixed_authentic_commits_not_current_checkout():
    from ci.local_resources import HISTORICAL_SOURCES
    assert set(runner.SOURCES) == {"0.5.1", "0.6.0", "0.6.1"}
    assert {version: HISTORICAL_SOURCES[version] for version in runner.SOURCES} == {
        version: spec[0] for version, spec in runner.SOURCES.items()}
    assert runner.SOURCES["0.6.1"] == (
        "6fac26b3648cb4a4b50c094ef12c1e103bc97ddd", "0012_delivery_target_audit", 5)
    assert runner.TARGET_VERSION == "0.8.0"
    assert runner.SOURCES["0.6.0"] == (
        "587909bc4462683e87e403dd2ea29a1d6d4afe08", "0012_delivery_target_audit", 5)
    assert runner.SOURCES["0.5.1"] == (
        "4519ed354202ea8f220682758da234e07b6df3ed", "0009_delivery_reviews", 4)


def test_shared_historical_catalog_has_five_exact_sources_without_expanding_identity_matrix():
    from ci.local_resources import HISTORICAL_SOURCES

    assert HISTORICAL_SOURCES == {
        "0.5.1": "4519ed354202ea8f220682758da234e07b6df3ed",
        "0.6.0": "587909bc4462683e87e403dd2ea29a1d6d4afe08",
        "0.6.1": "6fac26b3648cb4a4b50c094ef12c1e103bc97ddd",
        "0.7.0": "d9b6856e757a2a1fcab3913209146f3b7b79d70c",
        "0.8.0": "4eaaeb774878bca62d7d6f758157107f0557512e",
    }
    assert set(runner.SOURCES) == {"0.5.1", "0.6.0", "0.6.1"}


@pytest.mark.parametrize("failure", [None, RuntimeError("Failed build"), subprocess.TimeoutExpired("build", 1), KeyboardInterrupt()])
def test_authentic_061_cleans_registered_source_images_after_success_failure_timeout_or_cancel(tmp_path, monkeypatch, failure):
    import v070_recovery
    from ci import local_resources
    execution = "local-" + "a" * 32
    project = "trackvance-v070-test-auth061-src-" + "b" * 12
    registry = tmp_path / "images.json"
    monkeypatch.setattr(runner.os, "environ", {"TRACKVANCE_LOCAL_EXECUTION_ID": execution,
        "TRACKVANCE_LOCAL_IMAGE_REGISTRY": str(registry), "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA": "previous"})
    monkeypatch.setattr(runner.sys, "argv", ["restore", "--source-version", "0.6.1"])
    def cycle(source, evidence):
        assert source == runner.SOURCES["0.6.1"][0]
        assert os.environ["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA"] == source
        assert os.environ["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION"] == "0.6.1"
        registry.write_text(json.dumps({"builds": [{"project": project, "source_sha": source, "version": "0.6.1"}]}))
        if failure:
            raise failure
        return 0
    monkeypatch.setattr(v070_recovery, "authentic_061_cycle", cycle)
    calls = []
    monkeypatch.setattr(local_resources, "cleanup_registered_images", lambda *args, **kwargs:
        calls.append((args, kwargs)) or {"status": "PASS", "removed": [], "skipped": []})
    if failure:
        with pytest.raises(type(failure)):
            runner.main()
    else:
        assert runner.main() == 0
    assert calls[0][0] == (registry, execution) and calls[0][1]["projects"] == {project}
    assert os.environ["TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA"] == "previous"
    assert "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION" not in os.environ
    assert json.loads((tmp_path / "historical-061-image-cleanup.json").read_text())["status"] == "PASS"


def test_historical_environment_restored_even_when_image_cleanup_fails(tmp_path, monkeypatch):
    import v070_recovery
    from ci import local_resources
    registry = tmp_path / "images.json"
    registry.write_text('{"builds": []}')
    monkeypatch.setattr(runner.os, "environ", {"TRACKVANCE_LOCAL_EXECUTION_ID": "local-" + "a" * 32,
        "TRACKVANCE_LOCAL_IMAGE_REGISTRY": str(registry)})
    monkeypatch.setattr(runner.sys, "argv", ["restore", "--source-version", "0.6.1"])
    monkeypatch.setattr(v070_recovery, "authentic_061_cycle", lambda *_args: 0)
    monkeypatch.setattr(local_resources, "cleanup_registered_images", lambda *_args, **_kwargs:
        (_ for _ in ()).throw(ValueError("Cleanup unavailable")))
    with pytest.raises(ValueError, match="Cleanup unavailable"):
        runner.main()
    assert "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_SHA" not in os.environ
    assert "TRACKVANCE_LOCAL_HISTORICAL_SOURCE_VERSION" not in os.environ


@pytest.mark.parametrize("allowed", [False, True])
def test_actual_061_private_evidence_guard_accepts_runner_root_without_weakening(allowed, tmp_path, monkeypatch):
    import docker_backup_cycle
    import isolation_profile
    import v070_recovery
    monkeypatch.setattr(v070_recovery, "ROOT", tmp_path)
    monkeypatch.setattr(v070_recovery.docker_state, "ensure_fresh_project", lambda *_args: None)
    monkeypatch.setattr(v070_recovery.certification_v070, "inventory", lambda *_args: {})
    monkeypatch.setattr(v070_recovery, "target_override", lambda *_args: tmp_path / "target.json")
    monkeypatch.setattr(docker_backup_cycle, "available_port", iter([32001, 32002]).__next__)
    monkeypatch.setattr(v070_recovery.os, "environ", {"PYTHONIOENCODING": "utf-8"})
    monkeypatch.setattr(isolation_profile, "runtime_diagnostics", lambda *_args: {})
    calls = []
    def stop_before_docker(arguments, *_args, **_kwargs):
        assert arguments[:2] == ["git", "archive"]
        calls.append(arguments)
        raise RuntimeError("Stop before Docker mutation")
    monkeypatch.setattr(docker_backup_cycle, "execute", stop_before_docker)
    evidence = tmp_path / (".codex-local/v070" if allowed else ".codex-local/local-deep") / ("local-" + "a" * 32 + "-historical")
    if allowed:
        assert v070_recovery.authentic_061_cycle(runner.SOURCES["0.6.1"][0], evidence) == 1
        assert calls and json.loads((evidence / "result.json").read_text())["failed_stage"] == "authentic_archive"
    else:
        with pytest.raises(ValueError, match="directorio privado"):
            v070_recovery.authentic_061_cycle(runner.SOURCES["0.6.1"][0], evidence)
        assert not calls and not evidence.exists()


def test_relative_evidence_is_resolved_before_running_from_archived_checkout(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner.sys, "argv", ["restore", "--source-version", "0.6.0", "--evidence-dir", "private"])
    monkeypatch.setattr(runner.os, "environ", dict(os.environ))
    monkeypatch.setattr(runner, "main_inventory", lambda _: [])
    monkeypatch.setattr(runner, "assert_main_unchanged", lambda *_: None)
    monkeypatch.setattr(runner.docker_state, "ensure_fresh_project", lambda _: None)
    calls = []

    def stop_at_archive(arguments, **kwargs):
        calls.append(arguments)
        archive = Path(arguments[arguments.index("--output") + 1])
        assert archive == tmp_path / "private" / "baseline.zip" and archive.is_absolute()
        raise RuntimeError("Stop before any Docker resource is created")

    monkeypatch.setattr(runner.subprocess, "run", stop_at_archive)
    assert runner.main() == 1 and len(calls) == 1
    result = json.loads((tmp_path / "private" / "result.json").read_text())
    assert result["cleanup"] == "PASS" and result["failed_stage"] == "authentic_source_build"


@pytest.mark.parametrize("after", [1, 2])
def test_post_restore_issuance_preserves_historical_notifications_and_never_reports_plaintext(monkeypatch, after):
    secrets = ("A" * 32, "B" * 32)
    counts = iter([1, after])
    monkeypatch.setattr(runner, "notification_count", lambda *args: next(counts))
    calls = []

    def request(method, path, *args, **kwargs):
        calls.append((method, path))
        if path == "/roles":
            return {"items": [{"id": "analyst", "name": "Data Analyst"}]}
        if method == "POST":
            return {"user": {"id": "user", "version": 1}, "temporary_credentials": {
                "temporary_password": secrets[int("regenerate" in path)]}}
        return {"id": "user"}

    api = SimpleNamespace(json=request, credentials=())
    if after != 1:
        with pytest.raises(ValueError, match="notificaciones"):
            runner.certify_061_credentials(api, [], {}, ())
    else:
        report, sensitive = runner.certify_061_credentials(api, [], {}, ())
        assert sensitive == secrets == api.credentials
        assert report["notifications_before"] == report["notifications_after"] == 1
        assert not any(value in json.dumps(report) for value in secrets)
        assert ("GET", "/users/user") in calls and ("GET", "/audit-events") in calls


def test_060_notification_uses_authentic_api_and_labels_only_other_metadata_synthetic():
    calls, scripts, payloads = [], [], []

    def request(method, path, *args, **kwargs):
        calls.append((method, path))
        if path == "/roles":
            return {"items": [{"id": "role", "name": "Data Analyst"}]}
        if path == "/users":
            return {"id": "user", "credential_delivery": {"status": "FAILED"}}
        if path == '/delivery/destinations':
            payloads.append(args[0])
        return {"id": "destination"}

    runner.seed_060_history(SimpleNamespace(json=request),
        lambda *args, **kwargs: scripts.append(kwargs["input_text"]), [], "private-sql-secret")
    assert ("POST", "/users") in calls
    assert "NotificationDeliveryRecord" not in scripts[0]
    assert "ExternalIdentity" in scripts[0] and "DeliveryTargetPolicy" in scripts[0]
    assert "private-sql-secret" not in scripts[0]
    assert payloads[0]['database'] == payloads[0]['username'] == 'tv_v070_test'
    assert '"database": "tv_v070_test"' in scripts[0] and '"database": "trackvance"' not in scripts[0]


def test_restore_compares_immutable_state_before_enabling_disposable_demo_access(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(runner.sys, "argv", ["restore", "--source-version", "0.5.1", "--evidence-dir", str(evidence)])
    monkeypatch.setattr(runner.os, "environ", dict(os.environ))
    monkeypatch.setattr(runner, "main_inventory", lambda _: [])
    monkeypatch.setattr(runner, "assert_main_unchanged", lambda *_: None)
    monkeypatch.setattr(runner, "available_port", iter([3201, 3202]).__next__)
    monkeypatch.setattr(runner.docker_state, "ensure_fresh_project", lambda _: None)
    versions = iter(["0.5.1", "0.8.0"])
    monkeypatch.setattr(runner, "health_version", lambda _: next(versions))
    before = {"migration": "0009_delivery_reviews", "schema_version": 4, "tables": {}}
    after = {"migration": "0015_sentinel_execution_identity", "schema_version": 6,
             "verified_artifacts": 0, "verified_source_secrets": 0, "verified_delivery_secrets": 0,
             "tables": {"roles": {"role": "hash"}, "users": {"user": "hash"}, "notification_deliveries": {}}}
    state = {"compared": False, "demo_enabled": False}

    def backup(project, path):
        path.mkdir()
        (path / "state.json").write_text(json.dumps(before))

    monkeypatch.setattr(runner.docker_state, "backup", backup)
    monkeypatch.setattr(runner.docker_state, "verify_backup", lambda _: {"migration": before["migration"]})
    monkeypatch.setattr(runner, "scan_backup_plaintext", lambda *args: {"status": "PASS"})

    def restore(*args, **kwargs):
        assert kwargs == {"start": False, "web_port": 3202}
        return {"status": "STOPPED_VERIFIED"}

    monkeypatch.setattr(runner.docker_state, "restore", restore)
    monkeypatch.setattr(runner.docker_state, "inventory", lambda _: {"containers": [{"service": "api", "id": "api"}]})

    def snapshot(container, path, *, command=None):
        if command == "snapshot-legacy-v4":
            state["compared"] = True
            return before
        return after

    monkeypatch.setattr(runner.docker_state, "_copy_snapshot", snapshot)

    def command(arguments, **kwargs):
        if arguments[:2] == ["git", "archive"]:
            with zipfile.ZipFile(arguments[arguments.index("--output") + 1], "w") as bundle:
                bundle.writestr("compose.yml", "services: {}")
        if "up" in arguments and "--build" not in arguments:
            assert kwargs["env"]["WEB_PORT"] == "3202"
            assert kwargs["env"]["DEMO_SEED_ENABLED"] == "false"
            assert arguments[-2:] == ["api", "web"]
            assert "--env-file" in arguments
            if kwargs["env"]["DEMO_ACCESS_ENABLED"] == "true":
                assert state["compared"]
                state["demo_enabled"] = True
            else:
                assert not state["compared"]
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", command)

    def api(*args):
        assert state["compared"] and state["demo_enabled"]
        return object()

    monkeypatch.setattr(runner, "RecoveryApi", api)
    monkeypatch.setattr(runner, "certify_061_credentials", lambda *args: ({"status": "PASS"}, args[-1]))
    assert runner.main() == 0
    result = json.loads((evidence / "result.json").read_text())
    assert result["exact_historical_state"] == result["current_credentials"]["status"] == "PASS"
    assert result["automatic_processes_started"] is False
