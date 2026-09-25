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
