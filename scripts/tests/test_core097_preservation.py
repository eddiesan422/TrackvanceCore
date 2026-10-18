"""Schema 12 preserves authentic schema 11 without concealing new evidence."""
from copy import deepcopy

import pytest
from test_core095_preservation import core095_rows
from test_docker_state import docker_state, rewrite_legacy_state, write_delivery_backup
from test_verify_storage import verify_storage


def core097_rows():
    _, before = core095_rows()
    before["acquisition_source_configurations"] = [{"id": "source", "organization_id": "org", "dataset_id": "dataset"}]
    before["acquisition_occurrences"] = [{"id": "old-cut", "organization_id": "org", "configuration_id": "source",
        "status": "BLOCKED", "reason_code": "HISTORICAL_FAILURE"}]
    after = deepcopy(before)
    after.update({name: [] for name in verify_storage.CORE097_TABLES})
    for name, defaults in verify_storage.CORE097_DEFAULTS.items():
        for record in after[name]:
            record.update(deepcopy(defaults))
    return before, after


def test_v12_projects_exact_populated_77_table_history_without_mutation():
    before, after = core097_rows()
    untouched = deepcopy(after)
    report = verify_storage.legacy_v11_report(after, [], current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=0, verified_source_secrets=0, verified_delivery_secrets=0)
    assert report["schema_version"] == 11 and report["migration"] == "0025_comparison_095"
    assert report["tables"] == verify_storage._table_hashes(before) and after == untouched
    assert len(report["tables"]) == 77 and len(verify_storage.FINGERPRINT_TABLES[verify_storage.CURRENT_MIGRATION]) == 80


@pytest.mark.parametrize("damage", ["draft", "reference", "test", "occurrence", "diagnostic", "job", "missing_default", "unknown"])
def test_v11_projection_refuses_new_activity_unknown_schema_or_missing_defaults(damage):
    _, after = core097_rows()
    if damage in {"draft", "reference", "test"}:
        table = {"draft": "document_drafts", "reference": "document_references", "test": "document_builder_tests"}[damage]
        after[table] = [{"id": "new-evidence"}]
    elif damage == "occurrence":
        after["acquisition_occurrences"][0]["coalesced_intervals"] = 1
    elif damage == "diagnostic":
        after["acquisition_source_configurations"][0]["dispatch_diagnostic"] = {"code": "NEW_FAILURE"}
    elif damage == "job":
        after["jobs"][0]["document_reference_id"] = "new-reference"
    elif damage == "missing_default":
        del after["jobs"][0]["document_reference_id"]
    else:
        after["unknown"] = []
    with pytest.raises(ValueError):
        verify_storage.project_core097_upgrade(after, [])


@pytest.mark.parametrize("migration, schema, count", [("0025_comparison_095", 11, 77), ("0027_document_builder_097", 12, 80)])
def test_snapshots_retain_their_real_revision_and_frozen_inventory(monkeypatch, migration, schema, count):
    rows = {name: [] for name in verify_storage.FINGERPRINT_TABLES[migration]}
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (migration, rows, [], 0, 0, 0))
    snapshot = verify_storage.snapshot()
    assert (snapshot["schema_version"], snapshot["migration"], len(snapshot["tables"])) == (schema, migration, count)
    docker_state.validate_delivery_state(snapshot)


def test_backup11_remains_readable_and_requires_exact_independent_projection_to12(tmp_path):
    directory = tmp_path / "backup095"
    manifest, expected = write_delivery_backup(directory, current=True)
    expected.update(schema_version=11, migration=docker_state.CORE095_MIGRATION)
    expected["tables"].update({name: {} for name in docker_state.CORE095_STATE_TABLES_ALL - set(expected["tables"])})
    manifest["migration"] = docker_state.CORE095_MIGRATION
    rewrite_legacy_state(directory, manifest, expected)
    assert docker_state.verify_backup(directory) == manifest
    restored = deepcopy(expected)
    restored.update(schema_version=12, migration=docker_state.CURRENT_MIGRATION)
    restored["tables"].update({name: {} for name in docker_state.CORE097_STATE_TABLES})
    docker_state.validate_restored_state(manifest, expected, restored, expected)
    with pytest.raises(docker_state.OperationError, match="v11 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored)
    changed = deepcopy(expected)
    changed["tables"]["delivery_attempts"]["unknown"] = "f" * 64
    with pytest.raises(docker_state.OperationError, match="v11 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored, changed)
    restored["tables"]["document_drafts"]["new-evidence"] = "a" * 64
    with pytest.raises(docker_state.OperationError, match="v11 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored, expected)


def test_cli_v11_projection_uses_verified_inputs(monkeypatch, capsys):
    import json

    _, after = core097_rows()
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (verify_storage.CURRENT_MIGRATION, after, [], 0, 0, 0))
    monkeypatch.setattr(verify_storage.sys, "argv", ["verify_storage", "snapshot-legacy-v11"])
    assert verify_storage.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 11 and result["migration"] == "0025_comparison_095"
