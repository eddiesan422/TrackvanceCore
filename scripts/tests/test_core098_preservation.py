"""Schema 13 preserves schema 12 exactly and refuses to erase IDP provenance."""
import ast
from copy import deepcopy
from pathlib import Path

import pytest
from test_core097_preservation import core097_rows
from test_docker_state import docker_state, rewrite_legacy_state, write_delivery_backup
from test_verify_storage import verify_storage

LEGACY_DOCUMENT_PHASES = {"QUEUED", "EXTRACTION", "OCR", "LLM", "VALIDATION", "REVIEW_REQUIRED",
    "PUBLICATION", "COMPLETE", "FAILED", "CANCELLED"}
LOSSLESS_DOCUMENT_PHASES = (
    "NATIVE_SOURCE_PROBE", "DIGITAL_NATIVE", "QWEN_VISUAL", "PADDLE_STRUCTURED",
    "PADDLE_RECOGNITION", "RENDER_SOURCE", "QWEN_GENERATION", "IMPORT_PADDLE",
    "IMPORT_PADDLEOCR", "CREATE_PIPELINE", "PREDICT_NEXT", "POSTPROCESS_PAGE",
    "SERIALIZE_OBSERVATIONS", "HASH_MODEL", "WRITE_RESULT", "DONE", "OPEN_SOURCE",
    "RENDER_PAGE", "PERSIST_ORIGINAL_PAGE",
)


def core098_rows():
    _, before = core097_rows()
    before["users"] = [{"id": "actor", "organization_id": "org"}]
    before["artifacts"] = [{"id": "original-artifact", "organization_id": "org", "sha256": "a" * 64}]
    before["document_families"] = [{"id": "family", "organization_id": "org"}]
    definition = {"fields": [], "tables": []}
    before["document_templates"] = [{"id": "template", "family_id": "family", "organization_id": "org",
        "definition": definition, "definition_hash": verify_storage._canonical_hash(definition)}]
    before["document_originals"] = [{"id": "historical-document", "organization_id": "org", "family_id": "family",
        "template_id": "template", "artifact_id": "original-artifact", "sha256": "a" * 64}]
    after = deepcopy(before)
    after.update({name: [] for name in verify_storage.CORE098_TABLES})
    for name, defaults in verify_storage.CORE098_DEFAULTS.items():
        for row in after[name]:
            row.update(defaults)
    return before, after


def test_v13_projects_exact_populated_80_table_history_without_mutation():
    before, after = core098_rows()
    unchanged = deepcopy(after)
    report = verify_storage.legacy_v12_report(after, [], current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=0, verified_source_secrets=0, verified_delivery_secrets=0)
    assert (report["schema_version"], report["migration"]) == (12, "0027_document_builder_097")
    assert report["tables"] == verify_storage._table_hashes(before) and after == unchanged
    assert len(before) == 80 and len(after) == 81


@pytest.mark.parametrize("damage", ["settings", "snapshot", "snapshot_hash", "phase", "missing_default", "unknown"])
def test_historical_projection_rejects_new_provider_evidence_and_unknown_schema(damage):
    _, after = core098_rows()
    if damage == "settings":
        configuration = {"provider": "TESSERACT_LEGACY"}
        after["document_engine_settings"] = [{"id": "new-setting", "organization_id": "org", "actor_id": "actor",
            "scope_key": "", "version": 1, "configuration": configuration,
            "configuration_hash": verify_storage._canonical_hash(configuration)}]
    elif damage == "unknown":
        after["unrecognized"] = []
    else:
        row = after["document_originals"][0]
        if damage == "missing_default":
            del row["engine_snapshot"]
        else:
            row[{"snapshot": "engine_snapshot", "snapshot_hash": "engine_snapshot_hash", "phase": "processing_phase"}[damage]] = "new-evidence"
    with pytest.raises(ValueError):
        verify_storage.project_core098_upgrade(after, [])


@pytest.mark.parametrize("migration,schema,count", [("0027_document_builder_097", 12, 80), ("0028_document_idp_098", 13, 81)])
def test_native_and_historical_snapshots_keep_exact_frozen_revision(monkeypatch, migration, schema, count):
    rows = {name: [] for name in verify_storage.FINGERPRINT_TABLES[migration]}
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (migration, rows, [], 0, 0, 0))
    snapshot = verify_storage.snapshot()
    assert (snapshot["migration"], snapshot["schema_version"], len(snapshot["tables"])) == (migration, schema, count)
    docker_state.validate_delivery_state(snapshot)


def test_v12_backup_requires_independent_exact_projection_before_native_restore(tmp_path):
    directory = tmp_path / "backup097"
    manifest, expected = write_delivery_backup(directory, current=True)
    manifest["migration"] = docker_state.CORE097_MIGRATION
    expected.update(schema_version=12, migration=docker_state.CORE097_MIGRATION)
    expected["tables"].update({name: {} for name in docker_state.CORE097_STATE_TABLES_ALL - set(expected["tables"])})
    rewrite_legacy_state(directory, manifest, expected)
    assert docker_state.verify_backup(directory) == manifest
    restored = deepcopy(expected)
    restored.update(schema_version=13, migration=docker_state.CURRENT_MIGRATION)
    restored["tables"].update({name: {} for name in docker_state.CORE098_STATE_TABLES})
    docker_state.validate_restored_state(manifest, expected, restored, expected)
    with pytest.raises(docker_state.OperationError, match="v12 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored)
    restored["tables"]["document_engine_settings"]["new-setting"] = "a" * 64
    with pytest.raises(docker_state.OperationError, match="v12 normalizada"):
        docker_state.validate_restored_state(manifest, expected, restored, expected)


def test_cli_v12_projection_uses_verified_inputs(monkeypatch, capsys):
    import json

    _, after = core098_rows()
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (verify_storage.CURRENT_MIGRATION, after, [], 0, 0, 0))
    monkeypatch.setattr(verify_storage.sys, "argv", ["verify_storage", "snapshot-legacy-v12"])
    assert verify_storage.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 12 and result["migration"] == verify_storage.CORE097_MIGRATION


@pytest.mark.parametrize("damage", [None, "configuration_hash", "actor_tenant", "family_tenant", "duplicate_scope", "snapshot_hash", "snapshot_family"])
def test_idp_provenance_integrity_and_tenant_boundaries(damage):
    _, rows = core098_rows()
    configuration = {"provider": "TESSERACT_LEGACY"}
    rows["document_engine_settings"] = [{"id": "setting", "organization_id": "org", "actor_id": "actor",
        "scope_key": "family", "version": 1, "configuration": configuration,
        "configuration_hash": verify_storage._canonical_hash(configuration)}]
    snapshot = {"provider": "TESSERACT_LEGACY", "family_id": "family"}
    original = rows["document_originals"][0]
    original.update(engine_snapshot=snapshot, engine_snapshot_hash=verify_storage._canonical_hash(snapshot))
    if damage == "configuration_hash":
        rows["document_engine_settings"][0]["configuration_hash"] = "b" * 64
    elif damage == "actor_tenant":
        rows["users"][0]["organization_id"] = "other"
    elif damage == "family_tenant":
        rows["document_families"][0]["organization_id"] = "other"
    elif damage == "duplicate_scope":
        rows["document_engine_settings"].append({**rows["document_engine_settings"][0], "id": "duplicate"})
    elif damage == "snapshot_hash":
        original["engine_snapshot_hash"] = "c" * 64
    elif damage == "snapshot_family":
        snapshot["family_id"] = "another-family"
        original["engine_snapshot_hash"] = verify_storage._canonical_hash(snapshot)
    if damage is None:
        assert verify_storage.validate_relationships(rows, []) > 0
    else:
        with pytest.raises(ValueError):
            verify_storage.validate_relationships(rows, [])


@pytest.mark.parametrize("phase", LOSSLESS_DOCUMENT_PHASES)
def test_lossless_stage_is_snapshotted_without_rewriting_evidence(monkeypatch, phase):
    _, rows = core098_rows()
    rows["document_originals"][0].update(processing_phase=phase, status="REVIEW_REQUIRED")
    unchanged = deepcopy(rows)
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (
        verify_storage.CURRENT_MIGRATION, rows, [], 0, 0, 0))
    snapshot = verify_storage.snapshot()
    assert snapshot["schema_version"] == 13 and len(snapshot["tables"]) == 81
    assert snapshot["tables"] == verify_storage._table_hashes(unchanged) and rows == unchanged


@pytest.mark.parametrize("phase", [None, *sorted(LEGACY_DOCUMENT_PHASES)])
def test_legacy_document_phases_still_validate(phase):
    _, rows = core098_rows()
    rows["document_originals"][0]["processing_phase"] = phase
    assert verify_storage.validate_relationships(rows, []) > 0


@pytest.mark.parametrize("phase", ["UNKNOWN_PHASE", "QWEN_VISUAL_OTHER", "TERMINAL", "PADDLE", "QWEN", "predict_next",
    "SELECTIVE", "NATIVE_FIRST", "REFERENCE_DISCOVERY"])
def test_unknown_and_health_only_phases_are_not_persisted_event_stages(phase):
    _, rows = core098_rows()
    rows["document_originals"][0]["processing_phase"] = phase
    with pytest.raises(ValueError, match="fase documental persistida"):
        verify_storage.validate_relationships(rows, [])


@pytest.mark.parametrize("damage", [None, "snapshot_hash", "snapshot_family"])
def test_reference_snapshots_keep_hash_and_family_guards_without_a_processing_phase(damage):
    _, rows = core098_rows()
    payload = {"lines": []}
    snapshot = {"provider": "TESSERACT_LEGACY", "family_id": "family"}
    reference = {"id": "reference", "organization_id": "org", "family_id": "family",
        "artifact_id": "original-artifact", "sha256": "a" * 64, "payload": payload,
        "payload_hash": verify_storage._canonical_hash(payload), "engine_snapshot": snapshot,
        "engine_snapshot_hash": verify_storage._canonical_hash(snapshot)}
    rows["document_references"] = [reference]
    assert "processing_phase" not in reference
    if damage == "snapshot_hash":
        reference["engine_snapshot_hash"] = "b" * 64
    elif damage == "snapshot_family":
        snapshot["family_id"] = "missing-family"
        reference["engine_snapshot_hash"] = verify_storage._canonical_hash(snapshot)
    if damage:
        with pytest.raises(ValueError, match="instantánea del motor"):
            verify_storage.validate_relationships(rows, [])
    else:
        assert verify_storage.validate_relationships(rows, []) > 0


def test_independent_phase_catalog_covers_literal_versioned_producer_contracts():
    # Parse source only: importing the gateway/child would initialize runtime state.
    root = Path(__file__).resolve().parents[2]
    lossless = ast.parse((root / "backend/src/trackvance/document_lossless.py").read_text(encoding="utf-8"))
    gateway = ast.parse((root / "deploy/idp/recognition.py").read_text(encoding="utf-8"))
    diagnostics = ast.parse((root / "deploy/idp/idp_observability.py").read_text(encoding="utf-8"))
    child_stages = next(ast.literal_eval(node.value.args[0]) for node in diagnostics.body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "STAGES" for target in node.targets))

    def choices(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return choices(node.body) | choices(node.orelse)
        return set()

    stages = set(child_stages)
    for node in ast.walk(gateway):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "stage":
                    stages.update(choices(keyword.value))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "phase" and node.args:
                stages.update(choices(node.args[0]))
    for node in ast.walk(lossless):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "stage":
                    stages.update(choices(value))
    assert stages == set(LOSSLESS_DOCUMENT_PHASES)
    assert verify_storage.CORE098_DOCUMENT_PHASES == LEGACY_DOCUMENT_PHASES | stages
    assert all(len(stage) <= 30 for stage in stages)
