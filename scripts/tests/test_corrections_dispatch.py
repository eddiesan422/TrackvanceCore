"""Safety and evidence contracts; real PG/load is certified by the container helper."""

import json
import sys
from argparse import Namespace
from types import SimpleNamespace
from uuid import uuid4

import corrections_dispatch as runner
import pytest


def arguments(tmp_path):
    return Namespace(api_url="http://api:8000", source_version=str(uuid4()), additional_source_version=str(uuid4()),
        configuration=str(uuid4()), busy_run=None, evidence=tmp_path, scheduler_paused=True,
        count=4, expected_rows=1000000, timeout=1800)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", "trackvance-v070-test-corrections-0123456789ab")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://tv_v070_test:private@postgres:5432/tv_v070_test")


def test_dispatch_guard_accepts_only_explicit_pg_uuid_and_scheduler_ack(tmp_path, isolated):
    project, database = runner.validate_arguments(arguments(tmp_path))
    assert project.endswith("0123456789ab")
    assert database.database == database.username == "tv_v070_test"


@pytest.mark.parametrize("project", ["trackvance-core", "trackvance-v070-test-core", "trackvance-v070-test-core-nothex"])
def test_dispatch_guard_rejects_other_projects(tmp_path, isolated, monkeypatch, project):
    monkeypatch.setenv("TRACKVANCE_CERTIFICATION_PROJECT", project)
    with pytest.raises(RuntimeError, match="exclusivo"):
        runner.validate_arguments(arguments(tmp_path))


@pytest.mark.parametrize("database", [
    "sqlite:///test.db", "postgresql://trackvance:private@postgres/trackvance",
    "postgresql://trackvance:private@postgres/tv_v070_test",
])
def test_dispatch_guard_rejects_ambient_database(tmp_path, isolated, monkeypatch, database):
    monkeypatch.setenv("DATABASE_URL", database)
    with pytest.raises(RuntimeError, match="aislada"):
        runner.validate_arguments(arguments(tmp_path))


@pytest.mark.parametrize(("field", "value"), [
    ("api_url", "http://localhost:3100"), ("api_url", "http://private@api:8000"),
    ("api_url", "http://api:8000?secret=private"), ("api_url", "http://external:32070"),
    ("scheduler_paused", False), ("count", 1), ("count", 9), ("expected_rows", 999999),
    ("timeout", 0), ("timeout", 7201), ("source_version", "invalid"),
    ("additional_source_version", "invalid"),
])
def test_dispatch_guard_rejects_unsafe_or_unrepresentative_inputs(tmp_path, isolated, field, value):
    args = arguments(tmp_path)
    setattr(args, field, value)
    with pytest.raises(ValueError):
        runner.validate_arguments(args)


def test_dispatch_guard_requires_two_distinct_published_identifiers(tmp_path, isolated):
    args = arguments(tmp_path)
    args.additional_source_version = args.source_version
    with pytest.raises(ValueError, match="dos DatasetVersions"):
        runner.validate_arguments(args)


def test_dispatch_population_io_guard_counts_and_restores_real_provider():
    from trackvance.artifactstore import storage_provider

    original = storage_provider.open_read
    with runner.forbid_population_io() as calls, pytest.raises(AssertionError, match="storage.open_read"):
        storage_provider.open_read(None)
    assert calls["storage.open_read"] == 1
    assert sum(calls.values()) == 1
    assert storage_provider.open_read == original


def test_full_preflights_are_sequential_and_failures_cannot_publish():
    calls = []

    def call(path, body=None, *, expected=200):
        calls.append((path, body, expected))
        if path == "/delivery/validations":
            return {"id": "own-validation"}
        return {"status": "SUCCESS", "result": {"status": "FAIL"}}

    report, owned = {"sources": [{"dataset_version_id": "version1", "dataset_id": "dataset1", "row_count": 1000000}]}, []
    with pytest.raises(AssertionError, match="preflight completo real"):
        runner.publish_targets(SimpleNamespace(call=call), [{"target": {"schema_name": "cert"}}] * 5,
                               "0123456789ab", 30, owned, report)
    assert owned == ["own-validation"]
    assert [path for path, _, _ in calls] == ["/delivery/validations", "/delivery/validations/own-validation"]


def test_full_preflight_setup_clones_mapping_uses_unique_targets_and_reports_separate_clock():
    calls, validation_count, configuration_count = [], 0, 0

    def call(path, body=None, *, expected=200):
        nonlocal validation_count, configuration_count
        calls.append((path, body, expected))
        if path == "/delivery/validations":
            validation_count += 1
            return {"id": f"validation-{validation_count}"}
        if path.startswith("/delivery/configurations?"):
            configuration_count += 1
            return {"id": f"config-{configuration_count}"}
        return {"status": "SUCCESS", "result": {"status": "PASS"}}

    source = {"target": {"schema_name": "cert", "table_name": "original"}, "dataset_version_id": "version1",
              "destination_version_id": "destination-revision",
              "columns": [{"source_column": "col", "target_column": "col"}]}
    additional_source = {**source, "dataset_version_id": "version2"}
    report, owned = {"sources": [{"dataset_version_id": f"version{i}", "dataset_id": f"dataset{i}", "row_count": 1000000}
                                 for i in (1, 2)]}, []
    configs = runner.publish_targets(SimpleNamespace(call=call),
        [source, additional_source, source, additional_source, source], "0123456789ab", 30, owned, report)
    drafts = [body for path, body, _ in calls if path == "/delivery/validations"]
    assert len(configs) == len(owned) == 5
    assert len({draft["target"]["table_name"] for draft in drafts}) == 5
    assert all(draft["columns"] == source["columns"] for draft in drafts)
    assert all(draft["target"]["create_schema"] is False and draft["write_strategy"] == "CREATE_AND_LOAD" for draft in drafts)
    assert source["target"]["table_name"] == "original"
    assert report["setup"]["measured_in_dispatch"] is False
    assert report["setup"]["distinct_datasets"] == 2
    assert report["setup"]["version_reuse_declared"] is True
    assert [draft["dataset_version_id"] for draft in drafts] == ["version1", "version2", "version1", "version2", "version1"]
    assert {item["dataset_id"] for item in report["setup"]["complete_api_preflights"]} == {"dataset1", "dataset2"}
    assert report["configuration_inputs"][configs[1]]["source"]["dataset_id"] == "dataset2"
    assert all(item["population_rows"] == 1000000 for item in report["setup"]["complete_api_preflights"])


def test_validation_cleanup_requests_only_own_active_ids_without_rewriting_finished_history():
    calls = []

    def call(path, body=None):
        calls.append((path, body))
        if path.endswith("/cancel"):
            return {"status": "CANCELLED"}
        if path.endswith("active"):
            return {"status": "QUEUED" if len(calls) == 1 else "CANCELLED"}
        return {"status": "SUCCESS"}

    report = {}
    runner.cancel_validations(SimpleNamespace(call=call), ["active", "complete"], report, 30)
    assert [path for path, body in calls if body is not None] == ["/delivery/validations/active/cancel"]
    assert report["cleanup"]["validation_cancel_failures"] == []
    assert report["cleanup"]["validation_terminal"][-1] == {"run_id": "complete", "status": "SUCCESS"}


def test_failure_report_keeps_safe_phase_and_discovers_committed_runs_for_normal_cleanup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "validate_arguments", lambda _args: ("trackvance-v070-test-corrections-0123456789ab", None))
    monkeypatch.setattr(runner, "Client", lambda _: SimpleNamespace(call=lambda *_args, **_kwargs: {"organization": {"id": "org"}, "id": "own-automation"}))
    monkeypatch.setattr(runner, "load_template", lambda *_, **kwargs: ({}, {
        "row_count": 1000000, "dataset_version_id": "version2" if kwargs else "version1",
        "dataset_id": "dataset2" if kwargs else "dataset1"}))
    monkeypatch.setattr(runner, "publish_targets", lambda *_: ["config-1", "config-2", "config-3", "config-4"])
    monkeypatch.setattr(runner, "busy_snapshot", lambda *_: {"live_worker_lease": True})
    monkeypatch.setattr(runner, "wait_value", lambda callback, _predicate, _timeout: callback())
    monkeypatch.setattr(runner, "measured_dispatch", lambda *_: (_ for _ in ()).throw(RuntimeError("password-and-business-data")))
    monkeypatch.setattr(runner, "discover_owned_runs", lambda *_: ["committed-own-run"])
    cleanup = []
    monkeypatch.setattr(runner, "cancel_owned", lambda _client, ids, _org, _report: cleanup.append(("cancel", ids)) or True)
    monkeypatch.setattr(runner, "pause_owned", lambda _client, ids, _report: cleanup.append(("pause", ids)))
    monkeypatch.setattr(sys, "argv", [
        "dispatch", "--source-version", str(uuid4()), "--additional-source-version", str(uuid4()), "--configuration", str(uuid4()),
        "--busy-run", str(uuid4()), "--evidence", str(tmp_path), "--scheduler-paused"])
    assert runner.main() == 1
    report = json.loads((tmp_path / "dispatch-results.json").read_text())
    assert report["failure"]["phase"] == "metadata_only_dispatch_under_real_worker_load"
    assert report["failure"]["exception_type"] == "RuntimeError"
    assert cleanup[0] == ("cancel", ["committed-own-run"])
    assert len(cleanup[1][1]) == 4
    assert "password-and-business-data" not in json.dumps(report) + capsys.readouterr().out
    assert len(report["script_sha256"]) == 64
