"""Execute Intake and durable outbox decisions without inventing terminal runs."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_automation_events import automation_case  # noqa: F401
from test_delivery_service_api import delivery_case, delivery_runtime  # noqa: F401

from trackvance import events
from trackvance.automation import consume_intake_event
from trackvance.automation_models import DeliveryOccurrence, OutboxEvent
from trackvance.db import iso, utcnow
from trackvance.delivery_service import execute_delivery_run
from trackvance.models import Configuration, Dataset, DatasetVersion, Run, User
from trackvance.services import create_version, execute_run


@pytest.mark.parametrize("case,allow_warnings,decision,reason", [
    ("warning", False, "APPROVED_WITH_WARNINGS", "INTAKE_DECISION_NOT_ACCEPTED"),
    ("warning", True, "APPROVED_WITH_WARNINGS", None),
    ("rejected", False, "REJECTED", "INTAKE_DECISION_NOT_ACCEPTED"),
    ("empty", False, "APPROVED", "EMPTY_INPUT_BLOCKED"),
])
def test_actual_intake_decisions_control_chain_and_duplicate_event_never_replays(
    authenticated, database, automation_case, tmp_path, case, allow_warnings, decision, reason,  # noqa: F811
):
    with database() as db:
        source = db.get(DatasetVersion, automation_case["source_id"])
        if case == "empty":
            empty = tmp_path / "empty.csv"
            empty.write_text("tenant_id,external_id,amount,note\n", encoding="utf-8")
            source = create_version(db, db.get(Dataset, source.dataset_id), empty, empty.name,
                                    actor=db.get(User, "test-user").name)
        config = {"required_columns": ["external_id"], "max_error_rate": 0}
        if case != "empty":
            config["rules"] = [{"type": "length", "column": "external_id",
                "severity": "WARNING" if case == "warning" else "ERROR",
                "parameters": {"min": 20, "max": 30}}]
        intake = Configuration(name=f"Actual {case} Intake", module="intake",
            dataset_id=source.dataset_id, config=config)
        db.add(intake)
        db.commit()
        source_id, intake_id = source.id, intake.id

    response = authenticated.post("/api/v1/delivery/automations", json={
        "configuration_id": automation_case["config_id"], "name": f"Chain {case} {allow_warnings}",
        "settings": {"mode": "CHAINED", "timezone": "America/Bogota",
            "starts_at": iso(utcnow() - timedelta(seconds=1)), "source_policy": "INTAKE_OUTPUT",
            "intake_configuration_id": intake_id, "allow_warnings": allow_warnings},
    })
    assert response.status_code == 201, response.text
    automation_id = response.json()["id"]
    response = authenticated.post("/api/v1/intake/runs", json={
        "contract_id": intake_id, "dataset_version_id": source_id,
    })
    assert response.status_code == 202, response.text
    intake_run_id = response.json()["id"]
    with database() as db:
        run = db.get(Run, intake_run_id)
        execute_run(db, run)
        db.commit()
        assert run.status == "SUCCESS" and run.decision == decision
        assert run.output_version_id and run.evidence_path
        output_id = run.output_version_id
        assert db.get(DatasetVersion, output_id).row_count == (0 if case != "warning" else 2)

    while events.consume_once("CHAINING"):
        pass
    response = authenticated.get(f"/api/v1/delivery/automations/{automation_id}/occurrences")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    occurrence = response.json()["items"][0]
    assert occurrence["source_run_id"] == intake_run_id
    assert occurrence["reason_code"] == reason
    assert occurrence["status"] == ("QUEUED" if reason is None else "SKIPPED")

    with database() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == intake_run_id,
                                                  OutboxEvent.event_type == "RUN_TERMINAL"))
        consume_intake_event(db, event)
        db.commit()
        assert db.scalar(select(func.count()).select_from(DeliveryOccurrence).where(
            DeliveryOccurrence.automation_id == automation_id)) == 1
        assert db.scalar(select(func.count()).select_from(Run).where(Run.module == "DELIVERY")) == (reason is None)
        if reason is None:
            delivery = db.get(Run, occurrence["run_id"])
            assert delivery.dataset_version_id == output_id != source_id
            captured = []
            automation_case["runtime"].deliver_hook = lambda: captured.extend(
                next(call[1] for call in reversed(automation_case["runtime"].calls) if call[0] == "deliver"))
            execute_delivery_run(db, delivery)
            db.commit()
            assert delivery.status == "SUCCESS" and delivery.decision == "COMMITTED"
            written = [call for call in automation_case["runtime"].calls if call[0] == "deliver"]
            assert len(written) == 1
            assert captured == [("T1", "A-001", Decimal("10.25"), "alpha"),
                                ("T1", "A-002", Decimal("20.50"), "beta")]
            assert all(isinstance(row[2], Decimal) for row in captured)
            consume_intake_event(db, event)
            db.commit()
            assert db.scalar(select(func.count()).select_from(Run).where(Run.module == "DELIVERY")) == 1
        else:
            assert occurrence["run_id"] is None
            assert not any(call[0] == "deliver" for call in automation_case["runtime"].calls)
