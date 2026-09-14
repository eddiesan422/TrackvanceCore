import copy
import hashlib
import json
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import func, select

from trackvance.artifactstore import ArtifactIntegrityError, FileArtifactStore, artifact_dto
from trackvance.audit_context import Actor, actor_context, request_id_context, sanitize_metadata
from trackvance.manifests import adapt_manifest, read_manifest
from trackvance.models import (
    Artifact,
    ArtifactLink,
    AuditEvent,
    DatasetVersion,
    Run,
    SentinelMetricHistory,
)
from trackvance.services import (
    _record_metric_history,
    audit,
    backfill_artifacts,
    ensure_input,
    execute_run,
    register_export,
    version_dto,
)
from trackvance.worker import process_once

FIXTURES = Path(__file__).parent / "fixtures" / "manifests"


def test_artifact_store_copy_verify_immutability_and_boundary(database, tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    store = FileArtifactStore(root)
    source = tmp_path / "input.csv"
    source.write_bytes(b"id\n001234567\n1234567\n")
    with database() as db:
        first = store.put_file(db, source, "ORIGINAL_UPLOAD", "org-a", "input.csv", "first-artifact")
        db.commit()
        assert first.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
        assert first.size_bytes == source.stat().st_size
        assert first.path != str(source)
        assert store.open_read(first).read() == source.read_bytes()
        repeat = store.put_file(db, source, "ORIGINAL_UPLOAD", "org-a", "input.csv", "first-artifact")
        assert repeat.id == first.id
        with pytest.raises(ArtifactIntegrityError, match="organización"):
            store.register_existing(db, Path(first.path), "ORIGINAL_UPLOAD", "org-b")
        source.write_bytes(b"different")
        with pytest.raises(ArtifactIntegrityError, match="IMMUTABLE"):
            store.put_file(db, source, "ORIGINAL_UPLOAD", "org-a", "input.csv", "first-artifact")
        assert store.verify(first).read_bytes() == b"id\n001234567\n1234567\n"
        with pytest.raises(ArtifactIntegrityError, match="fuera"):
            store.checked_path(tmp_path / "outside.csv")
        Path(first.path).write_bytes(b"tampered")
        with pytest.raises(ArtifactIntegrityError, match="HASH_MISMATCH"):
            store.verify(first)


def test_intake_canonical_parquet_lineage_manifest_and_repeat_safety(database, queued_intake):
    assert process_once("evidence-worker")
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        assert run.status == "SUCCESS", run.error
        output = db.get(DatasetVersion, run.output_version_id)
        canonical = db.get(Artifact, output.canonical_artifact_id)
        assert output.source_type == "INTAKE_OUTPUT"
        assert output.original_artifact_id is None and output.original_path == ""
        assert output.filename == "accepted.parquet"
        assert canonical.kind == "INTAKE_ACCEPTED" and Path(canonical.path).suffix == ".parquet"
        assert output.sha256 == canonical.sha256
        assert output.source_run_id == run.id
        assert ensure_input(output, db).to_dicts() == [{"order_id": "A", "amount": "12.25"}]
        dto = version_dto(output, db)
        assert dto["is_derived"] is True and dto["has_original_upload"] is False
        assert {link["relation"] for link in dto["lineage"]} >= {"RUN_OUTPUT", "INTAKE_ACCEPTED_FROM", "DERIVED_FROM"}
        manifest_bytes = Path(run.evidence_path).read_bytes()
        result_bytes = Path(run.result_path).read_bytes()
        manifest = json.loads(manifest_bytes)
        assert manifest["schema_version"] == 2
        assert manifest["initiated_by"] == {"type": "USER", "id": "test-user", "display_name": "Test User"}
        assert manifest["processing"]["engine"] == "POLARS"
        assert {a["kind"] for a in manifest["result_artifacts"]} == {"INTAKE_ACCEPTED", "INTAKE_ERRORS"}
        for metadata in manifest["result_artifacts"]:
            artifact = db.get(Artifact, metadata["artifact_id"])
            assert metadata == artifact_dto(artifact)
            assert metadata["sha256"] == hashlib.sha256(Path(artifact.path).read_bytes()).hexdigest()
        identities = (run.output_version_id, run.result_path, run.evidence_path)
        execute_run(db, run, lease_owner="different-worker")
        db.commit()
        assert identities == (run.output_version_id, run.result_path, run.evidence_path)
        assert manifest_bytes == Path(run.evidence_path).read_bytes()
        assert result_bytes == Path(run.result_path).read_bytes()


def test_backfill_is_idempotent_and_keeps_old_csv_identity_and_profile(database, queued_intake):
    assert process_once("legacy-worker")
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        output = db.get(DatasetVersion, run.output_version_id)
        legacy = Path(output.canonical_path).with_name("accepted.csv")
        pl.read_parquet(output.canonical_path).write_csv(legacy)
        output.original_path, output.filename = str(legacy), "accepted.csv"
        output.sha256 = hashlib.sha256(legacy.read_bytes()).hexdigest()
        output.canonical_artifact_id = None
        output.source_run_id = None
        before = copy.deepcopy((output.profile, output.schema_json, output.sha256, output.schema_hash,
                                output.filename, output.original_path, output.canonical_path))
        evidence_bytes = Path(run.evidence_path).read_bytes()
        result_bytes = Path(run.result_path).read_bytes()
        backfill_artifacts(db)
        db.commit()
        total = db.scalar(select(func.count(Artifact.id)))
        links = db.scalar(select(func.count(ArtifactLink.id)))
        backfill_artifacts(db)
        db.commit()
        assert db.scalar(select(func.count(Artifact.id))) == total
        assert db.scalar(select(func.count(ArtifactLink.id))) == links
        assert before == (output.profile, output.schema_json, output.sha256, output.schema_hash,
                          output.filename, output.original_path, output.canonical_path)
        assert output.canonical_artifact_id and output.source_run_id == run.id
        assert output.original_artifact_id is None
        assert Path(run.evidence_path).read_bytes() == evidence_bytes
        assert Path(run.result_path).read_bytes() == result_bytes


def test_canonical_corruption_cannot_be_used_by_a_run(database, queued_intake):
    with database() as db:
        version = db.get(DatasetVersion, queued_intake["version_id"])
        Path(version.canonical_path).write_bytes(b"not parquet")
        with pytest.raises(ValueError, match="HASH_MISMATCH"):
            ensure_input(version, db)


def test_manifest_v1_golden_adapter_never_rewrites_input(tmp_path):
    raw = (FIXTURES / "v1.json").read_bytes()
    file = tmp_path / "evidence.json"
    file.write_bytes(raw)
    artifact = {"artifact_id": "artifact-results", "kind": "RECON_RESULTS", "name": "results.parquet",
                "sha256": "abc123", "size_bytes": 1234}
    adapted = read_manifest(file, Actor("USER", "user-123", "Equipo Trackvance"), [artifact])
    expected = json.loads((FIXTURES / "v1-adapted-v2.json").read_text(encoding="utf-8"))
    assert adapted == expected
    assert file.read_bytes() == raw
    assert adapt_manifest(adapted) == adapted
    adapted["metrics"]["matched"] = 0
    assert read_manifest(file)["metrics"]["matched"] == 110


def test_manifest_unknown_versions_and_unresolved_legacy_identity():
    with pytest.raises(ValueError, match="no soportada"):
        adapt_manifest({"schema_version": 999})
    result = adapt_manifest({"initiated_by": "Unknown Name", "result_artifacts": [{"name": "x", "sha256": "y"}]})
    assert result["initiated_by"]["type"] == "SYSTEM"
    assert result["identity_resolution"] == "LEGACY_UNRESOLVED_DISPLAY_NAME"
    assert result["result_artifacts"][0]["legacy_metadata_incomplete"]


def test_audit_actor_request_run_and_allowlisted_metadata(database):
    token = actor_context.set(Actor("USER", "test-user", "Test User"))
    request_token = request_id_context.set("request-123")
    try:
        with database() as db:
            audit(db, "RUN_EXPORTED", "run", "run-123", "Informe Excel descargado", metadata={
                "format": "xlsx", "artifact_id": "a-123", "password": "secret", "token": "secret", "rows": [{"email": "private@example.test"}]})
            db.commit()
            event = db.scalar(select(AuditEvent))
            assert (event.actor_type, event.actor_id, event.actor) == ("USER", "test-user", "Test User")
            assert event.request_id == "request-123" and event.run_id == "run-123"
            assert event.metadata_json == {"format": "xlsx", "artifact_id": "a-123"}
    finally:
        actor_context.reset(token)
        request_id_context.reset(request_token)
    assert sanitize_metadata({"config": {"token": "hidden"}, "data": "private"}) == {}


def test_export_registered_with_lineage_survives_temp_cleanup(database, queued_intake, tmp_path):
    source = tmp_path / "trackvance_intake_sample.xlsx"
    source.write_bytes(b"fake xlsx bytes - storage test only")
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        artifact = register_export(db, run, source)
        db.commit()
        source.unlink()
        assert Path(artifact.path).read_bytes().startswith(b"fake xlsx")
        assert artifact.kind == "EXPORT_XLSX"
        assert artifact.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        link = db.scalar(select(ArtifactLink).where(ArtifactLink.relation == "EXPORT_OF"))
        assert (link.source_id, link.target_id) == (artifact.id, run.id)


def test_metric_history_keeps_method_version_dimensions_and_is_idempotent(database, queued_intake):
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        metrics = {"metric_records": [{"metric_key": "distinct_count:order_id", "dimensions": {"column": "order_id"},
            "numeric_value": "3", "method": "EXACT_OBSERVED", "metric_definition_version": 2}]}
        _record_metric_history(db, run, metrics)
        db.flush()
        _record_metric_history(db, run, metrics)
        db.commit()
        records = db.scalars(select(SentinelMetricHistory)).all()
        assert len(records) == 1
        assert records[0].numeric_value == "3" and records[0].metric_definition_version == 2
        assert records[0].dimensions == {"column": "order_id"}
