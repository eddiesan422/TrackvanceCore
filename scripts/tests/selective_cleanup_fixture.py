"""Populate an owned restored PostgreSQL copy only; no worker or remote I/O."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


def require_native_database(db):
    if db.get_bind().dialect.name != "postgresql":
        raise ValueError("The real-copy harness requires PostgreSQL.")


def build(project: str) -> dict:
    if not re.fullmatch(r"trackvance-v080-test-restore085-[a-f0-9]{12}", project) or os.getenv("TRACKVANCE_CERTIFICATION_PROJECT") != project:
        raise ValueError("Synthetic cleanup fixture requires the exact owned restored-copy environment.")
    from sqlalchemy import select

    from trackvance import models  # noqa: F401
    from trackvance.automation_models import (
        DeliveryTargetGuard,
        EventConsumption,
        InternalNotification,
        OutboxEvent,
    )
    from trackvance.credential_store import secret_store
    from trackvance.db import SessionLocal, utcnow
    from trackvance.delivery_credential_store import destination_secret_store
    from trackvance.governance_models import (
        GovernanceHistory,
        GovernancePerson,
        StrictApproval,
    )
    from trackvance.models import (
        Artifact,
        AuditEvent,
        Configuration,
        Dataset,
        DeliveryAttempt,
        DeliveryDestination,
        DeliveryDestinationVersion,
        ExternalConnection,
        ExternalConnectionVersion,
        Job,
        MonitorSchedule,
        MonitorScheduleVersion,
        Role,
        Run,
        User,
    )
    from trackvance.operational_cleanup import digest
    from trackvance.report_models import (
        ReportContext,
        ReportDefinition,
        ReportExecution,
        ReportRevision,
    )
    from trackvance.services import create_version

    label = "Synthetic cleanup085 " + uuid4().hex[:10]
    with SessionLocal() as db, TemporaryDirectory(prefix="cleanup-fixture-") as temp:
        require_native_database(db)
        admin = db.scalar(select(User).join(Role, Role.id == User.role_id).where(
            Role.system_key == "ADMINISTRATOR", User.active.is_(True), User.deleted.is_(False)).order_by(User.id))
        person = db.scalar(select(GovernancePerson).where(GovernancePerson.organization_id == admin.organization_id))
        org, now = admin.organization_id, utcnow()
        common = {"organization_id": org}
        source = Path(temp) / "synthetic.csv"
        source.write_text("id,value\n001,alpha\n002,beta\n003,gamma\n", encoding="utf-8")
        clean = Dataset(name=label + " removable", information_classification="INTERNAL",
            business_owner_person_id=person.id, steward_person_id=person.id, technical_custodian_person_id=person.id, **common)
        guarded = Dataset(name=label + " UNKNOWN retained", **common)
        db.add_all([clean, guarded]); db.flush()
        raw = create_version(db, clean, source, source.name)
        guarded_source = Path(temp) / "guarded.csv"
        guarded_source.write_text("id,value\n901,guarded\n902,retained\n903,unknown\n", encoding="utf-8")
        guarded_version = create_version(db, guarded, guarded_source, guarded_source.name)
        contract = Configuration(name=label + " intake metadata", module="intake", dataset_id=clean.id,
            config={"fixture": "SYNTHETIC_METADATA_NO_ENGINE_RUN"}, **common)
        db.add(contract); db.flush()
        output_dataset = Dataset(name=label + " removable output", intake_input_dataset_id=clean.id,
            intake_contract_id=contract.id, **common)
        db.add(output_dataset); db.flush()
        output = create_version(db, output_dataset, source, source.name, parent_version_id=raw.id)
        completed = Run(module="intake", name=label + " terminal metadata", status="SUCCESS", decision="APPROVED",
            config_id=contract.id, dataset_version_id=raw.id, output_version_id=output.id,
            initiated_by=admin.name, initiated_by_type="USER", initiated_by_id=admin.id,
            metrics={"fixture": "SYNTHETIC_METADATA_NO_ENGINE_RUN"}, **common)
        db.add(completed); db.flush(); output.source_run_id=completed.id
        db.add(Job(run_id=completed.id, status="SUCCESS", lane="DEFAULT", **common))
        db.add(StrictApproval(run_id=completed.id, input_version_id=raw.id, output_version_id=output.id,
            contract_id=contract.id, contract_revision_id=contract.id, evidence_hash="a"*64,
            governance_snapshot={"schema_version":2,"fixture":"SYNTHETIC_METADATA"}, criterion_version=2, **common))
        db.add(GovernanceHistory(dataset_id=clean.id, version=1, actor_id=admin.id,
            snapshot={"schema_version":2,"business_owner_person_id":person.id}, reason="Synthetic cleanup fixture", **common))
        monitor = Configuration(name=label + " disabled monitor", module="sentinel", dataset_id=clean.id, config={}, **common)
        db.add(monitor); db.flush()
        schedule=MonitorSchedule(monitor_id=monitor.id, enabled=False, next_run_at=now, **common)
        db.add(schedule); db.flush()
        db.add(MonitorScheduleVersion(schedule_id=schedule.id, version=1, interval_seconds=3600,
            enabled=False, starts_at=now, actor_id=admin.id, responsible_user_id=admin.id, **common))
        # Frozen Reportes references every exact input/output identity; no report
        # worker is invoked and no current engine decision is claimed by this fixture.
        definition=ReportDefinition(name=label+" source-only report",owner_user_id=admin.id,**common)
        db.add(definition);db.flush()
        draft={"mode":"GUIDED","sources":[{"alias":"a","input_dataset_id":clean.id,"contract_id":contract.id,
            "contract_revision_ids":[contract.id],"policy":"LATEST_APPROVED"}],"columns":[],"joins":[],"order_by":[]}
        revision=ReportRevision(definition_id=definition.id,version=1,draft=draft,query_hash=digest(draft),created_by_user_id=admin.id,**common)
        db.add(revision);db.flush()
        artifact=db.get(Artifact,output.canonical_artifact_id)
        frozen={"user_id":admin.id,"organization_id":org,"sources":[{"alias":"a","input_dataset_id":clean.id,
            "input_version_id":raw.id,"output_dataset_id":output_dataset.id,"output_version_id":output.id,
            "approval_run_id":completed.id,"contract_id":contract.id,"contract_revision_id":contract.id,
            "canonical_artifact_id":artifact.id,"canonical_sha256":artifact.sha256,"schema_hash":output.schema_hash,"schema":output.schema_json}]}
        context=ReportContext(user_id=admin.id,revision_id=revision.id,expires_at=now+timedelta(minutes=30),snapshot=frozen,integrity_hash=digest(frozen),**common)
        db.add(context);db.flush()
        execution=ReportExecution(context_id=context.id,user_id=admin.id,profile="PREVIEW",status="SUCCESS",generation_status="READY",**common)
        db.add(execution);db.flush()
        # Protected reference/secret families are populated locally and disabled.
        # Opaque references and plaintext values are never printed or connected.
        connection=ExternalConnection(name=label+" never-connected source",source_type="POSTGRESQL",enabled=False,**common)
        destination=DeliveryDestination(name=label+" never-connected destination",sink_type="POSTGRESQL",enabled=False,**common)
        db.add_all([connection,destination]);db.flush()
        db.add(ExternalConnectionVersion(connection_id=connection.id,version=1,host="fixture.invalid",port=5432,
            database="synthetic",username="synthetic",options={},secret_reference=secret_store.put(org,uuid4().hex),config_hash="b"*64,**common))
        destination_version=DeliveryDestinationVersion(destination_id=destination.id,version=1,
            config={"host":"fixture.invalid","port":5432,"database":"synthetic","username":"synthetic","options":{}},
            secret_reference=destination_secret_store.put(org,uuid4().hex),config_hash="c"*64,**common)
        db.add(destination_version);db.flush()
        delivery=Configuration(name=label+" guarded metadata",module="DELIVERY",dataset_id=guarded.id,
            config={"destination_id":destination.id,"destination_version_id":destination_version.id,"fixture":"SYNTHETIC_NO_REMOTE_IO"},**common)
        db.add(delivery);db.flush()
        unknown=Run(module="DELIVERY",name=label+" UNKNOWN synthetic",status="UNKNOWN",decision="UNKNOWN",
            config_id=delivery.id,dataset_version_id=guarded_version.id,initiated_by=admin.name,
            initiated_by_type="USER",initiated_by_id=admin.id,metrics={"fixture":"SYNTHETIC_UNKNOWN_NO_REMOTE_IO"},**common)
        db.add(unknown);db.flush()
        attempt=DeliveryAttempt(run_id=unknown.id,destination_version_id=destination_version.id,attempt_number=1,
            idempotency_key=uuid4().hex,status="UNKNOWN",target_locator="synthetic.never_connected",rows_attempted=3,
            system_audit={"fixture":"SYNTHETIC_UNKNOWN_NO_REMOTE_IO"},**common)
        guard=DeliveryTargetGuard(target_fingerprint=uuid4().hex+uuid4().hex,unknown_run_id=unknown.id,**common)
        db.add_all([attempt,guard,Job(run_id=unknown.id,lane="DELIVERY",status="UNKNOWN",**common)])
        for run in (completed,unknown):
            event=OutboxEvent(dedupe_key="cleanup-fixture:"+run.id,event_type="RUN_COMPLETED",aggregate_type="RUN",
                aggregate_id=run.id,module=run.module,payload={"recipient_user_id":admin.id,"status":run.status},**common)
            db.add(event);db.flush()
            db.add(EventConsumption(event_id=event.id,consumer="NOTIFICATIONS",status="DONE",completed_at=now,**common))
            db.add(InternalNotification(event_id=event.id,recipient_user_id=admin.id,module=run.module,origin="MANUAL",
                status=run.status,decision=run.decision,description="Synthetic cleanup notice",resource_type="RUN",resource_id=run.id,
                detail_url="/runs/"+run.id,**common))
        db.add(AuditEvent(event_type="SYNTHETIC_CLEANUP_FIXTURE_CREATED",actor="Isolated fixture",actor_type="USER",actor_id=admin.id,
            subject_type="synthetic_cleanup_fixture",subject_id=clean.id,message="Synthetic local metadata; no external I/O",**common))
        db.commit()
        return {"project":project,"organization_id":org,"actor_id":admin.id,"dataset_ids":[clean.id,output_dataset.id,guarded.id],
            "removable_dataset_ids":[clean.id,output_dataset.id],"protected_dataset_id":guarded.id,"unknown_run_id":unknown.id,
            "unknown_attempt_id":attempt.id,"target_guard_id":guard.id,"fixture":"SYNTHETIC_LOCAL_METADATA_NO_ENGINE_OR_REMOTE_IO"}


if __name__=="__main__":
    try:
        print(json.dumps(build(sys.argv[1])))
    except Exception as exc:  # noqa: BLE001 - private native fixture boundary redacts every SQL/secret error
        print(json.dumps({"status":"FAIL","error_type":type(exc).__name__}))
        raise SystemExit(1) from None
