"""Schema-11 fingerprints retain the complete authentic schema-10 contract."""
from copy import deepcopy

import pytest
from test_docker_state import (
    docker_state,
    restored_current_state,
    rewrite_legacy_state,
    write_delivery_backup,
)
from test_verify_storage import core090_bootstrap_rows, verify_storage


def core095_rows():
    _older, before = core090_bootstrap_rows()
    before["acquisition_runs"] = [{"id": "historical-failure", "organization_id": "org", "dataset_id": "dataset",
        "status": "FAILED", "source_snapshot": {}, "error_code": "HISTORICAL", "processed_rows": 17}]
    before["jobs"] = [{"id": "historical-job", "organization_id": "org", "run_id": "historical-run",
        "acquisition_id": None, "report_execution_id": None, "lane": "DEFAULT", "status": "SUCCESS"}]
    after = deepcopy(before)
    after.update({name: [] for name in verify_storage.CORE095_TABLES})
    for name, defaults in verify_storage.CORE095_DEFAULTS.items():
        for record in after[name]:
            record.update(deepcopy(defaults))
    return before, after


def test_v11_projects_exact_65_table_history_and_all_old_values_without_mutation():
    before, after = core095_rows()
    immutable = deepcopy(after)
    report = verify_storage.legacy_v10_report(after, [], current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=0, verified_source_secrets=0, verified_delivery_secrets=0)
    assert report["schema_version"] == 10 and report["migration"] == "0022_intake_automation_deletion"
    assert report["tables"] == verify_storage._table_hashes(before)
    assert after == immutable
    assert len(report["tables"]) == 65
    assert len(verify_storage.FINGERPRINT_TABLES[verify_storage.CURRENT_MIGRATION]) == 77
    assert len(verify_storage.CORE095_TABLES) == 12


@pytest.mark.parametrize("damage", ["activity", "retry", "missing_default", "provisional", "document_job", "unknown_table"])
def test_v10_projection_never_hides_product_activity_or_unknown_schema(damage):
    _before, after = core095_rows()
    if damage == "activity":
        after["acquisition_source_configurations"].append({"id": "new-setting"})
    elif damage == "retry":
        after["acquisition_runs"][0]["retry_state"] = {"attempt": 2}
    elif damage == "missing_default":
        after["jobs"][0].pop("comparison_id")
    elif damage == "provisional":
        after["acquisition_runs"][0].update(dataset_id=None, source_snapshot={"pending_dataset": {"name": "New"}})
    elif damage == "document_job":
        after["jobs"][0].update(run_id=None, document_id="new-doc", lane="DOCUMENT")
    else:
        after["unrecognized"] = []
    with pytest.raises(ValueError):
        verify_storage.project_core095_upgrade(after, [])


def test_projection_retains_a_changed_historical_value_for_hash_comparison():
    before, after = core095_rows()
    after["acquisition_runs"][0]["processed_rows"] = 18
    projected, _ = verify_storage.project_core095_upgrade(after, [])
    assert verify_storage._table_hashes(projected) != verify_storage._table_hashes(before)


@pytest.mark.parametrize("schema,migration", [(10, "0022_intake_automation_deletion"), (11, "0025_comparison_095")])
def test_snapshot_labels_both_authentic_runtimes_instead_of_relabeling_history(monkeypatch, schema, migration):
    rows = {name: [] for name in verify_storage.FINGERPRINT_TABLES[migration]}
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (migration, rows, [], 0, 0, 0))
    report = verify_storage.snapshot()
    assert report["schema_version"] == schema and report["migration"] == migration
    assert len(report["tables"]) == (65 if schema == 10 else 77)


def test_backup10_requires_independently_validated_projection_before_restore11(tmp_path):
    directory = tmp_path / "backup090"
    manifest, expected = write_delivery_backup(directory, current=True)
    expected.update(schema_version=10, migration=docker_state.CORE090_MIGRATION)
    expected["tables"].update({name: {} for name in docker_state.CORE090_STATE_TABLES_ALL - set(expected["tables"])})
    manifest["migration"] = docker_state.CORE090_MIGRATION
    rewrite_legacy_state(directory, manifest, expected)
    assert docker_state.verify_backup(directory) == manifest
    restored = deepcopy(expected)
    restored.update(schema_version=11, migration=docker_state.CURRENT_MIGRATION)
    restored["tables"].update({name: {} for name in docker_state.CORE095_STATE_TABLES})
    docker_state.validate_restored_state(manifest, expected, restored, expected)
    with pytest.raises(docker_state.OperationError, match="v10 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored)
    changed = deepcopy(expected)
    changed["tables"]["delivery_attempts"]["unknown"] = "f" * 64
    with pytest.raises(docker_state.OperationError, match="v10 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored, changed)
    restored["tables"]["document_families"]["new-activity"] = "a" * 64
    with pytest.raises(docker_state.OperationError, match="v10 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored, expected)


def test_new_native_job_lanes_are_exclusive_and_organization_scoped():
    for table, lane, field in (("document_originals", "DOCUMENT", "document_id"), ("version_comparisons", "COMPARISON", "comparison_id")):
        rows = {"jobs": [{"id": "job", "organization_id": "org", "lane": lane, field: "subject"}],
                table: [{"id": "subject", "organization_id": "org"}]}
        # The generic Job invariant runs before subject-specific document data.
        job = rows["jobs"][0]
        job["run_id"] = "other"
        with pytest.raises(ValueError, match="exactamente un Run"):
            verify_storage.validate_relationships(rows, [])
        job["run_id"] = None
        job["organization_id"] = "other"
        with pytest.raises(ValueError, match="organización"):
            verify_storage.validate_relationships(rows, [])


def test_current_fingerprint_inventory_and_backup_validator_agree():
    current = restored_current_state()
    docker_state.validate_delivery_state(current)
    assert set(current["tables"]) == verify_storage.FINGERPRINT_TABLES[verify_storage.CURRENT_MIGRATION]
    assert current["schema_version"] == verify_storage.SCHEMA_VERSION == 11
    assert current["migration"] == verify_storage.CURRENT_MIGRATION == "0025_comparison_095"


def test_new_workers_are_quiesced_and_only_previously_running_workers_restart(monkeypatch):
    state = {"containers": [{"id": "doc-worker", "service": "document-worker", "running": True},
        {"id": "comparison-worker", "service": "comparison-worker", "running": False}]}
    commands = []
    monkeypatch.setattr(docker_state, "execute", lambda arguments, **_kwargs: commands.append(arguments))
    stopped = docker_state._stop_services(state, ("document-worker", "comparison-worker"))
    assert stopped == ["doc-worker"]
    docker_state._restart_containers(state, set(stopped))
    assert commands == [["docker", "stop", "doc-worker"], ["docker", "start", "doc-worker"]]
    assert {"document-worker", "comparison-worker"} <= docker_state.OPTIONAL_SERVICES


def test_restore_snapshot_routes_the_actual_v10_command(monkeypatch, tmp_path):
    import json

    commands = []

    def execute(arguments, **_kwargs):
        commands.append(arguments)
        return json.dumps({"schema_version": 10}) if arguments[:2] == ["docker", "exec"] else ""

    monkeypatch.setattr(docker_state, "execute", execute)
    value = docker_state._copy_snapshot("synthetic-api", tmp_path / "state.json", command="snapshot-legacy-v10")
    assert value["schema_version"] == 10
    assert commands[-1][-1] == "snapshot-legacy-v10"
