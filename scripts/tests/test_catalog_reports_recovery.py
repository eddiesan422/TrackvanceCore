"""Recovery refuses incomplete native state and unguarded Docker scopes."""

import json
from types import SimpleNamespace

import catalog_reports_recovery as recovery
import pytest


def test_authentic_source_is_fixed_070_commit():
    assert recovery.AUTHENTIC_070 == "d9b6856e757a2a1fcab3913209146f3b7b79d70c"


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

    def failed_backup(identity, _destination):
        assert identity == project and recovery.docker_state.compose is adapter
        raise ValueError("controlled backup failure")

    monkeypatch.setattr(recovery.docker_state, "backup", failed_backup)
    with pytest.raises(ValueError, match="controlled backup failure"):
        recovery.native_cycle(tmp_path, {"project": project}, tmp_path)
    assert recovery.docker_state.compose is original
