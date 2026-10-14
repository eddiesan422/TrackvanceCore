"""Durable Intake event ordering, frozen revisions, calendar fences and safe deletion."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_governance_catalog import (  # noqa: F401 -- reusable real approval fixture
    approved,
    classify,
)

from trackvance.automation import AutomationError, AutomationSettings
from trackvance.automation_models import DeliveryAutomation, DeliveryAutomationVersion, OutboxEvent
from trackvance.db import iso, utcnow
from trackvance.intake_automation import (
    IntakeScheduleSettings,
    consume_dataset_event,
    delete_contract,
    deletion_impact,
    dispatch_due,
    save_schedule,
)
from trackvance.intake_automation_models import (
    IntakeContractState,
    IntakeOccurrence,
    IntakeScheduleVersion,
)
from trackvance.models import Configuration, Dataset, DatasetVersion, ExceptionCase, Job, Run, User
from trackvance.services import create_version, enqueue, execute_run


def setup_contract(db, tmp_path):
    user = db.get(User, "test-user")
    dataset = Dataset(name="Intake lifecycle", organization_id=user.organization_id)
    db.add(dataset)
    db.flush()
    version = publish(db, dataset, tmp_path, "001")
    contract = Configuration(name="Intake lifecycle contract", module="intake", dataset_id=dataset.id,
        organization_id=user.organization_id, config={"required_columns": ["id"], "max_error_rate": 0})
    db.add(contract)
    db.flush()
    return user, dataset, version, contract


def publish(db, dataset, tmp_path, identifier):
    source = tmp_path / f"intake-{identifier}.csv"
    source.write_text(f"id,amount\n{identifier},1.25\n", encoding="utf-8")
    return create_version(db, dataset, source, source.name)


def event_for(db, version):
    return db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == version.id,
        OutboxEvent.event_type == "DATASET_VERSION_PUBLISHED"))


def test_event_replay_v2_v3_frozen_revision_pending_fifo_and_no_output_cycle(database, tmp_path):
    with database() as db:
        user, dataset, v1, first = setup_contract(db, tmp_path)
        settings = IntakeScheduleSettings(starts_at=utcnow() - timedelta(seconds=1))
        schedule = save_schedule(db, first, user, settings, enabled=True)
        v2 = publish(db, dataset, tmp_path, "002")
        event_v2 = event_for(db, v2)
        second = Configuration(name=first.name, module="intake", dataset_id=dataset.id,
            organization_id=user.organization_id, version=2, previous_version_id=first.id,
            config={"required_columns": ["id"], "positive_columns": ["amount"], "max_error_rate": 0})
        db.add(second)
        db.flush()
        save_schedule(db, second, user, settings, enabled=True, expected_version=1)
        v3 = publish(db, dataset, tmp_path, "003")
        # Events still pending in the outbox use the revision in force at
        # publication, even when the consumer runs after the schedule edit.
        consume_dataset_event(db, event_v2)
        consume_dataset_event(db, event_for(db, v3))
        consume_dataset_event(db, event_v2)
        db.commit()
        occurrences = db.scalars(select(IntakeOccurrence).order_by(IntakeOccurrence.planned_at)).all()
        assert len(occurrences) == 2
        runs = [db.get(Run, item.run_id) for item in occurrences]
        assert [run.dataset_version_id for run in runs] == [v2.id, v3.id]
        assert [run.config_id for run in runs] == [first.id, second.id]
        assert [db.scalar(select(Job.status).where(Job.run_id == run.id)) for run in runs] == ["QUEUED", "WAITING"]
        snapshot = runs[1].execution_plan["intake"]
        assert snapshot["effective_parameters"]["positive_columns"] == ["amount"]
        assert snapshot["root_configuration_id"] == first.id
        execute_run(db, runs[1])
        db.commit()
        assert runs[1].status == "QUEUED" and runs[1].output_version_id is None
        # A manual run on another revision joins the same logical queue.
        manual = enqueue(db, first, v1, None, user.name)
        db.commit()
        assert db.scalar(select(Job.status).where(Job.run_id == manual.id)) == "WAITING"
        execute_run(db, runs[0])
        db.commit()
        assert runs[0].decision == "APPROVED"
        assert db.scalar(select(Job.status).where(Job.run_id == runs[1].id)) == "QUEUED"
        output = db.get(DatasetVersion, runs[0].output_version_id)
        consume_dataset_event(db, event_for(db, output))
        db.commit()
        assert db.scalar(select(func.count()).select_from(IntakeOccurrence)) == 2
        execute_run(db, runs[1])
        db.commit()
        assert runs[1].execution_plan["intake"] == snapshot
        assert db.scalar(select(Job.status).where(Job.run_id == manual.id)) == "QUEUED"
        assert db.scalar(select(func.count()).select_from(IntakeScheduleVersion)) == 2
        assert schedule.version == 2


@pytest.mark.parametrize("mode", ["DAILY", "WEEKLY", "INTERVAL"])
def test_calendar_finite_idempotent_and_timezone_revalidated(database, tmp_path, mode):
    with database() as db:
        user, _, _, contract = setup_contract(db, tmp_path)
        now = utcnow()
        settings = IntakeScheduleSettings(mode=mode, starts_at=now,
            timezone="UTC", local_time=now.strftime("%H:%M"), weekdays=[now.weekday()], interval_seconds=60)
        schedule = save_schedule(db, contract, user, settings, enabled=True)
        due = schedule.next_run_at
        assert due is not None
        assert dispatch_due(db, due) == 1
        assert dispatch_due(db, due) == 0
        assert schedule.next_run_at > due
        assert db.scalar(select(func.count()).select_from(IntakeOccurrence)) == 1
        user.active = False
        db.flush()
        assert dispatch_due(db, schedule.next_run_at) == 1
        latest = db.scalar(select(IntakeOccurrence).order_by(IntakeOccurrence.planned_at.desc()))
        assert latest.status == "BLOCKED" and latest.reason_code == "INTAKE_EXECUTOR_DISABLED"
    with pytest.raises(ValueError):
        IntakeScheduleSettings(starts_at=now, timezone="not/a-zone")


def test_cancel_promotes_other_revision_quality_rejection_is_not_retried(authenticated, database, tmp_path):
    with database() as db:
        user, _, source, contract = setup_contract(db, tmp_path)
        second = Configuration(name=contract.name, organization_id=user.organization_id,
            module="intake", dataset_id=contract.dataset_id, previous_version_id=contract.id, version=2,
            config={"rules": [{"type": "length", "column": "id", "parameters": {"min": 10}}], "max_error_rate": 0})
        db.add(second)
        db.flush()
        first_run = enqueue(db, contract, source, None, user.name)
        rejected = enqueue(db, second, source, None, user.name)
        db.commit()
        first_id, rejected_id = first_run.id, rejected.id
    assert authenticated.post(f"/api/v1/runs/{first_id}/cancel").status_code == 200
    with database() as db:
        rejected = db.get(Run, rejected_id)
        assert db.scalar(select(Job.status).where(Job.run_id == rejected_id)) == "QUEUED"
        execute_run(db, rejected)
        db.commit()
        assert rejected.status == "SUCCESS" and rejected.decision == "REJECTED"
        assert db.scalar(select(IntakeContractState.active_run_id)) is None
        assert db.scalar(select(func.count()).select_from(Run).where(Run.module == "intake")) == 2
        schedule = save_schedule(db, second, db.get(User, "test-user"),
            IntakeScheduleSettings(mode="INTERVAL", starts_at=utcnow(), interval_seconds=60), enabled=True)
        assert dispatch_due(db, schedule.next_run_at) == 1
        occurrence = db.scalar(select(IntakeOccurrence))
        assert occurrence.status == "BLOCKED" and occurrence.reason_code == "QUALITY_REJECTION_REVIEW_REQUIRED"
        assert db.scalar(select(func.count()).select_from(Run).where(Run.module == "intake")) == 2


def test_delete_impact_revalidated_block_pending_exceptions_preserve_history_and_disable_hooks(authenticated, database, tmp_path):
    with database() as db:
        user, _, source, contract = setup_contract(db, tmp_path)
        settings = IntakeScheduleSettings(starts_at=utcnow() - timedelta(seconds=1))
        schedule = save_schedule(db, contract, user, settings, enabled=True)
        run = enqueue(db, contract, source, None, user.name)
        db.commit()
        blocked = deletion_impact(db, contract)
        assert not blocked["can_delete"]
        with pytest.raises(AutomationError, match="") as error:
            delete_contract(db, contract, user, impact_token=blocked["impact_token"], note="Baja probada")
        assert error.value.code == "INTAKE_DELETE_BLOCKED"
        execute_run(db, run)
        db.commit()
        output_id, config_id, historical_hash = run.output_version_id, contract.id, run.execution_plan["config_hash"]
        case = ExceptionCase(organization_id=user.organization_id, display_id="EXC-intake-delete",
            run_id=run.id, configuration_id=contract.id, title="Correct before delete", module="intake", severity="HIGH")
        db.add(case)
        db.flush()
        assert not deletion_impact(db, contract)["can_delete"]
        case.state = "ACCEPTED"
        db.flush()
        automation = DeliveryAutomation(organization_id=user.organization_id, name="Future output hook",
            responsible_user_id=user.id, enabled=True, version=1)
        db.add(automation)
        db.flush()
        chain = AutomationSettings(mode="CHAINED", starts_at=utcnow(), source_policy="INTAKE_OUTPUT", intake_configuration_id=contract.id)
        db.add(DeliveryAutomationVersion(organization_id=user.organization_id, automation_id=automation.id,
            version=1, configuration_id=contract.id, responsible_user_id=user.id, actor_id=user.id,
            enabled=True, settings={**chain.model_dump(mode="json"), "activated_at": iso(utcnow()), "template": {}}))
        db.commit()
        impact = deletion_impact(db, contract)
        assert impact["can_delete"] and impact["delivery_automation_ids"] == [automation.id]
        result = delete_contract(db, contract, user, impact_token=impact["impact_token"], note="Baja sin borrar históricos")
        db.commit()
        assert result["status"] == "DELETED"
        assert contract.status == "DELETED" and not schedule.enabled and not automation.enabled
        assert db.get(DatasetVersion, output_id) is not None
        assert run.decision == "APPROVED" and run.execution_plan["config_hash"] == historical_hash
        from trackvance.governance import strict_approval
        assert strict_approval(db, run)["approved"]
        with pytest.raises(AutomationError) as error:
            enqueue(db, contract, source, None, user.name)
        assert error.value.code == "INTAKE_CONTRACT_DELETED"
    assert not any(item["id"] == config_id for item in authenticated.get("/api/v1/intake/contracts").json()["items"])
    historical = authenticated.get(f"/api/v1/intake/contracts/{config_id}")
    assert historical.status_code == 200 and historical.json()["status_label"] == "Contrato eliminado"
    assert authenticated.post(f"/api/v1/intake/contracts/{config_id}/versions", json={"config": {}}).status_code == 410


def test_stale_delete_confirmation_and_permission_denial(authenticated, database, tmp_path):
    with database() as db:
        user, _, source, contract = setup_contract(db, tmp_path)
        impact = deletion_impact(db, contract)
        enqueue(db, contract, source, None, user.name)
        db.commit()
        config_id = contract.id
    result = authenticated.request("DELETE", f"/api/v1/intake/contracts/{config_id}", json={"impact_token": impact["impact_token"], "note": "Stale preview"})
    assert result.status_code == 409 and result.json()["error"]["code"] == "INTAKE_DELETE_IMPACT_CHANGED"
    with database() as db:
        user = db.get(User, "test-user")
        from trackvance.models import Role
        user.role_id = db.scalar(select(Role.id).where(Role.name == "Data Analyst"))
        db.commit()
    assert authenticated.get(f"/api/v1/intake/contracts/{config_id}/deletion-impact").status_code == 403


def test_publication_event_is_atomic_and_incomplete_event_has_no_effect(database, tmp_path):
    with database() as db:
        user, dataset, _, contract = setup_contract(db, tmp_path)
        save_schedule(db, contract, user, IntakeScheduleSettings(starts_at=utcnow()), enabled=True)
        db.commit()
        version = publish(db, dataset, tmp_path, "abort")
        identifier = version.id
        assert event_for(db, version).payload["publication_status"] == "COMPLETE"
        db.rollback()
        assert db.get(DatasetVersion, identifier) is None
        assert db.scalar(select(OutboxEvent.id).where(OutboxEvent.aggregate_id == identifier)) is None
        source = publish(db, dataset, tmp_path, "ready")
        event = event_for(db, source)
        event.payload = {**event.payload, "publication_status": "IN_PROGRESS"}
        consume_dataset_event(db, event)
        assert db.scalar(select(func.count()).select_from(IntakeOccurrence)) == 0


def test_technical_retries_are_bounded_and_interrupted_worker_recovers_without_losing_pending(database, tmp_path):
    from trackvance.worker import process_once

    class TechnicalFailure:
        def execute(self, db, run, lease_owner=None):
            raise OSError("synthetic transient storage interruption")

    with database() as db:
        user, _, source, contract = setup_contract(db, tmp_path)
        first = enqueue(db, contract, source, None, user.name)
        second = enqueue(db, contract, source, None, user.name)
        db.commit()
        first_id, second_id = first.id, second.id
    for expected_attempts in (1, 2, 3):
        assert process_once(owner=f"retry-owner-{expected_attempts}", engine=TechnicalFailure())
        with database() as db:
            job = db.scalar(select(Job).where(Job.run_id == first_id))
            run = db.get(Run, first_id)
            assert job.attempts == expected_attempts
            if expected_attempts < 3:
                assert run.status == "QUEUED" and job.status == "RETRY_WAIT"
                assert db.scalar(select(Job.status).where(Job.run_id == second_id)) == "WAITING"
                job.lease_until = utcnow() - timedelta(seconds=1)
                db.commit()
            else:
                assert run.status == "FAILED" and job.status == "FAILED"
                assert db.scalar(select(Job.status).where(Job.run_id == second_id)) == "QUEUED"
    # Durable lease recovery retains the same pending run and immutable input.
    with database() as db:
        job = db.scalar(select(Job).where(Job.run_id == second_id))
        job.status, job.lease_owner, job.lease_until, job.attempts = "RUNNING", "dead-owner", utcnow() - timedelta(seconds=1), 1
        db.get(Run, second_id).status = "RUNNING"
        db.commit()
    assert process_once(owner="recovered-owner")
    with database() as db:
        recovered = db.get(Run, second_id)
        assert recovered.status == "SUCCESS" and recovered.dataset_version_id == source.id
        assert db.scalar(select(Job.attempts).where(Job.run_id == second_id)) == 2
        assert db.scalar(select(IntakeContractState.active_run_id)) is None


def test_logical_delete_preserves_verified_strict_approval_and_report_source_eligibility(database, authenticated, approved):  # noqa: F811
    from trackvance.governance import (
        dataset_eligibility,
        strict_approval,
        verify_strict_approval_artifacts,
    )
    from trackvance.governance_models import StrictApproval
    from trackvance.report_service import _source

    classify(authenticated, approved["dataset"])
    request = {"alias": "approved_history", "input_dataset_id": approved["dataset"],
        "contract_id": approved["config"], "contract_revision_ids": [approved["config"]],
        "policy": "SPECIFIC", "input_version_id": approved["input"]}
    with database() as db:
        user, run = db.get(User, "test-user"), db.get(Run, approved["run"])
        contract, output = db.get(Configuration, approved["config"]), db.get(DatasetVersion, approved["output"])
        prior_approval = strict_approval(db, run)
        assert prior_approval["approved"] and verify_strict_approval_artifacts(db, run)["verified_bytes"]
        assert dataset_eligibility(db, user, output, "REPORT")["eligible"]
        before_source, before_warnings = _source(db, request, user)
        immutable = {"hash": output.sha256, "schema_hash": output.schema_hash,
                     "metrics": dict(run.metrics), "manifest": run.evidence_path, "config": dict(contract.config)}
        impact = deletion_impact(db, contract)
        delete_contract(db, contract, user, impact_token=impact["impact_token"], note="Preservar aprobación histórica")
        db.commit()
        assert strict_approval(db, run) == prior_approval
        assert verify_strict_approval_artifacts(db, run)["verified_bytes"]
        after_source, after_warnings = _source(db, request, user)
        assert after_source == before_source and after_warnings == before_warnings
        assert dataset_eligibility(db, user, output, "REPORT")["eligible"]
        assert {"hash": output.sha256, "schema_hash": output.schema_hash, "metrics": run.metrics,
                "manifest": run.evidence_path, "config": contract.config} == immutable
        assert db.scalar(select(func.count()).select_from(StrictApproval).where(StrictApproval.run_id == run.id)) == 1
