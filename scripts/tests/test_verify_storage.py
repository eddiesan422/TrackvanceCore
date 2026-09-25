import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trackvance.db import Base
from trackvance.models import Artifact, ArtifactLink, ExceptionCase

SCRIPT = Path(__file__).resolve().parents[1] / "verify_storage.py"
spec = importlib.util.spec_from_file_location("verify_storage", SCRIPT)
verify_storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_storage)


def minimal_rows():
    return {
        "artifacts": [{"id": "artifact", "organization_id": "org"}],
        "datasets": [{"id": "dataset", "organization_id": "org"}],
        "dataset_versions": [
            {
                "id": "version",
                "organization_id": "org",
                "dataset_id": "dataset",
                "parent_version_id": None,
                "source_run_id": None,
                "ingestion_metadata": {},
            }
        ],
        "runs": [],
        "external_connections": [],
        "external_connection_versions": [],
        "artifact_links": [
            {
                "id": "link",
                "organization_id": "org",
                "source_type": "DATASET_VERSION",
                "source_id": "version",
                "target_type": "ARTIFACT",
                "target_id": "artifact",
            }
        ],
    }


def exception_evidence_rows(
    *, exception_id: str = "exception", exception_org: str = "org"
):
    """Round-trip the production ORM mappings used by snapshot()."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                ExceptionCase(
                    id="exception",
                    organization_id=exception_org,
                    display_id="EXC-ORM-1",
                    run_id="run",
                    configuration_id="configuration",
                    title="ORM evidence",
                    module="SENTINEL",
                    severity="HIGH",
                ),
                Artifact(
                    id="artifact",
                    organization_id="org",
                    kind="EXCEPTION_ATTACHMENT",
                    name="evidence.txt",
                    path="artifacts/evidence.txt",
                    sha256="0" * 64,
                    size_bytes=1,
                    media_type="text/plain",
                ),
                ArtifactLink(
                    id="link",
                    organization_id="org",
                    relation="EXCEPTION_EVIDENCE",
                    source_type="EXCEPTION",
                    source_id=exception_id,
                    target_type="ARTIFACT",
                    target_id="artifact",
                ),
            ]
        )
        session.commit()
        return {
            table_name: list(
                session.execute(select(Base.metadata.tables[table_name])).mappings()
            )
            for table_name in ("exceptions", "artifacts", "artifact_links")
        }


def test_relationship_validation_covers_declared_and_polymorphic_links():
    rows = minimal_rows()
    checks = verify_storage.validate_relationships(
        rows, [("dataset_versions", "dataset_id", "datasets", "id")]
    )
    assert checks == 3


def test_relationship_validation_rejects_cross_organization_link():
    rows = minimal_rows()
    rows["artifacts"][0]["organization_id"] = "other-org"
    with pytest.raises(ValueError, match="organización"):
        verify_storage.validate_relationships(rows, [])


def test_exception_evidence_uses_real_orm_table_mapping():
    assert verify_storage.validate_relationships(exception_evidence_rows(), []) == 2


def test_exception_evidence_rejects_missing_exception_from_real_orm_rows():
    rows = exception_evidence_rows(exception_id="missing")
    with pytest.raises(ValueError, match="source EXCEPTION"):
        verify_storage.validate_relationships(rows, [])


def test_exception_evidence_rejects_cross_organization_from_real_orm_rows():
    rows = exception_evidence_rows(exception_org="other-org")
    with pytest.raises(ValueError, match="organización"):
        verify_storage.validate_relationships(rows, [])


def test_relationship_validation_rejects_stale_source_identity():
    rows = minimal_rows()
    rows["external_connections"] = [{"id": "connection", "organization_id": "org"}]
    rows["external_connection_versions"] = [
        {
            "id": "connection-version",
            "organization_id": "org",
            "connection_id": "connection",
            "config_hash": "current",
        }
    ]
    rows["dataset_versions"][0]["ingestion_metadata"] = {
        "source": {
            "connection_id": "connection",
            "connection_version_id": "connection-version",
            "config_hash": "stale",
        }
    }
    with pytest.raises(ValueError, match="config_hash"):
        verify_storage.validate_relationships(rows, [])


def test_compare_reports_new_scheduler_table_change():
    before = {
        "schema_version": 3,
        "tables": {"monitor_occurrences": {"one": "before"}},
    }
    after = {
        "schema_version": 3,
        "tables": {"monitor_occurrences": {"one": "after"}},
    }
    with pytest.raises(ValueError, match="monitor_occurrences"):
        verify_storage.compare(before, after)


def delivery_rows():
    return {
        "runs": [{"id": "run", "organization_id": "org", "module": "delivery"}],
        "jobs": [
            {
                "id": "job",
                "organization_id": "org",
                "run_id": "run",
                "lane": "DELIVERY",
            }
        ],
        "delivery_destinations": [
            {"id": "destination", "organization_id": "org"}
        ],
        "delivery_destination_versions": [
            {
                "id": "destination-version",
                "organization_id": "org",
                "destination_id": "destination",
            }
        ],
        "delivery_attempts": [
            {
                "id": "attempt",
                "organization_id": "org",
                "run_id": "run",
                "destination_version_id": "destination-version",
                "status": "COMMITTED",
            }
        ],
        "artifact_links": [
            {
                "id": "delivery-link",
                "organization_id": "org",
                "source_type": "RUN",
                "source_id": "run",
                "target_type": "DELIVERY_DESTINATION_VERSION",
                "target_id": "destination-version",
            }
        ],
    }


def test_delivery_relationships_lane_attempt_and_polymorphic_target_are_validated():
    rows = delivery_rows()
    checks = verify_storage.validate_relationships(
        rows,
        [
            (
                "delivery_destination_versions",
                "destination_id",
                "delivery_destinations",
                "id",
            ),
            ("delivery_attempts", "run_id", "runs", "id"),
            (
                "delivery_attempts",
                "destination_version_id",
                "delivery_destination_versions",
                "id",
            ),
        ],
    )
    assert checks == 7


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("lane", "DEFAULT", "lane"),
        ("status", "RETRYING", "Estado"),
    ],
)
def test_delivery_relationships_reject_invalid_lane_or_attempt_status(
    field, value, message
):
    rows = delivery_rows()
    target = rows["jobs"][0] if field == "lane" else rows["delivery_attempts"][0]
    target[field] = value
    with pytest.raises(ValueError, match=message):
        verify_storage.validate_relationships(rows, [])


def legacy_compatible_rows():
    rows = minimal_rows()
    rows["runs"] = [
        {"id": "run", "organization_id": "org", "module": "INTAKE"}
    ]
    rows["jobs"] = [
        {
            "id": "job",
            "organization_id": "org",
            "run_id": "run",
            "status": "SUCCESS",
            "lane": "DEFAULT",
        }
    ]
    for table in verify_storage.DELIVERY_TABLES:
        rows[table] = []
    return rows


def test_legacy_v2_report_recalculates_complete_pre_0008_fingerprint():
    rows = legacy_compatible_rows()
    foreign_keys = [
        ("dataset_versions", "dataset_id", "datasets", "id"),
        ("jobs", "run_id", "runs", "id"),
    ]

    report = verify_storage.legacy_v2_report(
        rows,
        foreign_keys,
        current_migration="0008_data_delivery",
        verified_artifacts=1,
        verified_source_secrets=2,
    )

    legacy_job = {key: value for key, value in rows["jobs"][0].items() if key != "lane"}
    assert report["schema_version"] == 2
    assert report["migration"] == "0007_monitor_scheduling"
    assert report["verified_artifacts"] == 1
    assert report["verified_secrets"] == 2
    assert report["validated_relationships"] == 4
    assert not (verify_storage.DELIVERY_TABLES & report["tables"].keys())
    assert report["tables"]["jobs"]["job"] == verify_storage._canonical_hash(legacy_job)
    assert report["tables"]["jobs"]["job"] != verify_storage._canonical_hash(
        rows["jobs"][0]
    )


def test_legacy_v2_report_detects_changes_to_job_payload_after_lane_is_removed():
    rows = legacy_compatible_rows()
    baseline = verify_storage.legacy_v2_report(
        rows,
        [("jobs", "run_id", "runs", "id")],
        current_migration="0008_data_delivery",
        verified_artifacts=1,
        verified_source_secrets=0,
    )
    rows["jobs"][0]["status"] = "FAILED"
    changed = verify_storage.legacy_v2_report(
        rows,
        [("jobs", "run_id", "runs", "id")],
        current_migration="0008_data_delivery",
        verified_artifacts=1,
        verified_source_secrets=0,
    )

    assert baseline["tables"]["jobs"] != changed["tables"]["jobs"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("delivery", "registros de Data Delivery"),
        ("delivery_run", "Runs DELIVERY"),
        ("non_default_lane", "lane DEFAULT"),
    ],
)
def test_legacy_v2_report_rejects_non_deterministic_0008_state(mutation, message):
    rows = deepcopy(legacy_compatible_rows())
    if mutation == "delivery":
        rows["delivery_destinations"].append(
            {"id": "destination", "organization_id": "org"}
        )
    elif mutation == "delivery_run":
        rows["runs"][0]["module"] = "DELIVERY"
    else:
        rows["jobs"][0]["lane"] = "DELIVERY"

    with pytest.raises(ValueError, match=message):
        verify_storage.legacy_v2_report(
            rows,
            [("jobs", "run_id", "runs", "id")],
            current_migration="0008_data_delivery",
            verified_artifacts=0,
            verified_source_secrets=0,
        )


def test_legacy_v2_report_requires_exact_post_migration_revision():
    with pytest.raises(ValueError, match="migración 0008"):
        verify_storage.legacy_v2_report(
            legacy_compatible_rows(),
            [],
            current_migration="0007_monitor_scheduling",
            verified_artifacts=0,
            verified_source_secrets=0,
        )


def reviewed_rows():
    rows = delivery_rows()
    rows["runs"][0]["status"] = "UNKNOWN"
    rows["runs"][0]["decision"] = "UNKNOWN"
    rows["delivery_attempts"][0]["status"] = "UNKNOWN"
    rows["users"] = [{"id": "reviewer", "organization_id": "org"}]
    rows["delivery_reviews"] = [{
        "id": "review", "organization_id": "org", "run_id": "run",
        "delivery_attempt_id": "attempt", "reviewer_id": "reviewer",
        "outcome": "INCONCLUSIVE",
    }]
    return rows


@pytest.mark.parametrize("outcome", sorted(verify_storage.REVIEW_OUTCOMES))
def test_review_relationships_preserve_unknown_and_organization(outcome):
    rows = reviewed_rows()
    rows["delivery_reviews"][0]["outcome"] = outcome
    assert verify_storage.validate_relationships(rows, []) == 5


@pytest.mark.parametrize("mutation", [
    "attempt", "run", "reviewer", "attempt_state", "run_state", "outcome", "organization",
])
def test_review_relationships_reject_inconsistent_review(mutation):
    rows = reviewed_rows()
    review = rows["delivery_reviews"][0]
    if mutation in {"attempt", "run", "reviewer"}:
        field = {"attempt": "delivery_attempt_id", "run": "run_id", "reviewer": "reviewer_id"}
        review[field[mutation]] = "missing"
    elif mutation == "attempt_state":
        rows["delivery_attempts"][0]["status"] = "COMMITTED"
    elif mutation == "run_state":
        rows["runs"][0]["status"] = "SUCCESS"
    elif mutation == "outcome":
        review["outcome"] = "COMMITTED"
    else:
        review["organization_id"] = "other-org"
    with pytest.raises(ValueError, match="UNKNOWN|organización"):
        verify_storage.validate_relationships(rows, [])


def v3_report(rows):
    return verify_storage.legacy_v3_report(
        rows, [], current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=2, verified_source_secrets=1, verified_delivery_secrets=1,
    )


def test_legacy_v3_preserves_delivery_payload_and_excludes_only_empty_review_table():
    rows = delivery_rows()
    rows["delivery_reviews"] = []
    report = v3_report(rows)
    assert report["schema_version"] == 3
    assert report["migration"] == "0008_data_delivery"
    assert report["verified_secrets"] == 2
    assert report["verified_delivery_secrets"] == 1
    assert "delivery_reviews" not in report["tables"]
    assert report["tables"]["delivery_attempts"]["attempt"] == verify_storage._canonical_hash(
        rows["delivery_attempts"][0]
    )
    before = deepcopy(report)
    rows["delivery_attempts"][0]["status"] = "UNKNOWN"
    with pytest.raises(ValueError, match="delivery_attempts"):
        verify_storage.compare(before, v3_report(rows))


def test_legacy_projections_reject_reviews_and_current_state_includes_them():
    rows = reviewed_rows()
    with pytest.raises(ValueError, match="revisiones vacías"):
        v3_report(rows)
    legacy = legacy_compatible_rows()
    legacy["delivery_reviews"] = rows["delivery_reviews"]
    with pytest.raises(ValueError, match="registros de Data Delivery"):
        verify_storage.legacy_v2_report(
            legacy, [], current_migration=verify_storage.CURRENT_MIGRATION,
            verified_artifacts=0, verified_source_secrets=0,
        )
    assert verify_storage._table_hashes(rows)["delivery_reviews"]["review"]


@pytest.mark.parametrize(("migration", "schema"), [
    ("0007_monitor_scheduling", 2), ("0008_data_delivery", 3), ("0009_delivery_reviews", 4),
])
def test_snapshot_labels_exact_running_revision_not_current_cli_version(monkeypatch, migration, schema):
    rows = {name: [] for name in verify_storage.FINGERPRINT_TABLES[migration]}
    rows["jobs"] = [{"id": "job", "status": "SUCCESS", "run_id": "run"}]
    if schema > 2:
        rows["jobs"][0]["lane"] = "DEFAULT"
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (migration, rows, [], 0, 0, 0))
    report = verify_storage.snapshot()
    assert report["schema_version"] == schema and report["migration"] == migration
    assert report["tables"]["jobs"]["job"] == verify_storage._canonical_hash(rows["jobs"][0])
    assert ("verified_delivery_secrets" in report) is (schema > 2)
    assert ("verified_source_secrets" in report) is (schema > 2)


@pytest.mark.parametrize("damage", ["unknown_revision", "missing_table", "unexpected_table"])
def test_snapshot_refuses_wrong_runtime_inventory_instead_of_mislabeling(monkeypatch, damage):
    migration = verify_storage.DELIVERY_BASELINE_MIGRATION
    rows = {name: [] for name in verify_storage.FINGERPRINT_TABLES[migration]}
    if damage == "unknown_revision":
        migration = "0010_unrecognized"
    elif damage == "missing_table":
        rows.pop("delivery_attempts")
    else:
        rows["delivery_reviews"] = []
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (migration, rows, [], 0, 0, 0))
    with pytest.raises(ValueError, match="revisión y el inventario"):
        verify_storage.snapshot()
