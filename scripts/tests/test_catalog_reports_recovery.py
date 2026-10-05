"""Recovery refuses incomplete native state and unguarded Docker scopes."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import catalog_reports_recovery as recovery
import pytest


def test_authentic_source_is_fixed_070_commit():
    assert recovery.AUTHENTIC_070 == "d9b6856e757a2a1fcab3913209146f3b7b79d70c"


def test_diagnostic_fixture_passes_bounded_inspection_and_fails_complete_reader(tmp_path):
    from trackvance.batch_readers import FileBatchReader, inspect_file
    from trackvance.processing import ProcessingError

    path = tmp_path / "invalid.csv"
    path.write_bytes(recovery.diagnostic_fixture_payload())
    assert inspect_file(path, path.name)["sampled_rows"] == 100
    with pytest.raises(ProcessingError, match="ACQUISITION_SCHEMA_MISMATCH"):
        list(FileBatchReader(path, path.name))


def test_saved_definition_resolves_its_exact_persisted_schema_draft():
    draft = {"mode": "GUIDED", "expected_schemas": {"a": [{"name": "id", "logical_type": "STRING"}]}}
    calls = []
    api = SimpleNamespace(json=lambda *arguments: calls.append(arguments) or {"context_id": "frozen"})
    result = recovery.resolve_saved_definition(api, {"revisions": [{"id": "r2", "draft": draft}]})
    assert result == {"context_id": "frozen"}
    assert calls == [("POST", "/reports/resolve", {"draft": draft, "revision_id": "r2"})]


def test_native_recovery_requires_populated_new_entities():
    tables = {name: {"row": "hash"} for name in recovery.docker_state.CURRENT_STATE_TABLES}
    state = {"schema_version": 8, "migration": "0017_catalog_reports", "tables": tables}
    recovery.assert_native_state(state)
    for name in recovery.docker_state.CATALOG_STATE_TABLES:
        state["tables"] = {**tables, name: {}}
        with pytest.raises(ValueError, match="trece entidades"):
            recovery.assert_native_state(state)


def test_adapter_rejects_another_project_before_any_command(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("A foreign project must never reach Compose or inventory.")
    monkeypatch.setattr(recovery, "preflight", forbidden)
    monkeypatch.setattr(recovery, "run", forbidden)
    compose = recovery.compose_adapter(tmp_path, {"project": "trackvance-v080-test-own-012345abcdef"}, {})
    with pytest.raises(ValueError, match="proyecto ajeno"):
        compose("trackvance-certification", "down", "--volumes")


def test_habitual_image_rejected_before_docker_inspection(monkeypatch):
    monkeypatch.setattr(recovery.guard, "command", lambda *_args, **_kwargs: pytest.fail("No habitual image inspection expected."))
    with pytest.raises(ValueError, match="imágenes privadas"):
        recovery.image_id("trackvance-api:latest", "backend")


def test_resolved_config_credentials_are_private(monkeypatch, tmp_path):
    secret = "synthetic-config-secret"
    config = {"services": {"api": {"environment": {"POSTGRES_PASSWORD": secret}}}}
    monkeypatch.setattr(recovery, "assert_main", lambda _context: None)
    monkeypatch.setattr(recovery, "run", lambda *_args, **_kwargs: pytest.fail("Resolved secrets must not enter the public command logger."))
    monkeypatch.setattr(recovery.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(config)))
    observed = []
    monkeypatch.setattr(recovery.guard, "validate_resolved", lambda value, *_args: observed.append(value))
    result = recovery.preflight(tmp_path, {"project": "trackvance-v080-test-own-012345abcdef"}, {"POSTGRES_PASSWORD": secret})
    assert result == config and observed == [config]


def test_native_backup_pins_source_adapter_and_restores_it_on_failure(monkeypatch, tmp_path):
    project = "trackvance-v080-test-own-012345abcdef"
    original = recovery.docker_state.compose
    adapter = lambda *_args, **_kwargs: None
    monkeypatch.setattr(recovery, "private_environment", lambda _directory: {"POSTGRES_PASSWORD": "synthetic"})
    monkeypatch.setattr(recovery, "preflight", lambda *_args: None)
    monkeypatch.setattr(recovery, "compose_adapter", lambda *_args: adapter)
    monkeypatch.setattr(recovery, "prepare_native", lambda *_args: {})
    stopped = []
    monkeypatch.setattr(recovery, "stop_quiescent_population", lambda *_args: stopped.append(project) or "STOPPED_QUIESCENT")

    def failed_backup(identity, _destination):
        assert identity == project and recovery.docker_state.compose is adapter
        raise ValueError("controlled backup failure")

    monkeypatch.setattr(recovery.docker_state, "backup", failed_backup)
    with pytest.raises(ValueError, match="controlled backup failure"):
        recovery.native_cycle(tmp_path, {"project": project}, tmp_path)
    assert recovery.docker_state.compose is original
    assert stopped == [project]


def test_native_sequence_starts_fixture_services_then_stops_source_after_restore(monkeypatch, tmp_path):
    project = "trackvance-v080-test-own-012345abcdef"
    environment = {"POSTGRES_PASSWORD": "synthetic"}
    events = []
    original = recovery.docker_state.compose

    def compose(identity, *arguments):
        assert identity == project
        if arguments[0] == "up":
            assert arguments[:2] == ("up", "--no-build")
            assert arguments[-6:] == recovery.NATIVE_FIXTURE_SERVICES
            events.append("start")
        else:
            assert arguments[:3] == ("create", "--no-build", "--no-recreate")
            assert arguments[3:] == ("delivery-worker", "scheduler", "events-notifications", "events-chaining")
            events.append("create_inactive")

    def backup(identity, target):
        assert identity == project
        target.mkdir()
        (target / "state.json").write_text("{}")
        events.append("backup")

    monkeypatch.setattr(recovery, "private_environment", lambda *_args: environment)
    monkeypatch.setattr(recovery, "preflight", lambda *_args: None)
    monkeypatch.setattr(recovery, "compose_adapter", lambda *_args: compose)
    monkeypatch.setattr(recovery, "prepare_native", lambda *_args: events.append("fixture") or {})
    monkeypatch.setattr(recovery.docker_state, "backup", backup)
    monkeypatch.setattr(recovery.docker_state, "verify_backup", lambda *_args: events.append("verify"))
    monkeypatch.setattr(recovery, "assert_native_state", lambda *_args: events.append("state"))
    monkeypatch.setattr(recovery, "scan_backup_plaintext", lambda *_args: events.append("privacy") or {})
    monkeypatch.setattr(recovery, "restore_compare", lambda *_args, **_kwargs: events.append("restore") or {})
    monkeypatch.setattr(recovery, "stop_quiescent_population", lambda *_args: events.append("stop") or "STOPPED_QUIESCENT")
    result = recovery.native_cycle(tmp_path, {"project": project}, tmp_path)
    assert events == ["start", "create_inactive", "fixture", "backup", "verify", "state", "privacy", "restore", "stop"]
    assert result["source_final_state"] == "STOPPED_QUIESCENT"
    assert recovery.docker_state.compose is original


@pytest.mark.parametrize("active", [False, True])
def test_source_population_stop_requires_zero_active_jobs(monkeypatch, tmp_path, active):
    project = "trackvance-v080-test-own-012345abcdef"
    state = {"containers": [{"id": "own-pg", "service": "postgres", "running": True}]}
    calls = []
    monkeypatch.setattr(recovery, "preflight", lambda *_args: None)
    monkeypatch.setattr(recovery.docker_state, "inventory", lambda _project: state)
    monkeypatch.setattr(recovery, "assert_main", lambda *_args: None)

    def query(arguments, _environment):
        assert arguments[:3] == ["docker", "exec", "own-pg"]
        assert "BEGIN READ ONLY" in arguments[-1]
        calls.append("read")
        return "1" if active else "0"

    def stop(identity, *arguments):
        assert identity == project and arguments == ("stop", "--timeout", "30")
        calls.append("stop")
        state["containers"][0]["running"] = False

    monkeypatch.setattr(recovery, "run", query)
    monkeypatch.setattr(recovery.docker_state, "compose", stop)
    environment = {"POSTGRES_USER": "tv_v080_test", "POSTGRES_DB": "tv_v080_test"}
    if active:
        with pytest.raises(ValueError, match="Jobs activos"):
            recovery.stop_quiescent_population(tmp_path, {"project": project}, environment)
        assert calls == ["read"]
    else:
        assert recovery.stop_quiescent_population(tmp_path, {"project": project}, environment) == "STOPPED_QUIESCENT"
        assert calls == ["read", "stop"]


@pytest.mark.parametrize("expired", [True, False])
def test_restored_block_is_checked_independently_of_context_expiry(expired):
    calls = []

    def response(_method, path, _payload, *, expected):
        calls.append((path, expected))
        if path == "/reports/resolve":
            return {"error": {"code": "REPORT_SOURCE_INELIGIBLE", "details": {"reasons": [{"code": "DATASET_BLOCKED"}]}}}
        return {"error": {"code": "DATASET_BLOCKED"}}

    expires = datetime.now(UTC) + timedelta(minutes=-1 if expired else 1)
    fixture = {"context_id": "persisted-context", "context_expires_at": expires.isoformat()}
    result = recovery.verify_restored_restriction(SimpleNamespace(json=response), {"selected_revision": {"id": "revision", "draft": {}}}, fixture)
    assert calls[0] == ("/reports/resolve", 422) and result["current_block_new_resolution"] == "PASS"
    assert result["frozen_context_preview"] == ("NOT_RUN_EXPIRED" if expired else "PASS_CURRENT_BLOCK")
    assert calls == [("/reports/resolve", 422)] + ([] if expired else [("/reports/preview", 403)])
