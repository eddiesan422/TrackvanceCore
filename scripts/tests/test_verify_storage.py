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
        rows, [], current_migration=verify_storage.IDENTITY_MIGRATION,
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
    ("0012_delivery_target_audit", 5),
    ("0015_sentinel_execution_identity", 6),
    ("0016_acquisition_diagnostics", 7),
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


def test_identity_projection_preserves_051_history_and_refuses_new_activity():
    rows = reviewed_rows()
    original = deepcopy(rows)
    rows.update({name: [] for name in verify_storage.IDENTITY_TABLES})
    rows["roles"] = [{"id": "role", "organization_id": "org"}]
    for user in rows["users"]:
        user.update(username="historical.user", role_id="role", first_name=None,
                    last_name=None, deleted=False, deleted_at=None,
                    must_change_password=False, temporary_password_expires_at=None)
    for attempt in rows["delivery_attempts"]:
        attempt["system_audit"] = {}
    report = verify_storage.legacy_v4_report(
        rows, [("users", "role_id", "roles", "id")],
        current_migration=verify_storage.IDENTITY_MIGRATION,
        verified_artifacts=0, verified_source_secrets=0, verified_delivery_secrets=0,
    )
    assert report["tables"] == verify_storage._table_hashes(original)
    assert report["schema_version"] == 4
    assert report["migration"] == "0009_delivery_reviews"
    rows["notification_deliveries"] = [{"id": "sent"}]
    with pytest.raises(ValueError, match="actividad nueva"):
        verify_storage.project_identity_upgrade(rows, [])


def test_identity_projection_rejects_changed_user_or_audited_attempt():
    rows = reviewed_rows()
    rows["users"][0]["deleted"] = True
    with pytest.raises(ValueError, match="cambios de identidad"):
        verify_storage.project_identity_upgrade(rows, [])
    rows["users"][0]["deleted"] = False
    rows["delivery_attempts"][0]["system_audit"] = {"enabled": True}
    with pytest.raises(ValueError, match="auditoría de entrega nueva"):
        verify_storage.project_identity_upgrade(rows, [])


def asynchronous_upgrade_rows():
    previous = {name: [] for name in verify_storage.FINGERPRINT_TABLES[verify_storage.IDENTITY_MIGRATION]}
    previous['users'] = [{'id': 'owner', 'organization_id': 'org'}]
    previous['monitor_schedules'] = [{'id': 'legacy', 'organization_id': 'org', 'enabled': True, 'version': 1},
        {'id': 'verified', 'organization_id': 'org', 'enabled': True, 'version': 1}]
    previous['monitor_schedule_versions'] = [
        {'id': 'old-revision', 'schedule_id': 'legacy', 'version': 1, 'organization_id': 'org', 'actor_id': 'SYSTEM'},
        {'id': 'verified-revision', 'schedule_id': 'verified', 'version': 1, 'organization_id': 'org', 'actor_id': 'owner'}]
    upgraded = deepcopy(previous)
    upgraded.update({name: [] for name in verify_storage.ASYNC_TABLES})
    upgraded['monitor_schedules'][0].update(enabled=False, legacy_enabled_before_identity=True)
    upgraded['monitor_schedules'][1]['legacy_enabled_before_identity'] = None
    upgraded['monitor_schedule_versions'][0]['responsible_user_id'] = None
    upgraded['monitor_schedule_versions'][1]['responsible_user_id'] = 'owner'
    return previous, upgraded


def test_v5_projection_preserves_all_historical_hashes_and_only_reverses_explicit_pause():
    previous, upgraded = asynchronous_upgrade_rows()
    report = verify_storage.legacy_v5_report(upgraded, [], current_migration=verify_storage.CURRENT_MIGRATION,
        verified_artifacts=0, verified_source_secrets=0, verified_delivery_secrets=0)
    assert report['tables'] == verify_storage._table_hashes(previous)
    assert report['schema_version'] == 5 and report['migration'] == verify_storage.IDENTITY_MIGRATION
    assert upgraded['monitor_schedules'][0]['enabled'] is False


@pytest.mark.parametrize('damage', ['new_notification', 'unknown_table', 'assigned_owner', 'missing_pause', 'enabled_legacy'])
def test_v5_projection_never_discards_new_activity_or_non_deterministic_defaults(damage):
    _, upgraded = asynchronous_upgrade_rows()
    if damage == 'new_notification':
        upgraded['internal_notifications'] = [{'id': 'personal-event'}]
    elif damage == 'unknown_table':
        upgraded['plugin_history'] = []
    elif damage == 'assigned_owner':
        upgraded['monitor_schedule_versions'][0]['responsible_user_id'] = 'owner'
    elif damage == 'missing_pause':
        upgraded['monitor_schedules'][0]['legacy_enabled_before_identity'] = None
    else:
        upgraded['monitor_schedules'][0]['enabled'] = True
    with pytest.raises(ValueError, match='actividad nueva|inventario completo|asignación nueva|pausa'):
        verify_storage.project_async_upgrade(upgraded, [])


@pytest.mark.parametrize('identities', [(None, None), ('run', 'acquisition')])
def test_job_requires_exactly_one_execution_identity(identities):
    rows = {'jobs': [{'id': 'job', 'lane': 'ACQUISITION', 'run_id': identities[0], 'acquisition_id': identities[1]}]}
    with pytest.raises(ValueError, match='exactamente un Run'):
        verify_storage.validate_relationships(rows, [])


def corrections_upgrade_rows():
    previous = {name: [] for name in verify_storage.FINGERPRINT_TABLES[verify_storage.PRE_CORRECTIONS_MIGRATION]}
    previous["users"] = [{"id": "owner", "organization_id": "org", "name": "Historical user", "password_hash": "unaltered"}]
    previous["datasets"] = [{"id": "dataset", "organization_id": "org", "domain": "Área histórica"}]
    previous["artifacts"] = [{"id": "original", "organization_id": "org", "sha256": "a" * 64,
                              "path": "artifacts/historical.csv", "size_bytes": 12345}]
    previous["acquisition_runs"] = [{"id": "acquisition", "organization_id": "org", "dataset_id": "dataset",
        "initiated_by_id": "owner", "status": "FAILED", "stage": "READING", "output_version_id": None,
        "error_code": "HISTORICAL_ERROR", "error_message": "Mensaje anterior exacto",
        "source_snapshot": {"row_numbering": "WORKSHEET_ROW", "sheet_name": "Histórico"},
        "reader_options": {"header_row": 2}, "effective_limits": {"batch_bytes": 8388608},
        "processed_rows": 17, "processed_bytes": 12345}]
    previous["jobs"] = [{"id": "job", "organization_id": "org", "run_id": None,
                         "acquisition_id": "acquisition", "lane": "ACQUISITION", "status": "FAILED"}]
    previous["outbox_events"] = [{"id": "event", "organization_id": "org", "payload": {"status": "FAILED", "error_code": "HISTORICAL_ERROR"}}]
    previous["internal_notifications"] = [{"id": "notice", "organization_id": "org", "event_id": "event",
        "recipient_user_id": "owner", "description": "Descripción anterior exacta", "read_at": "2026-09-11T12:34:56Z"}]
    upgraded = deepcopy(previous)
    upgraded["acquisition_runs"][0].update(error_details=None, error_reference=None)
    fks = [("acquisition_runs", "dataset_id", "datasets", "id"),
           ("acquisition_runs", "initiated_by_id", "users", "id"),
           ("jobs", "acquisition_id", "acquisition_runs", "id"),
           ("internal_notifications", "event_id", "outbox_events", "id"),
           ("internal_notifications", "recipient_user_id", "users", "id")]
    return previous, upgraded, fks


def corrections_report(rows, fks, migration=verify_storage.CURRENT_MIGRATION):
    return verify_storage.legacy_v6_report(rows, fks, current_migration=migration,
        verified_artifacts=1, verified_source_secrets=0, verified_delivery_secrets=0)


def test_v6_projection_retains_every_historical_row_value_and_aggregate():
    previous, upgraded, fks = corrections_upgrade_rows()
    original = deepcopy(upgraded)
    report = corrections_report(upgraded, fks)
    assert report["schema_version"] == 6 and report["migration"] == verify_storage.PRE_CORRECTIONS_MIGRATION
    assert len(report["tables"]) == 42
    assert report["tables"] == verify_storage._table_hashes(previous)
    assert report["validated_relationships"] == verify_storage.validate_relationships(previous, fks)
    assert report["verified_artifacts"] == 1 and report["verified_secrets"] == 0
    assert upgraded == original


@pytest.mark.parametrize("damage", ["details", "empty_details", "reference", "empty_reference",
                                   "missing_column", "unknown_table", "missing_table", "wrong_revision"])
def test_v6_projection_rejects_any_nonnull_new_diagnostic_or_incomplete_schema(damage):
    _, upgraded, fks = corrections_upgrade_rows()
    migration = verify_storage.CURRENT_MIGRATION
    if damage == "details":
        upgraded["acquisition_runs"][0]["error_details"] = {"observed": 400000}
    elif damage == "empty_details":
        upgraded["acquisition_runs"][0]["error_details"] = {}
    elif damage == "reference":
        upgraded["acquisition_runs"][0]["error_reference"] = "correlation"
    elif damage == "empty_reference":
        upgraded["acquisition_runs"][0]["error_reference"] = ""
    elif damage == "missing_column":
        del upgraded["acquisition_runs"][0]["error_details"]
    elif damage == "unknown_table":
        upgraded["plugin_history"] = []
    elif damage == "missing_table":
        del upgraded["outbox_events"]
    else:
        migration = verify_storage.PRE_CORRECTIONS_MIGRATION
    with pytest.raises(ValueError, match="NULL|inventario completo|0016"):
        corrections_report(upgraded, fks, migration)


@pytest.mark.parametrize("damage", ["old_error", "read_at", "description", "area", "options", "numbering",
                                   "limits", "artifact_hash", "unknown_column", "extra_row"])
def test_v6_projection_never_hides_changes_to_historical_data(damage):
    _, upgraded, fks = corrections_upgrade_rows()
    expected = corrections_report(upgraded, fks)
    if damage == "old_error":
        upgraded["acquisition_runs"][0]["error_message"] = "Reinterpreted historical message"
    elif damage in {"read_at", "description"}:
        upgraded["internal_notifications"][0][damage] = None if damage == "read_at" else "Rewritten"
    elif damage == "area":
        upgraded["datasets"][0]["domain"] = "Changed"
    elif damage == "options":
        upgraded["acquisition_runs"][0]["reader_options"]["header_row"] = 1
    elif damage == "numbering":
        upgraded["acquisition_runs"][0]["source_snapshot"]["row_numbering"] = "RECORD_NUMBER"
    elif damage == "limits":
        upgraded["acquisition_runs"][0]["effective_limits"]["batch_bytes"] = 1
    elif damage == "artifact_hash":
        upgraded["artifacts"][0]["sha256"] = "b" * 64
    elif damage == "unknown_column":
        upgraded["acquisition_runs"][0]["future_column"] = None
    else:
        upgraded["outbox_events"].append({"id": "new-event", "organization_id": "org", "payload": {}})
    with pytest.raises(ValueError, match="persistencia cambió"):
        verify_storage.compare(expected, corrections_report(upgraded, fks))


@pytest.mark.parametrize("schema", [2, 3, 4, 5])
@pytest.mark.parametrize("migration", [verify_storage.PRE_CORRECTIONS_MIGRATION, verify_storage.CURRENT_MIGRATION])
def test_older_projections_remain_strict_across_both_async_heads(schema, migration):
    _, upgraded = asynchronous_upgrade_rows()
    kwargs = {"current_migration": migration, "verified_artifacts": 0, "verified_source_secrets": 0}
    if schema > 2:
        kwargs["verified_delivery_secrets"] = 0
    report = getattr(verify_storage, f"legacy_v{schema}_report")(upgraded, [], **kwargs)
    assert report["schema_version"] == schema
    assert len(report["tables"]) == {2: 21, 3: 24, 4: 25, 5: 31}[schema]


def test_cli_v6_projection_uses_checked_snapshot_inputs(monkeypatch, capsys):
    _, upgraded, fks = corrections_upgrade_rows()
    monkeypatch.setattr(verify_storage, "_snapshot_inputs", lambda: (
        verify_storage.CURRENT_MIGRATION, upgraded, fks, 1, 0, 0))
    monkeypatch.setattr(verify_storage.sys, "argv", ["verify_storage", "snapshot-legacy-v6"])
    assert verify_storage.main() == 0
    import json

    assert json.loads(capsys.readouterr().out) == corrections_report(upgraded, fks)
