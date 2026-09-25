"""Legacy certification must never claim an operational project or its data."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("legacy_restore_cycle", Path(__file__).with_name("legacy_restore_cycle.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-core", "trackvance-recovery-src-real", "other"])
def test_legacy_runner_rejects_unrelated_project(project):
    with pytest.raises(ValueError, match="aislado"):
        runner.isolated_project(project)


def test_baseline_images_only_inspects_existing_installation(monkeypatch):
    state = {"containers": [{"service": name, "image_id": "sha256:" + "a" * 64}
                            for name in runner.IMAGE_SERVICES]}
    calls = []
    monkeypatch.setattr(runner.docker_state, "inventory", lambda project: calls.append(project) or state)
    observed, images = runner.baseline_images("trackvance-certification")
    assert observed == state and set(images) == runner.IMAGE_SERVICES
    assert calls == ["trackvance-certification"]


def test_image_overrides_contain_only_immutable_ids_and_no_credentials_or_builds():
    images = {name: "sha256:" + "b" * 64 for name in runner.IMAGE_SERVICES}
    frozen = runner.image_overrides(images)
    assert "build" not in json.dumps(frozen)
    assert "PASSWORD" not in json.dumps(frozen)
    assert all(service["pull_policy"] == "never" for service in frozen["services"].values())


def test_delivery_configuration_uses_supported_list_endpoint():
    class Api:
        def json(self, method, path):
            assert (method, path) == ("GET", "/delivery/configurations")
            return {"items": [{"id": "other"}, {"id": "wanted", "version": 1}]}

    assert runner.delivery_configuration(Api(), "wanted") == {"id": "wanted", "version": 1}


def test_legacy_delivery_waits_for_publication_before_downloads(monkeypatch):
    run = {"id": "run", "status": "SUCCESS", "decision": "COMMITTED"}
    events = []

    class Api:
        def json(self, method, path, *_args, **_kwargs):
            return {
                ("POST", "/delivery/configurations"): {"id": "configuration"},
                ("POST", "/delivery/runs"): {"id": "run"},
                ("GET", "/runs/run"): run,
                ("GET", "/datasets/dataset"): {"versions": [{"id": "version"}]},
                ("GET", "/delivery/configurations"): {"items": [{"id": "configuration"}]},
                ("GET", "/delivery/runs/run/attempts"): {"items": []},
            }[method, path]

        def request(self, _method, _path):
            assert events == ["published"]
            return b"{}"

    monkeypatch.setattr(runner.recovery, "delivery_draft", lambda *_args: {})
    monkeypatch.setattr(runner.recovery, "wait_existing_run", lambda *_args: run)
    monkeypatch.setattr(runner.recovery, "wait_for_delivery_evidence",
                        lambda *_args: events.append("published") or run)
    monkeypatch.setattr(runner.recovery, "artifact_hash", lambda *_args: "canonical")
    captured = runner.capture_delivery(Api(), {"dataset_id": "dataset", "version": {"id": "version"}})
    assert captured["run"] == run
    assert captured["canonical_sha256"] == "canonical"


def test_legacy_runner_never_cleans_resources_after_failed_freshness(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.sys, "argv", ["legacy_restore_cycle.py", "--legacy041-backup",
        str(tmp_path / "unread-backup"), "--evidence-dir", str(tmp_path / "evidence")])
    state = {"containers": [], "volumes": [], "networks": []}
    monkeypatch.setattr(runner, "baseline_images", lambda _project: (state, {}))
    monkeypatch.setattr(runner.docker_state, "inventory", lambda _project: state)
    monkeypatch.setattr(runner.recovery, "assert_fresh", lambda _project: (_ for _ in ()).throw(ValueError("existing")))
    mutations = []
    monkeypatch.setattr(runner.recovery, "cleanup", lambda *args: mutations.append(args))
    assert runner.main() == 1
    assert mutations == []
    report = json.loads((tmp_path / "evidence/result.json").read_text(encoding="utf-8"))
    assert report["failed_stage"] == "validate_inputs"
    assert report["main_inventory_unchanged"] is True


def test_restore_comparison_captured_before_smoke_mutation(monkeypatch):
    expected = {"schema_version": 3, "migration": "0008_data_delivery",
                "tables": {"runs": {"run": "digest"}}}
    restored = {"schema_version": 4, "migration": "0009_delivery_reviews",
                "tables": {"runs": {"run": "digest"}, "delivery_reviews": {}},
                "verified_artifacts": 2, "verified_source_secrets": 0, "verified_delivery_secrets": 1}
    monkeypatch.setattr(runner.docker_state, "validate_restored_state", lambda *args: None)

    def restore(*_args, **_kwargs):
        runner.docker_state.validate_restored_state({"schema_version": 2}, expected, restored, expected)
        restored["tables"]["runs"]["new-smoke-run"] = "new-digest"
        return {"status": "RUNNING_VERIFIED"}

    monkeypatch.setattr(runner.docker_state, "restore", restore)
    report = runner.restore_and_capture(Path("backup"), "trackvance-recovery-dst-legacy-test", 3333)
    assert report["historical_table_counts"] == {"runs": 1}
    assert report["normalized_state_sha256"] == report["source_state_sha256"]
    assert report["exact_historical_state"] == "PASS"


def test_final_inspection_failure_still_persists_result(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.sys, "argv", ["legacy_restore_cycle.py", "--legacy041-backup",
        str(tmp_path / "unread-backup"), "--evidence-dir", str(tmp_path / "evidence")])
    monkeypatch.setattr(runner, "baseline_images", lambda _project: ({"containers": []}, {}))
    monkeypatch.setattr(runner.recovery, "assert_fresh", lambda _project: (_ for _ in ()).throw(ValueError("existing")))
    monkeypatch.setattr(runner.docker_state, "inventory", lambda _project: (_ for _ in ()).throw(ValueError("unavailable")))
    assert runner.main() == 1
    report = json.loads((tmp_path / "evidence/result.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert report["main_inventory_unchanged"] is False
    assert report["main_inspection_error"] == "ValueError"


def test_runtime_failure_context_redacts_generated_credentials(monkeypatch, tmp_path):
    secret = "private-generated-credential"
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _length: secret)
    monkeypatch.setattr(runner.sys, "argv", ["legacy_restore_cycle.py", "--legacy041-backup",
        str(tmp_path / "unread-backup"), "--evidence-dir", str(tmp_path / "evidence")])
    state = {"containers": []}
    monkeypatch.setattr(runner, "baseline_images", lambda _project: (state, {}))
    monkeypatch.setattr(runner.docker_state, "inventory", lambda _project: state)
    monkeypatch.setattr(runner.recovery, "assert_fresh",
                        lambda _project: (_ for _ in ()).throw(RuntimeError(f"failure {secret}")))
    assert runner.main() == 1
    report = (tmp_path / "evidence/result.json").read_text(encoding="utf-8")
    assert secret not in report
    assert json.loads(report)["failure_context"] == "failure [REDACTED]"


@pytest.mark.parametrize("mismatch", [None, "state", "archives"])
def test_current_tool_backup_matches_native_baseline(monkeypatch, tmp_path, mismatch):
    backups = [tmp_path / "archived", tmp_path / "current"]
    for index, path in enumerate(backups):
        path.mkdir()
        state = {"schema_version": 3, "migration": "0008_data_delivery",
                 "tables": {"runs": {"run": "changed" if mismatch == "state" and index else "same"}}}
        (path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def verify(path):
        fingerprint = "different" if mismatch == "archives" and path == backups[1] else "same"
        return {"components": {"volumes/artifacts.tar.gz": {"entries": {"artifact": fingerprint}}}}

    monkeypatch.setattr(runner.docker_state, "verify_backup", verify)
    if mismatch:
        with pytest.raises(RuntimeError, match="discrepan"):
            runner.compare_native_backups(*backups)
    else:
        report = runner.compare_native_backups(*backups)
        assert report["status"] == "PASS"
        assert report["native_state_schema"] == 3
        assert report["exact_archived_tool_state"] is True
