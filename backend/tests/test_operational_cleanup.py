"""Selective cleanup on disposable metadata/files, including rollback and drift."""
import hashlib
import json
import tarfile

import pytest
from sqlalchemy import select, text

from trackvance.automation_models import (
    DeliveryAutomation,
    DeliveryAutomationVersion,
    DeliveryOccurrence,
    DeliveryTargetGuard,
    EventConsumption,
    InternalNotification,
    OutboxEvent,
)
from trackvance.db import utcnow
from trackvance.events import append_event, consume_notification, consume_once
from trackvance.governance_models import GovernancePerson, MacroDomain
from trackvance.models import (
    AuditEvent,
    Configuration,
    Dataset,
    DatasetVersion,
    ExternalConnection,
    Job,
    MonitorOccurrence,
    MonitorSchedule,
    MonitorScheduleVersion,
    Run,
    User,
)
from trackvance.operational_cleanup import apply_plan, create_plan, digest, recover_quarantine
from trackvance.operations_common import OperationError
from trackvance.report_models import (
    ReportContext,
)


@pytest.fixture
def population_fixture(authenticated, database, tmp_path):
    storage = tmp_path / "store"
    storage.mkdir()
    paths = []
    for name in ("original.csv", "canonical.parquet", "other.csv"):
        path = storage / name
        path.write_bytes(("fixture-" + name).encode())
        paths.append(path)
    with database() as db:
        user = db.get(User, "test-user")
        org = user.organization_id
        db.add_all([Dataset(id="test-dataset", name="Confirmed test", organization_id=org),
            Dataset(id="protected-dataset", name="Keep operational", organization_id=org),
            GovernancePerson(id="person", name="Contact", normalized_name="contact", organization_id=org),
            MacroDomain(id="macro", name="Domain", normalized_name="domain", organization_id=org),
            ExternalConnection(id="connection", name="Offline private source", source_type="POSTGRESQL", organization_id=org)])
        db.flush()
        db.get(Dataset, "test-dataset").business_owner_person_id = "person"
        db.add(DatasetVersion(id="test-version", dataset_id="test-dataset", version=1, filename="original.csv",
            sha256=hashlib.sha256(paths[0].read_bytes()).hexdigest(), schema_hash="b" * 64, size_bytes=1,
            row_count=1, column_count=1, original_path=str(paths[0]), canonical_path=str(paths[1]), schema_json=[], profile={}, organization_id=org))
        db.add(Configuration(id="test-config", name="Confirmed configuration", module="sentinel", dataset_id="test-dataset", config={}, organization_id=org))
        db.flush()
        db.add_all([Run(id="test-run", name="Terminal test", module="sentinel", config_id="test-config",
            dataset_version_id="test-version", initiated_by="Test User", status="SUCCESS", organization_id=org),
            MonitorSchedule(id="test-schedule", monitor_id="test-config", enabled=False, next_run_at=utcnow(), organization_id=org),
            DeliveryAutomation(id="test-auto", name="Disabled test auto", responsible_user_id=user.id, enabled=False, organization_id=org)])
        db.flush()
        db.add(DeliveryAutomationVersion(id="test-auto-v1", automation_id="test-auto", version=1,
            configuration_id="test-config", responsible_user_id=user.id, actor_id=user.id, enabled=False, settings={}, organization_id=org))
        db.add(Job(id="test-job", run_id="test-run", status="SUCCESS", organization_id=org))
        db.add(AuditEvent(id="protected-audit", event_type="SYNTHETIC", actor="Test User", subject_type="run", subject_id="test-run", message="History retained", organization_id=org))
        db.commit()
    return database, storage, paths, org


def plan_and_backup(fixture, tmp_path):
    database, storage, _paths, org = fixture
    with database() as db:
        plan = json.loads(json.dumps(create_plan(db, "trackvance-cleanup-test", org, ["test-dataset"], storage)))
    backup = tmp_path / "verified-backup"
    backup.mkdir()
    (backup / "state.json").write_text(json.dumps({"tables": plan["scope"]["state_tables"]}))
    manifest = {"source_project": "trackvance-cleanup-test", "migration": plan["scope"]["migration"], "consistency": "quiesced"}
    (backup / "backup-manifest.json").write_text(json.dumps(manifest))
    (backup / "volumes").mkdir()
    with tarfile.open(backup / "volumes" / "trackvance_data.tar.gz", "w:gz") as archive:
        for entry in plan["scope"]["files"]:
            archive.add(storage / entry["path"], arcname=entry["path"])
    receipt = {"schema_version": 1, "status": "STOPPED_VERIFIED", "target_project": "trackvance-isolated-restore",
        "source_manifest_sha256": hashlib.sha256((backup / "backup-manifest.json").read_bytes()).hexdigest(),
        "verified_state_sha256": hashlib.sha256((backup / "state.json").read_bytes()).hexdigest()}
    (tmp_path / "restore-receipt.json").write_text(json.dumps(receipt))
    return plan, backup, lambda _: manifest


def apply(database, plan, storage, backup, verifier, quarantine, **kwargs):
    receipt = kwargs.pop("restore_receipt", None)
    if receipt is None:
        receipt = json.loads((backup.parent / "restore-receipt.json").read_text())
    with database() as db:
        return apply_plan(db, plan, storage, quarantine, backup, verifier, project="trackvance-cleanup-test",
            actor_id="test-user", restore_receipt=receipt, verify_quiescence=lambda: None, **kwargs)


def recovery_journal(plan, storage):
    journal = {"schema_version": 1, "project": "trackvance-cleanup-test", "storage": str(storage.resolve()),
        "organization_id": plan["scope"]["organization_id"], "nonce": plan["nonce"],
        "migration": plan["scope"]["migration"], "plan_sha256": plan["plan_sha256"], "files": plan["scope"]["files"]}
    return {**journal, "journal_sha256": digest(journal)}


def test_dry_plan_and_apply_delete_only_confirmed_dependencies_and_preserve_catalog_access(population_fixture, tmp_path):
    database, storage, paths, _ = population_fixture
    plan, backup, verifier = plan_and_backup(population_fixture, tmp_path)
    assert all(path.exists() for path in paths)
    assert plan["scope"]["delete"]["datasets"] == ["test-dataset"]
    assert plan["scope"]["delete"]["configurations"] == ["test-config"]
    assert plan["scope"]["delete"]["monitor_schedules"] == ["test-schedule"]
    assert plan["scope"]["delete"]["delivery_automations"] == ["test-auto"]
    for table in ("users", "roles", "governance_people", "macro_domains", "external_connections", "audit_events", "delivery_target_guards"):
        assert plan["scope"]["delete"][table] == []
    receipt = apply(database, plan, storage, backup, verifier, tmp_path / "quarantine")
    assert receipt["status"] == "METADATA_COMMITTED_FILES_QUARANTINED" and receipt["quarantined_files"] == 2
    assert not paths[0].exists() and paths[2].exists() and backup.exists()
    with database() as db:
        assert db.get(Dataset, "test-dataset") is None and db.get(Dataset, "protected-dataset")
        assert db.get(User, "test-user") and db.get(GovernancePerson, "person") and db.get(AuditEvent, "protected-audit")
        assert db.scalar(select(AuditEvent).where(AuditEvent.event_type == "OPERATIONAL_TEST_DATA_REMOVED"))
    with database() as db:
        assert recover_quarantine(db, tmp_path / "quarantine", storage, project="trackvance-cleanup-test", verify_quiescence=lambda: None)["files_restored"] == 0


def test_fault_before_commit_rolls_back_metadata_and_restores_all_moved_files(population_fixture, tmp_path):
    database, storage, paths, _ = population_fixture
    plan, backup, verifier = plan_and_backup(population_fixture, tmp_path)
    def fault():
        raise RuntimeError("synthetic commit failure")
    with pytest.raises(RuntimeError, match="synthetic"):
        apply(database, plan, storage, backup, verifier, tmp_path / "quarantine", before_commit=fault)
    assert all(path.exists() for path in paths)
    with database() as db:
        assert db.get(Dataset, "test-dataset") and db.get(Configuration, "test-config")
        assert not db.scalar(select(AuditEvent).where(AuditEvent.event_type == "OPERATIONAL_TEST_DATA_REMOVED"))
    assert json.loads((tmp_path / "quarantine" / "rollback.json").read_text())["status"] == "ROLLED_BACK"


@pytest.mark.parametrize("change", ["protected-row", "file", "schema", "expired", "backup"])
def test_apply_refuses_drift_or_missing_backup_before_deletion(population_fixture, tmp_path, change):
    database, storage, paths, _ = population_fixture
    plan, backup, verifier = plan_and_backup(population_fixture, tmp_path)
    code = "CLEANUP_PLAN_DRIFT"
    if change == "protected-row":
        with database() as db:
            db.get(User, "test-user").name = "Changed after plan"
            db.commit()
    elif change == "file":
        paths[0].write_bytes(b"changed")
    elif change == "schema":
        with database() as db:
            db.execute(text("ALTER TABLE datasets ADD COLUMN drift TEXT"))
            db.commit()
        code = "CLEANUP_SCHEMA_DRIFT"
    elif change == "expired":
        plan["expires_at"] = "2000-01-01T00:00:00+00:00"
        plan["plan_sha256"] = digest({key: value for key, value in plan.items() if key != "plan_sha256"})
        code = "CLEANUP_PLAN_EXPIRED"
    else:
        (backup / "state.json").write_text(json.dumps({"tables": {}}))
        code = "CLEANUP_BACKUP_MISMATCH"
    with pytest.raises(OperationError) as caught:
        apply(database, plan, storage, backup, verifier, tmp_path / "quarantine")
    assert caught.value.code == code
    assert all(path.exists() for path in paths)
    with database() as db:
        assert db.get(Dataset, "test-dataset")


@pytest.mark.parametrize("consumer", ["unknown", "external-barrier", "mixed-report", "mixed-configuration"])
def test_uncertain_external_or_shared_consumers_protect_entire_dataset_branch(population_fixture, consumer):
    database, storage, paths, org = population_fixture
    with database() as db:
        if consumer == "unknown":
            db.get(Run, "test-run").status = "UNKNOWN"
        elif consumer == "external-barrier":
            db.add(DeliveryTargetGuard(target_fingerprint="a" * 64, unknown_run_id="test-run", organization_id=org))
        elif consumer == "mixed-configuration":
            db.add(Configuration(name="Keep mixed", module="recon", dataset_id="protected-dataset", target_dataset_id="test-dataset", config={}, organization_id=org))
        else:
            db.add(ReportContext(user_id="test-user", expires_at=utcnow(), snapshot={"sources": [
                {"input_dataset_id": "test-dataset"}, {"input_dataset_id": "protected-dataset"}]}, integrity_hash="a" * 64, organization_id=org))
        db.commit()
    with database() as db:
        plan = create_plan(db, "trackvance-cleanup-test", org, ["test-dataset"], storage)
    assert all(not ids for ids in plan["scope"]["delete"].values()), {name: ids for name, ids in plan["scope"]["delete"].items() if ids}
    assert plan["scope"]["exceptions"] and all(path.exists() for path in paths)


def test_abrupt_precommit_file_move_has_controlled_recovery(population_fixture, tmp_path):
    database, storage, paths, _ = population_fixture
    plan, _, _ = plan_and_backup(population_fixture, tmp_path)
    quarantine = tmp_path / "interrupted"
    for entry in plan["scope"]["files"]:
        target = quarantine / "files" / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        (storage / entry["path"]).rename(target)
    journal = recovery_journal(plan, storage)
    (quarantine / "recovery.json").write_text(json.dumps(journal))
    with database() as db:
        result = recover_quarantine(db, quarantine, storage, project="trackvance-cleanup-test", verify_quiescence=lambda: None)
    assert result["files_restored"] == 2 and all(path.exists() for path in paths)


def test_cross_mount_quarantine_copy_is_fsynced_and_rollback_still_restores(population_fixture, tmp_path, monkeypatch):
    import errno
    from pathlib import Path
    database, storage, paths, _ = population_fixture
    plan, backup, verifier = plan_and_backup(population_fixture, tmp_path)
    def cross_mount(_source, _target):
        raise OSError(errno.EXDEV, "synthetic separate mount")
    monkeypatch.setattr(Path, "rename", cross_mount)
    def fault():
        raise RuntimeError("synthetic rollback across mounts")
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        apply(database, plan, storage, backup, verifier, tmp_path / "cross-mount", before_commit=fault)
    assert all(path.exists() for path in paths)
    assert not list((tmp_path / "cross-mount" / "files").glob("*"))
    with database() as db:
        assert db.get(Dataset, "test-dataset")


def test_relative_and_absolute_shared_file_paths_are_protected(population_fixture):
    database, storage, paths, org = population_fixture
    with database() as db:
        db.add(DatasetVersion(id="protected-version", dataset_id="protected-dataset", version=1,
            filename="shared.csv", sha256="a" * 64, schema_hash="b" * 64, size_bytes=1, row_count=1, column_count=1,
            original_path=paths[0].name, canonical_path=str(paths[2]), schema_json=[], profile={}, organization_id=org))
        db.commit()
    with database() as db, pytest.raises(OperationError) as caught:
        create_plan(db, "trackvance-cleanup-test", org, ["test-dataset"], storage)
    assert caught.value.code == "CLEANUP_FILE_SHARED" and paths[0].exists()


def test_terminal_reports_and_descriptor_parts_are_selected_with_exact_source_ids(population_fixture, tmp_path):
    from trackvance.models import Artifact, ArtifactLink
    from trackvance.report_models import ReportDefinition, ReportExecution, ReportRevision
    database, storage, paths, org = population_fixture
    part = storage / "canonical.part-1.parquet"
    part.write_bytes(b"synthetic part")
    with database() as db:
        db.add_all([Artifact(id="canonical", kind="CANONICAL_PARQUET", name="canonical", path=str(paths[1]),
            sha256="a" * 64, size_bytes=1, media_type="application/json", organization_id=org),
            Artifact(id="part", kind="CANONICAL_PART", name="part", path=str(part), sha256="a" * 64,
                size_bytes=1, media_type="application/vnd.apache.parquet", organization_id=org),
            ReportDefinition(id="definition", name="Synthetic source-only report", owner_user_id="test-user", organization_id=org)])
        db.flush()
        db.get(DatasetVersion, "test-version").canonical_artifact_id = "canonical"
        db.add(ArtifactLink(relation="DATASET_PART", source_type="ARTIFACT", source_id="canonical",
            target_type="ARTIFACT", target_id="part", organization_id=org))
        draft = {"sources": [{"input_dataset_id": "test-dataset", "contract_revision_ids": ["test-config"]}]}
        db.add(ReportRevision(id="revision", definition_id="definition", version=1, draft=draft,
            query_hash="a" * 64, created_by_user_id="test-user", organization_id=org))
        db.flush()
        db.add(ReportContext(id="context", user_id="test-user", revision_id="revision", expires_at=utcnow(),
            snapshot=draft, integrity_hash="a" * 64, organization_id=org))
        db.flush()
        db.add(ReportExecution(id="execution", context_id="context", user_id="test-user", profile="PREVIEW", status="SUCCESS", organization_id=org))
        db.commit()
    with database() as db:
        plan = create_plan(db, "trackvance-cleanup-test", org, ["test-dataset"], storage)
    for table, identity in (("report_definitions", "definition"), ("report_revisions", "revision"),
            ("report_contexts", "context"), ("report_executions", "execution")):
        assert plan["scope"]["delete"][table] == [identity]
    assert plan["scope"]["delete"]["artifacts"] == ["canonical", "part"]
    assert {entry["path"] for entry in plan["scope"]["files"]} == {paths[0].name, paths[1].name, part.name}
    assert all(path.exists() for path in [*paths, part])


def test_partial_copy_with_intact_original_recovers_without_overwriting_live_file(population_fixture, tmp_path):
    database, storage, paths, _ = population_fixture
    plan, _, _ = plan_and_backup(population_fixture, tmp_path)
    quarantine = tmp_path / "partial-copy"
    target = quarantine / "files" / plan["scope"]["files"][0]["path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unfinished copy")
    journal = recovery_journal(plan, storage)
    (quarantine / "recovery.json").write_text(json.dumps(journal))
    with database() as db:
        result = recover_quarantine(db, quarantine, storage, project="trackvance-cleanup-test", verify_quiescence=lambda: None)
    assert result["files_restored"] == 0 and result["retained_copy_files"] == 1
    assert target.read_bytes() == b"unfinished copy" and all(path.exists() for path in paths)


@pytest.mark.parametrize("kind", ["MONITOR_OCCURRENCE", "DELIVERY_OCCURRENCE"])
def test_occurrence_cleanup_removes_pending_events_and_notifications_without_repopulation(population_fixture, tmp_path, kind):
    database, storage, _, org = population_fixture
    with database() as db:
        if kind == "MONITOR_OCCURRENCE":
            db.add(MonitorScheduleVersion(id="schedule-v1", schedule_id="test-schedule", version=1,
                interval_seconds=60, enabled=False, starts_at=utcnow(), actor_id="test-user", organization_id=org))
            db.flush()
            db.add(MonitorOccurrence(id="occurrence", schedule_id="test-schedule", schedule_version_id="schedule-v1",
                monitor_id="test-config", planned_at=utcnow(), dispatched_at=utcnow(), status="BLOCKED", organization_id=org))
            table = "monitor_occurrences"
        else:
            db.add(DeliveryOccurrence(id="occurrence", automation_id="test-auto", automation_version_id="test-auto-v1",
                trigger_key="review", origin="SCHEDULED", planned_at=utcnow(), dispatched_at=utcnow(),
                status="BLOCKED", organization_id=org))
            table = "delivery_occurrences"
        event = append_event(db, organization_id=org, dedupe_key="occurrence-event", event_type="DELIVERY_AUTOMATION_BLOCKED",
            aggregate_type=kind, aggregate_id="occurrence", module="DELIVERY",
            payload={"recipient_user_id": "test-user", "status": "BLOCKED", "origin": "SCHEDULED",
                     "automation_id": "test-auto", "monitor_id": "test-config"})
        db.flush()
        consume_notification(db, event)
        db.commit()
    plan, backup, verifier = plan_and_backup(population_fixture, tmp_path)
    assert plan["scope"]["delete"][table] == ["occurrence"]
    assert len(plan["scope"]["delete"]["outbox_events"]) == 1
    assert len(plan["scope"]["delete"]["event_consumptions"]) == 2
    assert len(plan["scope"]["delete"]["internal_notifications"]) == 1
    apply(database, plan, storage, backup, verifier, tmp_path / "quarantine")
    with database() as db:
        for model in (OutboxEvent, EventConsumption, InternalNotification):
            assert db.scalar(select(model)) is None
    assert not consume_once("NOTIFICATIONS") and not consume_once("CHAINING")


def test_unknown_polymorphic_consumer_preserves_matching_operational_branch(population_fixture):
    database, storage, paths, org = population_fixture
    with database() as db:
        append_event(db, organization_id=org, dedupe_key="uncertain", event_type="UNKNOWN_SOURCE_EVENT",
            aggregate_type="UNRECOGNIZED", aggregate_id="test-dataset", module="UNKNOWN", payload={})
        db.commit()
    with database() as db:
        plan = create_plan(db, "trackvance-cleanup-test", org, ["test-dataset"], storage)
    assert all(not ids for ids in plan["scope"]["delete"].values())
    assert any(item["consumer_table"] == "outbox_events" for item in plan["scope"]["exceptions"])
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize("reference", ["logical-lineage", "frozen-report", "polymorphic-event"])
def test_broken_application_references_prevent_a_plan(population_fixture, reference):
    database, storage, paths, org = population_fixture
    with database() as db:
        if reference == "logical-lineage":
            db.get(DatasetVersion, "test-version").parent_version_id = "missing-version"
        elif reference == "frozen-report":
            db.add(ReportContext(user_id="test-user", expires_at=utcnow(), integrity_hash="a" * 64,
                snapshot={"sources": [{"input_dataset_id": "test-dataset", "output_version_id": "missing-version"}]},
                organization_id=org))
        else:
            append_event(db, organization_id=org, dedupe_key="missing", event_type="RUN_TERMINAL",
                aggregate_type="RUN", aggregate_id="missing-run", module="sentinel", payload={})
        db.commit()
    with database() as db, pytest.raises(OperationError) as caught:
        create_plan(db, "trackvance-cleanup-test", org, ["test-dataset"], storage)
    assert caught.value.code == "CLEANUP_REFERENTIAL_DRIFT" and all(path.exists() for path in paths)


def test_corrupt_quarantine_is_retained_and_never_restored_to_live_store(population_fixture, tmp_path):
    database, storage, paths, _ = population_fixture
    plan, backup, verifier = plan_and_backup(population_fixture, tmp_path)
    quarantine = tmp_path / "quarantine"
    def fault():
        (quarantine / "files" / paths[0].name).write_bytes(b"corrupted after quarantine")
        raise RuntimeError("fixture file failure")
    with pytest.raises(OperationError) as caught:
        apply(database, plan, storage, backup, verifier, quarantine, before_commit=fault)
    assert caught.value.code == "CLEANUP_RECOVERY_REQUIRED"
    assert not paths[0].exists() and paths[1].exists() and paths[2].exists()
    assert (quarantine / "files" / paths[0].name).read_bytes() == b"corrupted after quarantine"
    receipt = json.loads((quarantine / "rollback.json").read_text())
    assert receipt["status"] == "RECOVERY_REQUIRED" and receipt["conflict_files"] == [paths[0].name]
    with database() as db:
        assert db.get(Dataset, "test-dataset") and db.get(Configuration, "test-config")
    with database() as db, pytest.raises(OperationError) as caught:
        recover_quarantine(db, quarantine, storage, project="trackvance-cleanup-test", verify_quiescence=lambda: None)
    assert caught.value.code == "CLEANUP_RECOVERY_CONFLICT" and not paths[0].exists()
    assert backup.exists()


@pytest.mark.parametrize("drift", ["journal", "commit-marker"])
def test_recovery_refuses_journal_or_commit_marker_drift(population_fixture, tmp_path, drift):
    database, storage, paths, org = population_fixture
    plan, _, _ = plan_and_backup(population_fixture, tmp_path)
    quarantine = tmp_path / "interrupted"
    quarantine.mkdir()
    journal = recovery_journal(plan, storage)
    if drift == "journal":
        journal["files"][0]["sha256"] = "f" * 64
    else:
        with database() as db:
            db.add(AuditEvent(event_type="OPERATIONAL_TEST_DATA_REMOVED", actor="Test User", organization_id=org,
                subject_type="operational_cleanup", subject_id=plan["nonce"], message="Wrong marker",
                metadata_json={"plan_sha256": "b" * 64}))
            db.commit()
    (quarantine / "recovery.json").write_text(json.dumps(journal))
    with database() as db, pytest.raises(OperationError) as caught:
        recover_quarantine(db, quarantine, storage, project="trackvance-cleanup-test", verify_quiescence=lambda: None)
    assert caught.value.code == "CLEANUP_RECOVERY_INVALID" and all(path.exists() for path in paths)


@pytest.mark.parametrize("drift", ["missing-file", "different-bytes", "unverified-restore", "manifest-link", "state-link"])
def test_apply_requires_exact_backup_files_and_stopped_restore_of_same_backup(population_fixture, tmp_path, drift):
    import io
    database, storage, paths, _ = population_fixture
    plan, backup, verifier = plan_and_backup(population_fixture, tmp_path)
    receipt = json.loads((tmp_path / "restore-receipt.json").read_text())
    code = "CLEANUP_RESTORE_UNVERIFIED"
    if drift in {"missing-file", "different-bytes"}:
        code = "CLEANUP_BACKUP_FILES_MISMATCH"
        with tarfile.open(backup / "volumes" / "trackvance_data.tar.gz", "w:gz") as archive:
            for entry in plan["scope"]["files"]:
                if entry["path"] == paths[0].name:
                    if drift == "missing-file":
                        continue
                    payload = b"x" * entry["bytes"]
                    member = tarfile.TarInfo(entry["path"])
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                else:
                    archive.add(storage / entry["path"], arcname=entry["path"])
    elif drift == "unverified-restore":
        receipt["status"] = "RUNNING_VERIFIED"
    elif drift == "manifest-link":
        receipt["source_manifest_sha256"] = "b" * 64
    else:
        receipt["verified_state_sha256"] = "b" * 64
    with pytest.raises(OperationError) as caught:
        apply(database, plan, storage, backup, verifier, tmp_path / "quarantine", restore_receipt=receipt)
    assert caught.value.code == code and all(path.exists() for path in paths)
    assert not (tmp_path / "quarantine").exists()


def test_backup_ancestor_of_live_store_is_rejected_before_any_change(population_fixture, tmp_path):
    database, storage, paths, _ = population_fixture
    plan, _, verifier = plan_and_backup(population_fixture, tmp_path)
    with pytest.raises(OperationError) as caught:
        apply(database, plan, storage, tmp_path, verifier, tmp_path.parent / "outside-quarantine",
            restore_receipt={})
    assert caught.value.code == "CLEANUP_BACKUP_PROTECTED" and all(path.exists() for path in paths)
