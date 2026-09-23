import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from trackvance import api
from trackvance.db import utcnow
from trackvance.models import (
    AuditEvent,
    AuthSession,
    Configuration,
    Dataset,
    DatasetVersion,
    Finding,
    Job,
    Run,
)
from trackvance.services import create_exception, create_version, enqueue
from trackvance.worker import process_once


def test_authentication_csrf_origin_and_logout(client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/datasets").status_code == 401
    assert client.post("/api/v1/auth/demo").status_code == 404
    credentials = {"email": "tester@example.test", "password": "test-password"}
    assert client.post("/api/v1/auth/login", json={**credentials, "password": "wrong"}).status_code == 401
    denied = client.post("/api/v1/auth/login", json=credentials, headers={"Origin": "https://untrusted.test"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ORIGIN_DENIED"
    login = client.post("/api/v1/auth/login", json=credentials)
    assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/v1/me").json()["user"]["id"] == "test-user"
    for headers in ({}, {"X-CSRF-Token": "wrong"}):
        response = client.post("/api/v1/datasets", json={"name": "Blocked"}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    assert client.post("/api/v1/datasets", json={"name": "Authorized"}, headers=headers).status_code == 201
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/me").status_code == 401


def test_expired_session_and_cross_organization_record_are_rejected(authenticated, database):
    with database() as db:
        other = Dataset(name="Other organization", organization_id="other-org")
        db.add(other)
        db.commit()
        other_id = other.id
    assert authenticated.get(f"/api/v1/datasets/{other_id}").status_code == 404
    assert other_id not in [d["id"] for d in authenticated.get("/api/v1/datasets").json()["items"]]
    with database() as db:
        session = db.scalar(select(AuthSession))
        session.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert authenticated.get("/api/v1/datasets").status_code == 401


def test_upload_rejects_duplicate_headers_without_persisting_version(authenticated, database):
    dataset = authenticated.post("/api/v1/datasets", json={"name": "CSV validation"}).json()
    response = authenticated.post(f"/api/v1/datasets/{dataset['id']}/versions/upload",
                                  files={"file": ("duplicate.csv", b"id,id\nA,B\n", "text/csv")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DATA"
    with database() as db:
        assert db.scalar(select(DatasetVersion)) is None


def test_worker_persists_result_evidence_lineage_and_does_not_rerun(authenticated, database, queued_intake):
    run_id = queued_intake["run_id"]
    assert authenticated.get(f"/api/v1/runs/{run_id}/evidence").status_code == 409
    active = {}
    assert process_once("test-worker", active) is True
    assert active == {}
    assert process_once("test-worker", active) is False
    response = authenticated.get(f"/api/v1/runs/{run_id}")
    run = response.json()
    assert run["status"] == "SUCCESS"
    assert run["decision"] == "REJECTED"
    assert run["metrics"]["valid_rows"] == 1
    assert run["metrics"]["error_rows"] == 2
    assert len(run["findings"]) == 1
    results = authenticated.get(f"/api/v1/runs/{run_id}/results?limit=1").json()
    assert results["total"] == 2 and len(results["items"]) == 1
    assert authenticated.get(f"/api/v1/runs/{run_id}/results?offset=-1").status_code == 422
    evidence = authenticated.get(f"/api/v1/runs/{run_id}/evidence")
    assert evidence.status_code == 200
    manifest = evidence.json()
    assert manifest["configuration"]["id"] == queued_intake["config_id"]
    assert manifest["inputs"][0]["dataset_version_id"] == queued_intake["version_id"]
    assert authenticated.get(f"/api/v1/runs/{run_id}/export.csv").status_code == 200
    with database() as db:
        persisted = db.get(Run, run_id)
        output = db.get(DatasetVersion, persisted.output_version_id)
        job = db.scalar(select(Job).where(Job.run_id == run_id))
        assert output.parent_version_id == queued_intake["version_id"]
        assert output.row_count == 1
        assert job.status == "SUCCESS" and job.attempts == 1 and job.lease_until is None
        assert manifest["result_artifacts"][0]["sha256"] == hashlib.sha256(Path(persisted.result_path).read_bytes()).hexdigest()
        assert db.scalar(select(AuditEvent).where(AuditEvent.event_type == "RUN_COMPLETED"))


def test_worker_detects_tampered_input_and_records_failure(database, queued_intake):
    Path(queued_intake["original_path"]).write_text("order_id,amount\nA,99\n", encoding="utf-8")
    assert process_once("test-worker") is True
    with database() as db:
        run = db.get(Run, queued_intake["run_id"])
        job = db.scalar(select(Job).where(Job.run_id == run.id))
        assert run.status == job.status == "FAILED"
        assert "SHA-256" in run.error
        assert run.result_path is None and run.evidence_path is None
        assert db.scalar(select(AuditEvent).where(AuditEvent.event_type == "RUN_FAILED"))


def test_worker_recovers_expired_lease(database, queued_intake):
    with database() as db:
        job = db.scalar(select(Job))
        job.status, job.lease_owner = "RUNNING", "interrupted-worker"
        job.lease_until, job.attempts = utcnow() - timedelta(minutes=2), 1
        db.commit()
    assert process_once("recovery-worker") is True
    with database() as db:
        job = db.scalar(select(Job))
        assert job.status == "SUCCESS"
        assert job.attempts == 2 and job.lease_owner == "recovery-worker"


def test_cancelled_queue_is_never_processed(authenticated, queued_intake):
    url = f"/api/v1/runs/{queued_intake['run_id']}/cancel"
    response = authenticated.post(url)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert process_once("test-worker") is False
    assert authenticated.post(url).status_code == 409


def test_cancel_locks_run_before_deciding_queued_state(monkeypatch):
    run = Run(id="run-lock", organization_id="org-lock", module="DELIVERY", status="QUEUED")
    job = Job(
        id="job-lock",
        organization_id="org-lock",
        run_id=run.id,
        status="QUEUED",
        lane="DELIVERY",
    )

    class CapturingSession:
        def __init__(self):
            self.queries = []
            self.records = iter((run, job))
            self.committed = False

        def scalar(self, statement):
            self.queries.append(statement)
            return next(self.records)

        def commit(self):
            self.committed = True

    session = CapturingSession()
    monkeypatch.setattr(api, "audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api, "run_dto", lambda _db, value: {"status": value.status})

    result = api.cancel_run(
        run.id,
        db=session,
        user=SimpleNamespace(organization_id="org-lock", name="Tester"),
    )

    assert result == {"status": "CANCELLED"}
    assert session.committed is True
    assert len(session.queries) == 2
    assert session.queries[0]._for_update_arg is not None
    assert session.queries[1]._for_update_arg is not None


def test_exception_version_conflicts_transitions_and_resolution_requirements(authenticated, database, queued_intake, tmp_path):
    assert process_once("test-worker") is True
    with database() as db:
        finding = db.scalar(select(Finding).where(Finding.run_id == queued_intake["run_id"]))
        case = create_exception(db, finding, "Test User")
        db.commit()
        case_id, finding_id = case.id, finding.id
    created_again = authenticated.post(f"/api/v1/findings/{finding_id}/exceptions")
    assert created_again.json()["id"] == case_id
    url = f"/api/v1/exceptions/{case_id}"
    response = authenticated.patch(url, json={"version": 1, "state": "INVESTIGATING", "owner": "Analyst"})
    assert response.status_code == 200
    assert response.json()["version"] == 2
    stale = authenticated.patch(url, json={"version": 1, "owner": "Stale writer"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    direct = authenticated.patch(url, json={"version": 2, "state": "RESOLVED", "root_cause": "Invalid source amount", "resolution": "Source file corrected"})
    assert direct.status_code == 422 and direct.json()["error"]["code"] == "INVALID_TRANSITION"
    pending = authenticated.patch(url, json={"version": 2, "state": "PENDING_VALIDATION"})
    assert pending.status_code == 200 and pending.json()["version"] == 3
    unverified = authenticated.patch(url, json={"version": 3, "state": "RESOLVED", "root_cause": "Invalid source amount", "resolution": "Source file corrected"})
    assert unverified.status_code == 422
    assert unverified.json()["error"]["code"] == "TECHNICAL_VALIDATION_REQUIRED"

    corrected = tmp_path / "corrected-orders.csv"
    corrected.write_text("order_id,amount\nA,12.25\nB,1\nC,2\n", encoding="utf-8")
    with database() as db:
        origin = db.get(Run, queued_intake["run_id"])
        dataset = db.get(DatasetVersion, origin.dataset_version_id)
        corrected_version = create_version(db, db.get(Dataset, dataset.dataset_id), corrected, corrected.name)
        validation_run = enqueue(db, db.get(Configuration, origin.config_id), corrected_version, None, "Test User")
        db.commit()
        validation_run_id = validation_run.id
    assert process_once("validation-worker") is True
    validated = authenticated.get(url).json()
    assert validated["version"] == 4
    assert validated["technical_validation"]["can_resolve"] is True
    assert validated["validation_run_id"] == validation_run_id
    resolved = authenticated.patch(url, json={"version": 4, "state": "RESOLVED", "root_cause": "Invalid source amount", "resolution": "Source file corrected"})
    assert resolved.status_code == 200 and resolved.json()["version"] == 5
    assert resolved.json()["technical_validation"]["status"] == "VALIDATED"
    reopened = authenticated.patch(url, json={"version": 5, "state": "OPEN", "comment": "Recheck"})
    assert reopened.status_code == 200 and reopened.json()["version"] == 6
    no_reason = authenticated.patch(url, json={"version": 6, "state": "ACCEPTED"})
    assert no_reason.status_code == 422
    assert no_reason.json()["error"]["code"] == "ADMINISTRATIVE_REASON_REQUIRED"
    accepted = authenticated.patch(url, json={"version": 6, "state": "ACCEPTED", "administrative_reason": "Riesgo documentado"})
    assert accepted.status_code == 200 and accepted.json()["version"] == 7
    current = authenticated.get(url).json()
    assert current["owner"] == "Analyst"
    assert [(event["from_state"], event["to_state"]) for event in current["events"]] == [
        (None, "OPEN"), ("OPEN", "INVESTIGATING"),
        ("INVESTIGATING", "PENDING_VALIDATION"),
        ("PENDING_VALIDATION", "PENDING_VALIDATION"),
        ("PENDING_VALIDATION", "RESOLVED"), ("RESOLVED", "OPEN"),
        ("OPEN", "ACCEPTED")]
    assert "Stale writer" not in json.dumps(current)
