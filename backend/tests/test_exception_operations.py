import hashlib
import json
from datetime import timedelta

import polars as pl
import pytest
from sqlalchemy import select, update
from test_exception_validation import make_case

from trackvance.artifactstore import storage_provider
from trackvance.db import utcnow
from trackvance.models import Artifact, AuditEvent, ExceptionAttachment, ExceptionCase, User
from trackvance.operations_common import OperationError, save_case
from trackvance.processing import intake
from trackvance.services import (
    assess_exception_validation,
    record_exception_validation,
    refresh_pending_exception_validations,
)


def saved_case(database, state="OPEN"):
    with database() as db:
        case, _, run, _, _ = make_case(db, "sentinel", candidate_decision="HEALTHY")
        case.state = state
        db.commit()
        return case.id, run.id


def test_assignment_priority_sla_comments_and_server_filters(authenticated, database):
    case_id, _ = saved_case(database)
    base = f"/api/v1/exceptions/{case_id}"
    response = authenticated.patch(base, json={"version": 1, "state": "ASSIGNED", "assigned_user_id": "test-user",
                                               "priority": "CRITICAL", "sla_hours": 12, "comment": "Atender antes del cierre"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assignee"]["id"] == "test-user" and body["owner"] == "Test User"
    assert body["priority"] == "CRITICAL" and body["sla_hours"] == 12 and body["due_at"]
    assert body["auto_resolve_enabled"] is False
    assert body["events"][-1]["changes"]["assigned_user_id"] == "test-user"
    response = authenticated.post(f"{base}/comments", json={"version": 2, "comment": "Fuente contactada"})
    assert response.status_code == 200
    assert response.json()["events"][-1]["event_type"] == "COMMENT_ADDED"
    due = utcnow() - timedelta(hours=1)
    assert authenticated.patch(base, json={"version": 3, "due_at": due.isoformat()}).status_code == 200
    query = "/api/v1/exceptions?assigned_user_id=test-user&priority=CRITICAL&state=ASSIGNED&overdue=true&search=Hallazgo"
    result = authenticated.get(query).json()
    assert result["total"] == 1 and result["items"][0]["overdue"]
    assert authenticated.get("/api/v1/exceptions?search=nonexistent").json()["total"] == 0
    assert authenticated.get("/api/v1/exceptions?overdue=false").json()["total"] == 0
    assert authenticated.get("/api/v1/exceptions?limit=1001").status_code == 422
    assert authenticated.get("/api/v1/exceptions/assignees").json()["items"][0]["id"] == "test-user"


def test_attachment_storage_hash_timeline_download_and_scope(authenticated, database):
    case_id, _ = saved_case(database)
    base = f"/api/v1/exceptions/{case_id}"
    response = authenticated.post(f"{base}/attachments", data={"version": 1, "description": "Comprobante de corrección"},
                                  files={"file": ("../../evidence.txt", b"Immutable evidence", "text/plain")})
    assert response.status_code == 201, response.text
    body = response.json()
    attachment = body["attachments"][0]
    assert attachment["name"] == "evidence.txt" and body["version"] == 2
    downloaded = authenticated.get(f"{base}/attachments/{attachment['id']}/download")
    assert downloaded.status_code == 200 and downloaded.content == b"Immutable evidence"
    assert "attachment" in downloaded.headers["content-disposition"]
    with database() as db:
        row = db.get(ExceptionAttachment, attachment["id"])
        artifact = db.get(Artifact, row.artifact_id)
        assert artifact.kind == "EXCEPTION_ATTACHMENT"
        path = storage_provider.materialize(artifact)
        assert path.read_bytes() == b"Immutable evidence"
        db.get(ExceptionCase, case_id).organization_id = "another-org"
        db.commit()
    assert authenticated.get(f"{base}/attachments/{attachment['id']}/download").status_code == 404
    assert authenticated.post(f"{base}/comments", json={"version": 2, "comment": "Forbidden"}).status_code == 404


@pytest.mark.parametrize("filename,content,status", [("evil.html", b"<script>bad()</script>", 422),
                                                       ("empty.txt", b"", 422),
                                                       ("large.txt", b"x" * (10 * 1024 * 1024 + 1), 413)],
                         ids=["unsafe-extension", "empty", "too-large"])
def test_attachment_bounds_and_content_type(authenticated, database, filename, content, status):
    case_id, _ = saved_case(database)
    response = authenticated.post(f"/api/v1/exceptions/{case_id}/attachments", data={"version": 1}, files={"file": (filename, content)})
    assert response.status_code == status
    with database() as db:
        assert not db.scalar(select(ExceptionAttachment))


def test_reopened_case_cannot_reuse_old_validation(authenticated, database):
    case_id, run_id = saved_case(database, "PENDING_VALIDATION")
    base = f"/api/v1/exceptions/{case_id}"
    resolved = authenticated.patch(base, json={"version": 1, "state": "RESOLVED", "root_cause": "Error", "resolution": "Corregido"})
    assert resolved.status_code == 200
    assert authenticated.patch(base, json={"version": 2, "state": "REOPENED"}).status_code == 422
    opened = authenticated.patch(base, json={"version": 2, "state": "REOPENED", "comment": "Se detectó recurrencia"})
    assert opened.status_code == 200 and opened.json()["reopened_at"]
    assert opened.json()["technical_validation"]["status"] == "NO_LATER_RUN"
    assert opened.json()["validation_run_id"] is None
    assert any(event.get("validation_run_id") == run_id for event in opened.json()["events"])


def test_each_administrative_closure_requires_an_explicit_current_reason(authenticated, database):
    case_id, _ = saved_case(database)
    base = f"/api/v1/exceptions/{case_id}"
    accepted = authenticated.patch(base, json={"version": 1, "state": "ACCEPTED", "administrative_reason": "Riesgo inicial aceptado"})
    assert accepted.status_code == 200
    assert authenticated.patch(base, json={"version": 2, "state": "REOPENED", "comment": "El alcance cambió"}).status_code == 200
    for reason in ({}, {"administrative_reason": "   \t\n"}):
        denied = authenticated.patch(base, json={"version": 3, "state": "NOT_APPLICABLE", **reason})
        assert denied.status_code == 422
        assert denied.json()["error"]["code"] == "ADMINISTRATIVE_REASON_REQUIRED"
    result = authenticated.patch(base, json={"version": 3, "state": "NOT_APPLICABLE", "administrative_reason": "Nueva decisión fuera de alcance"})
    assert result.status_code == 200
    assert result.json()["validation_run_id"] is None
    reasons = [event.get("administrative_reason") for event in result.json()["events"]]
    assert "Riesgo inicial aceptado" in reasons and "Nueva decisión fuera de alcance" in reasons


@pytest.mark.parametrize("enabled", [True, False])
def test_auto_resolution_policy_system_evidence_and_idempotence(database, enabled):
    with database() as db:
        case, _, run, _, _ = make_case(db, "sentinel", candidate_decision="HEALTHY")
        case.auto_resolve_enabled = enabled
        db.flush()
        refresh_pending_exception_validations(db, run, "Worker")
        assert case.state == ("RESOLVED" if enabled else "PENDING_VALIDATION")
        first_version, first_events = case.version, list(case.events)
        refresh_pending_exception_validations(db, run, "Worker")
        assert case.version == first_version and case.events == first_events
        if enabled:
            assert case.events[-1]["actor_type"] == "SYSTEM"
            assert case.validation_run_id == run.id
            events = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "EXCEPTION_AUTO_RESOLVED")).all()
            assert len(events) == 1 and events[0].actor_type == "SYSTEM" and events[0].run_id == run.id


def test_validation_and_updates_reject_stale_concurrent_writer(database):
    with database() as db:
        case, _, _, _, _ = make_case(db, "sentinel", candidate_decision="HEALTHY")
        assessment = assess_exception_validation(db, case)
        db.execute(update(ExceptionCase).where(ExceptionCase.id == case.id).values(version=2, owner="Other writer")
                   .execution_options(synchronize_session=False))
        with pytest.raises(OperationError, match="Otra persona"):
            record_exception_validation(db, case, assessment, "Worker")
        with pytest.raises(OperationError):
            save_case(db, case, 1, {"owner": "Stale"})
        db.refresh(case)
        assert case.owner == "Other writer" and case.version == 2


@pytest.mark.parametrize("state", ["ASSIGNED", "INVESTIGATING", "PENDING_VALIDATION"])
def test_invalid_assignee_and_automatic_policy_permissions(authenticated, database, state):
    case_id, _ = saved_case(database, state if state != "ASSIGNED" else "OPEN")
    with database() as db:
        db.get(User, "test-user").role = "Data Analyst"
        db.add(User(id="other-user", name="Private", email="private@other.test", organization_id="other-org", password_hash="unused"))
        db.commit()
    base = f"/api/v1/exceptions/{case_id}"
    assert authenticated.patch(base, json={"version": 1, "assigned_user_id": "other-user"}).status_code == 422
    response = authenticated.patch(base, json={"version": 1, "auto_resolve_enabled": True})
    assert response.status_code == 403
    assert "other-user" not in json.dumps(authenticated.get("/api/v1/exceptions/assignees").json())


def test_declared_rule_without_evaluated_rows_is_not_technical_proof(database):
    with database() as db:
        case, _, candidate, finding, _ = make_case(db, "intake", candidate_decision="APPROVED")
        finding.details = {"rule_id": "rule-conditional"}
        candidate.metrics = {"rules": [{"rule_id": "rule-conditional", "failed_count": 0, "evaluated_count": 0}]}
        db.flush()
        assert assess_exception_validation(db, case)["validated"] is False
        candidate.metrics = {"rules": [{"rule_id": "rule-conditional", "failed_count": 0, "evaluated_count": 2}]}
        assert assess_exception_validation(db, case)["validated"] is True


def test_legacy_numeric_ignore_requires_real_evaluated_rows(database):
    config = {"rules": [{"type": "numeric", "column": "amount", "parameters": {"null_policy": "IGNORE"}}]}
    _, failed_metrics, _ = intake(pl.DataFrame({"amount": ["invalid"]}), config)
    _, skipped_metrics, _ = intake(pl.DataFrame({"amount": [None]}, schema={"amount": pl.String}), config)
    _, passed_metrics, _ = intake(pl.DataFrame({"amount": ["12.50"]}), config)
    assert failed_metrics["rules"][0]["rule_id"] is None
    assert skipped_metrics["rules"][0]["evaluated_count"] == 0
    with database() as db:
        case, configuration, candidate, finding, _ = make_case(db, "intake", candidate_decision="APPROVED")
        configuration.config = config
        finding.code = "NUMERIC:amount"
        finding.fingerprint = hashlib.sha256(b"NUMERIC:amount").hexdigest()
        finding.details = failed_metrics["rules"][0]
        candidate.metrics = skipped_metrics
        db.flush()
        skipped = assess_exception_validation(db, case)
        assert skipped["validated"] is False
        assert skipped["evidence"]["criterion"] == "INTAKE_RULE_NOT_EVALUATED"
        assert "no evaluó filas" in skipped["reason"]
        candidate.metrics = passed_metrics
        passed = assess_exception_validation(db, case)
        assert passed["validated"] is True
        assert passed["evidence"]["rule_evaluation"]["identity_method"] == "LEGACY_CODE_COLUMN"
        assert passed["evidence"]["rule_evaluation"]["summaries"][0]["evaluated_count"] == 1


@pytest.mark.parametrize("metrics", [
    {}, {"rules": []},
    {"rules": [{"code": "REQUIRED", "column": "customer_id", "status": "PASS"}]},
    {"rules": [{"code": "REQUIRED", "column": "other", "evaluated_count": 5, "failed_count": 0}]},
    {"rules": [{"code": "REQUIRED", "column": "customer_id", "evaluated_count": "5", "failed_count": 0}]},
])
def test_missing_or_unidentifiable_historical_counters_cannot_authorize_new_resolution(database, metrics):
    with database() as db:
        case, _, candidate, _, _ = make_case(db, "intake", candidate_decision="APPROVED")
        candidate.metrics = metrics
        assessment = assess_exception_validation(db, case)
        assert assessment["can_resolve"] is False
        assert assessment["evidence"]["criterion"] == "INTAKE_RULE_EVIDENCE_INSUFFICIENT"
        assert "Ejecuta nuevamente la misma configuración" in assessment["reason"]


def test_legacy_aggregated_fingerprint_requires_all_matching_rules_to_pass(database):
    with database() as db:
        case, _, candidate, finding, _ = make_case(db, "intake", candidate_decision="APPROVED")
        finding.code = "Legacy display label"
        finding.details = {}
        finding.fingerprint = hashlib.sha256(b"RANGE:amount").hexdigest()
        passed = {"code": "RANGE", "column": "amount", "evaluated_count": 1, "failed_count": 0}
        skipped = {**passed, "evaluated_count": 0}
        candidate.metrics = {"rules": [passed, skipped]}
        assert assess_exception_validation(db, case)["validated"] is False
        candidate.metrics = {"rules": [passed, passed]}
        assert assess_exception_validation(db, case)["validated"] is True


def test_legacy_case_with_no_metric_counters_is_blocked_at_resolution_endpoint(authenticated, database):
    with database() as db:
        case, _, candidate, _, _ = make_case(db, "intake", candidate_decision="APPROVED")
        candidate.metrics = {}
        db.commit()
        case_id = case.id
    response = authenticated.patch(f"/api/v1/exceptions/{case_id}", json={
        "version": 1, "state": "RESOLVED", "root_cause": "Corregido", "resolution": "Nueva fuente",
    })
    assert response.status_code == 422
    current = authenticated.get(f"/api/v1/exceptions/{case_id}").json()
    assert current["state"] == "PENDING_VALIDATION"
    assert current["technical_validation"]["evidence"]["criterion"] == "INTAKE_RULE_EVIDENCE_INSUFFICIENT"
