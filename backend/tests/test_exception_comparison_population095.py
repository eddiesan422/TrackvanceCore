"""A corrected external snapshot can prove population through the native comparator."""
import sqlite3

import pytest
from test_exception_validation import make_case

from trackvance.artifactstore import storage_provider
from trackvance.comparison_models import VersionComparison
from trackvance.governance_models import DatasetBlock
from trackvance.models import Dataset, DatasetVersion, ExceptionCase
from trackvance.services import assess_exception_validation
from trackvance.version_comparison import compact, compare_rows


def corrected_snapshot_and_report(db, tmp_path):
    case, _config, candidate, _finding, original = make_case(db, "intake", candidate_decision="APPROVED")
    schema = [{"name": "id", "logical_type": "STRING"}, {"name": "customer_id", "logical_type": "STRING"}]
    original.schema_json, original.source_type = schema, "REST"
    corrected = DatasetVersion(dataset_id=original.dataset_id, version=2, filename="corrected.parquet", source_type="REST",
        sha256="c" * 64, schema_hash=original.schema_hash, size_bytes=1, row_count=1, column_count=2,
        original_path="", canonical_path="unused-corrected.parquet", schema_json=schema, profile={}, ingestion_metadata={})
    db.add(corrected)
    db.flush()
    candidate.dataset_version_id = corrected.id
    settings = {"keys": ["id"], "algorithm_version": 1}
    temporary = tmp_path / "comparison-proof.sqlite"
    with sqlite3.connect(temporary) as report:
        summary = compare_rows(report, [{"id": "000001", "customer_id": ""}],
            [{"id": "000001", "customer_id": "CORRECTED"}], schema, schema, keys=settings["keys"])
        summary["before_scope"], summary["after_scope"] = {}, {}
        report.execute("CREATE TABLE report_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        for key, value in {"summary": summary, "settings": settings, "before_version_id": original.id,
                "after_version_id": corrected.id}.items():
            report.execute("INSERT INTO report_metadata VALUES(?,?)", (key, compact(value)))
        report.commit()
    artifact = storage_provider.put_file(db, temporary, "COMPARISON_REPORT", case.organization_id,
        "comparison-proof.sqlite", media_type="application/vnd.sqlite3")
    comparison = VersionComparison(organization_id=case.organization_id, before_version_id=original.id,
        after_version_id=corrected.id, actor_id="test-user", settings=settings, status="SUCCESS", summary=summary,
        artifact_id=artifact.id)
    db.add(comparison)
    db.flush()
    return case, candidate, original, corrected, comparison, artifact


def test_complete_unique_key_comparison_proves_same_external_population_and_preserves_identifiers(database, tmp_path):
    with database() as db:
        case, _candidate, _old, _new, comparison, artifact = corrected_snapshot_and_report(db, tmp_path)
        assessment = assess_exception_validation(db, case)
        assert assessment["validated"] and assessment["can_resolve"]
        proof, = assessment["evidence"]["population_proofs"]
        assert proof["comparison_id"] == comparison.id and proof["artifact_sha256"] == artifact.sha256
        assert proof["report_metadata_verified"] is True and proof["method"] == "VERIFIED_COMPLETE_COMPARISON_KEY_SET"
        assert proof["counts"] == {"ADDED": 0, "ABSENT": 0, "MODIFIED": 1, "UNCHANGED": 0}
        assert "comparación completa" in assessment["reason"]


def test_validate_then_close_records_verified_comparison_and_exact_origin(authenticated, database, tmp_path):
    with database() as db:
        case, candidate, original, corrected, comparison, artifact = corrected_snapshot_and_report(db, tmp_path)
        db.commit()
        identity, origin_id, corrected_id, run_id, report_id, report_hash = case.id, original.id, corrected.id, candidate.id, comparison.id, artifact.sha256
    validated = authenticated.post(f"/api/v1/exceptions/{identity}/validate", json={"version": 1})
    assert validated.status_code == 200, validated.text
    assert validated.json()["validation_evidence"]["population_proofs"][0]["artifact_sha256"] == report_hash
    closed = authenticated.patch(f"/api/v1/exceptions/{identity}", json={"version": 2, "state": "RESOLVED",
        "root_cause": "La fuente externa contenía un valor obligatorio vacío.",
        "resolution": "Nueva adquisición y comparación completa del mismo conjunto de claves; mismo control aprobado."})
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["state"] == "RESOLVED" and body["validation_run_id"] == run_id
    evidence = body["validation_evidence"]
    assert evidence["origin_dataset_version_id"] == origin_id and evidence["validation_dataset_version_id"] == corrected_id
    assert evidence["population_proofs"][0]["comparison_id"] == report_id
    assert evidence["population_proofs"][0]["report_metadata_verified"] is True


def test_candidate_parent_content_block_is_rechecked_for_actual_validator_and_closer(authenticated, database, tmp_path):
    with database() as db:
        case, _candidate, original, corrected, _comparison, _artifact = corrected_snapshot_and_report(db, tmp_path)
        parent = Dataset(name="Restricted candidate parent")
        db.add(parent)
        db.flush()
        parent_version = DatasetVersion(dataset_id=parent.id, version=1, filename="parent.parquet", source_type="UPLOAD",
            sha256="f" * 64, schema_hash=original.schema_hash, size_bytes=1, row_count=1, column_count=2,
            original_path="", canonical_path="unused-parent.parquet", schema_json=original.schema_json, profile={}, ingestion_metadata={})
        db.add(parent_version)
        db.flush()
        corrected.parent_version_id = parent_version.id
        db.add(DatasetBlock(dataset_id=parent.id, scope="CONTENT", reason="Synthetic current content restriction", created_by_id="test-user"))
        db.commit()
        identity = case.id
    validate = authenticated.post(f"/api/v1/exceptions/{identity}/validate", json={"version": 1})
    assert validate.status_code == 403 and validate.json()["error"]["code"] == "DATASET_BLOCKED"
    close = authenticated.patch(f"/api/v1/exceptions/{identity}", json={"version": 1, "state": "RESOLVED",
        "root_cause": "Correction", "resolution": "A later run passed"})
    assert close.status_code == 403 and close.json()["error"]["code"] == "DATASET_BLOCKED"
    with database() as db:
        unchanged = db.get(ExceptionCase, identity)
        assert unchanged.version == 1 and unchanged.state == "PENDING_VALIDATION" and not unchanged.validation_run_id


@pytest.mark.parametrize("damage", ["forged_summary", "hash", "wrong_pair", "ambiguous", "counts", "settings", "key_type", "independent_document_batch", "window"])
def test_invalid_or_unrelated_comparator_evidence_cannot_close_external_incident(database, tmp_path, damage):
    with database() as db:
        case, _candidate, _old, corrected, comparison, artifact = corrected_snapshot_and_report(db, tmp_path)
        if damage == "forged_summary":
            comparison.summary = {**comparison.summary, "counts": {"ADDED": 0, "ABSENT": 0, "MODIFIED": 0, "UNCHANGED": 1}}
        elif damage == "hash":
            with storage_provider.materialize(artifact).open("ab") as report:
                report.write(b"tampered-evidence")
        elif damage == "wrong_pair":
            comparison.before_version_id = corrected.id
        elif damage == "ambiguous":
            comparison.summary = {**comparison.summary, "mode": "MULTISET"}
        elif damage == "counts":
            comparison.summary = {**comparison.summary, "counts": {"ADDED": 1, "ABSENT": 0, "MODIFIED": 1, "UNCHANGED": 0}}
        elif damage == "settings":
            comparison.settings = {**comparison.settings, "keys": ["customer_id"]}
            comparison.summary = {**comparison.summary, "keys": ["customer_id"]}
        elif damage == "key_type":
            corrected.schema_json = [{"name": "id", "logical_type": "INT64"}, {"name": "customer_id", "logical_type": "STRING"}]
        elif damage == "independent_document_batch":
            corrected.ingestion_metadata = {"document_publication": {"batch_id": "independent-lot"}}
        else:
            corrected.ingestion_metadata = {"window_start": "2026-10-15T00:00:00Z", "window_end": "2026-10-16T00:00:00Z"}
        db.flush()
        assessment = assess_exception_validation(db, case)
        assert not assessment["validated"] and not assessment["can_resolve"]
        assert assessment["evidence"]["criterion"] == "VALIDATION_POPULATION_MISMATCH"
