"""Operational hardening never turns local evidence work into a remote replay."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from test_delivery_service_api import (
    delivery_case as delivery_case,  # noqa: PLC0414 - explicitly re-export pytest fixture.
)
from test_delivery_service_api import (
    delivery_runtime as delivery_runtime,  # noqa: PLC0414 - dependency of delivery_case fixture.
)
from test_delivery_service_api import (
    execute_queued,
    publish_configuration,
    queue_run,
)

from trackvance import __version__, delivery_service, worker
from trackvance.data_sinks import DeliveryError
from trackvance.db import iso, utcnow
from trackvance.delivery_schemas import DeliveryReviewBody
from trackvance.models import (
    Artifact,
    ArtifactLink,
    AuditEvent,
    Configuration,
    DatasetVersion,
    DeliveryAttempt,
    DeliveryDestination,
    DeliveryDestinationVersion,
    DeliveryOperationalReview,
    Job,
    Run,
    User,
)
from trackvance.services import backfill_artifacts


def completed_run(authenticated, database, case, monkeypatch, *, pending=False, unknown=False):
    config = publish_configuration(authenticated, case)
    run = queue_run(authenticated, config["id"], case.source["version_id"], "operations-test")
    if unknown:
        case.runtime.deliver_error = DeliveryError(
            "DESTINATION_COMMIT_UNKNOWN", "Confirmación remota incierta.", ambiguous=True,
        )
    if pending:
        with monkeypatch.context() as patch:
            def fail(*_args, **_kwargs):
                raise OSError("private local path never exposed")
            patch.setattr(delivery_service, "_publish_delivery_evidence", fail)
            execute_queued(database, run["id"])
    else:
        execute_queued(database, run["id"])
    with database() as db:
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run["id"]))
        return run["id"], attempt.id


def repair(client, run_id):
    return client.post(f"/api/v1/delivery/runs/{run_id}/repair-evidence")


def review_body(attempt_id, **values):
    return {
        "delivery_attempt_id": attempt_id, "outcome": "INCONCLUSIVE",
        "note": "Verificación externa documentada sin datos de negocio.", **values,
    }


def record_values(record):
    return {column.name: deepcopy(getattr(record, column.name)) for column in record.__table__.columns}


def assert_no_remote_access(monkeypatch, case):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Local evidence/review must not access any DataSink, preflight or secret.")
    monkeypatch.setattr(delivery_service.sink_registry, "create", forbidden)
    monkeypatch.setattr(delivery_service, "settings_for", forbidden)
    monkeypatch.setattr(delivery_service, "preflight_delivery", forbidden)
    monkeypatch.setattr(case.runtime.store, "get", forbidden)


def test_repair_is_local_idempotent_and_clears_pending_only_after_valid_evidence(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, attempt_id = completed_run(
        authenticated, database, delivery_case, monkeypatch, pending=True,
    )
    with database() as db:
        before_attempt = record_values(db.get(DeliveryAttempt, attempt_id))
        before_run = record_values(db.get(Run, run_id))
        before_job = record_values(db.scalar(select(Job).where(Job.run_id == run_id)))
    assert_no_remote_access(monkeypatch, delivery_case)
    first = repair(authenticated, run_id)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "REPAIRED"
    with database() as db:
        artifacts = db.scalars(select(Artifact).where(
            Artifact.id.in_([first.json()["receipt_artifact_id"], first.json()["manifest_artifact_id"]]),
        )).all()
        before_artifacts = [record_values(item) for item in artifacts]
        link_ids = set(db.scalars(select(ArtifactLink.id)).all())
    second = repair(authenticated, run_id)
    assert second.status_code == 200, second.text
    assert second.json() == {**first.json(), "status": "ALREADY_VALID"}
    with database() as db:
        run = db.get(Run, run_id)
        assert "evidence_status" not in run.metrics
        assert (run.status, run.decision) == ("SUCCESS", "COMMITTED")
        assert run.started_at == before_run["started_at"]
        assert run.finished_at == before_run["finished_at"]
        assert record_values(db.get(DeliveryAttempt, attempt_id)) == before_attempt
        assert record_values(db.scalar(select(Job).where(Job.run_id == run_id))) == before_job
        artifacts = [db.get(Artifact, item["id"]) for item in before_artifacts]
        assert [record_values(item) for item in artifacts] == before_artifacts
        assert set(db.scalars(select(ArtifactLink.id)).all()) == link_ids
        payloads = {item.kind: json.loads(Path(item.path).read_text(encoding="utf-8")) for item in artifacts}
        receipt, manifest = payloads["DELIVERY_RECEIPT"], payloads["RUN_MANIFEST"]
        assert receipt["delivery_attempt_id"] == attempt_id
        assert receipt["metric_semantics"]["physical_destination_rows"] == "NOT_MEASURED"
        assert manifest["engine_version"] == __version__
        for key in ("preflight_seconds", "write_seconds"):
            assert receipt[key] == run.metrics[key] >= 0
            assert manifest["metrics"][key] == run.metrics[key]
        assert "evidence_status" not in manifest["metrics"]
        events = db.scalars(select(AuditEvent).where(AuditEvent.run_id == run_id)).all()
        assert sum(item.event_type == "DELIVERY_EVIDENCE_REPAIR_STARTED" for item in events) == 2
        assert sum(item.event_type == "DELIVERY_EVIDENCE_REPAIRED" for item in events) == 2
        assert all(item.actor_id == "test-user" for item in events if "REPAIR" in item.event_type)


def test_interrupted_repair_reuses_unregistered_bytes_and_does_not_duplicate_receipts(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, _ = completed_run(authenticated, database, delivery_case, monkeypatch, pending=True)
    assert_no_remote_access(monkeypatch, delivery_case)
    original = delivery_service._write_json_artifact
    receipt_hashes = []

    def fail_manifest(db, run, kind, name, payload, **kwargs):
        if kind == "RUN_MANIFEST":
            raise OSError("secret=password should never leave this boundary")
        result = original(db, run, kind, name, payload, **kwargs)
        receipt_hashes.append(result.sha256)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(delivery_service, "_write_json_artifact", fail_manifest)
        response = repair(authenticated, run_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DELIVERY_EVIDENCE_REPAIR_FAILED"
    assert "password" not in response.text
    with database() as db:
        assert db.get(Run, run_id).metrics["evidence_status"] == "PENDING_REPAIR"
        assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.kind == "DELIVERY_RECEIPT")) == 0
        failed = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "DELIVERY_EVIDENCE_REPAIR_FAILED"))
        assert failed is not None and "password" not in json.dumps(failed.metadata_json)
    response = repair(authenticated, run_id)
    assert response.status_code == 200, response.text
    with database() as db:
        assert db.get(Artifact, response.json()["receipt_artifact_id"]).sha256 == receipt_hashes[0]
        assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.kind == "DELIVERY_RECEIPT")) == 1


def test_worker_partial_publication_and_repair_use_identical_artifact_identity(
    authenticated, database, delivery_case, monkeypatch,
):
    original = delivery_service._write_json_artifact
    receipts = []

    def fail_manifest(db, run, kind, name, payload, **kwargs):
        if kind == "RUN_MANIFEST":
            raise OSError("interrupted local publication")
        item = original(db, run, kind, name, payload, **kwargs)
        receipts.append((item.id, item.sha256))
        return item

    with monkeypatch.context() as patch:
        patch.setattr(delivery_service, "_write_json_artifact", fail_manifest)
        run_id, _ = completed_run(authenticated, database, delivery_case, patch)
    response = repair(authenticated, run_id)
    assert response.status_code == 200, response.text
    with database() as db:
        receipt = db.get(Artifact, response.json()["receipt_artifact_id"])
        assert (receipt.id, receipt.sha256) == receipts[0]


@pytest.mark.parametrize("missing", ["manifest_metadata", "receipt_links", "manifest_file", "receipt_file"])
def test_repair_partial_evidence_preserves_existing_identity_and_bytes(
    authenticated, database, delivery_case, monkeypatch, missing,
):
    run_id, _ = completed_run(authenticated, database, delivery_case, monkeypatch)
    with database() as db:
        run = db.get(Run, run_id)
        receipt = db.get(Artifact, run.metrics["receipt_artifact_id"])
        manifest = db.scalar(select(Artifact).where(Artifact.path == run.evidence_path))
        identities = receipt.id, manifest.id
        hashes = receipt.sha256, manifest.sha256
        if missing == "manifest_metadata":
            db.execute(delete(ArtifactLink).where(ArtifactLink.target_id == manifest.id))
            db.delete(manifest)
            run.evidence_path = None
        elif missing == "receipt_links":
            db.execute(delete(ArtifactLink).where(
                (ArtifactLink.target_id == receipt.id) | (ArtifactLink.source_id == receipt.id),
            ))
        elif missing == "manifest_file":
            Path(manifest.path).unlink()
        else:
            Path(receipt.path).unlink()
        run.metrics = {**run.metrics, "evidence_status": "PENDING_REPAIR"}
        db.commit()
    assert_no_remote_access(monkeypatch, delivery_case)
    response = repair(authenticated, run_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REPAIRED"
    assert (response.json()["receipt_artifact_id"], response.json()["manifest_artifact_id"]) == identities
    with database() as db:
        assert (db.get(Artifact, identities[0]).sha256, db.get(Artifact, identities[1]).sha256) == hashes
        assert len(db.scalars(select(ArtifactLink).where(
            ArtifactLink.relation == "DELIVERY_RECEIPT", ArtifactLink.source_id == run_id,
        )).all()) == 1


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_existing_050_evidence_is_preserved_and_missing_bytes_reconstruct_exactly(
    authenticated, database, delivery_case, monkeypatch, newline,
):
    publisher = delivery_service._publish_delivery_evidence

    def legacy_artifact(db, run, kind, name, payload):
        payload = deepcopy(payload)
        payload.pop("metric_semantics", None)
        return delivery_service._write_json_artifact(
            db, run, kind, name, payload, newline=newline,
        )

    def legacy_publish(db, run, *args):
        # Construct exact pre-0.5.1 fields with random artifact IDs, no timing or
        # source identity additions, and either historical platform newline.
        run.execution_plan = {key: value for key, value in run.execution_plan.items()
                              if key not in {"source_identity", "evidence_engine_version"}}
        run.metrics = {key: value for key, value in run.metrics.items()
                       if key not in {"metric_semantics", "preflight_seconds", "write_seconds"}}
        with monkeypatch.context() as patch:
            patch.setattr(delivery_service, "_publish_evidence_artifact", legacy_artifact)
            return publisher(db, run, *args)

    with monkeypatch.context() as patch:
        patch.setattr(delivery_service, "_publish_delivery_evidence", legacy_publish)
        run_id, _ = completed_run(authenticated, database, delivery_case, monkeypatch)
    with database() as db:
        run = db.get(Run, run_id)
        receipt = db.get(Artifact, run.metrics["receipt_artifact_id"])
        manifest = db.scalar(select(Artifact).where(Artifact.path == run.evidence_path))
        original = {item.id: (item.path, Path(item.path).read_bytes(), item.sha256)
                    for item in (receipt, manifest)}
        assert json.loads(original[manifest.id][1])["engine_version"] == "0.5.0"
        assert "metric_semantics" not in run.metrics
        assert receipt.id != delivery_service._evidence_identity(run, "DELIVERY_RECEIPT")
    assert_no_remote_access(monkeypatch, delivery_case)
    valid = repair(authenticated, run_id)
    assert valid.status_code == 200, valid.text
    assert valid.json()["status"] == "ALREADY_VALID"
    for path, _content, _digest in original.values():
        Path(path).unlink()
    repaired = repair(authenticated, run_id)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json() == {**valid.json(), "status": "REPAIRED"}
    with database() as db:
        assert "metric_semantics" not in db.get(Run, run_id).metrics
        for identity, (path, content, digest) in original.items():
            assert Path(path).read_bytes() == content
            assert db.get(Artifact, identity).sha256 == digest


@pytest.mark.parametrize("damage", [
    "canonical_bytes", "canonical_hash", "source_schema", "source_hash", "config_hash",
    "destination_hash", "destination_scope", "source_scope", "attempt_scope",
    "attempt_target", "attempt_metrics", "run_metrics", "attempt_unknown", "missing_attempt",
])
def test_repair_fails_closed_when_persisted_evidence_is_not_verifiable(
    authenticated, database, delivery_case, monkeypatch, damage,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, pending=True)
    with database() as db:
        run, attempt = db.get(Run, run_id), db.get(DeliveryAttempt, attempt_id)
        source = db.get(DatasetVersion, run.dataset_version_id)
        canonical = db.get(Artifact, source.canonical_artifact_id)
        destination = db.get(DeliveryDestinationVersion, attempt.destination_version_id)
        if damage == "canonical_bytes":
            Path(canonical.path).write_bytes(b"corrupt canonical")
        elif damage == "canonical_hash":
            canonical.sha256 = "0" * 64
        elif damage == "source_schema":
            source.schema_hash = "0" * 64
        elif damage == "source_hash":
            source.sha256 = "0" * 64
        elif damage == "config_hash":
            db.get(Configuration, run.config_id).config = {**db.get(Configuration, run.config_id).config, "write_strategy": "OVERWRITE"}
        elif damage == "destination_hash":
            destination.config_hash = "0" * 64
        elif damage == "destination_scope":
            destination.organization_id = "another-org"
        elif damage == "source_scope":
            source.organization_id = "another-org"
        elif damage == "attempt_scope":
            attempt.organization_id = "another-org"
        elif damage == "attempt_target":
            attempt.target_locator = "changed.target"
        elif damage == "attempt_metrics":
            attempt.rows_written = 999
        elif damage == "run_metrics":
            run.metrics = {**run.metrics, "rows_written": 999}
        elif damage == "attempt_unknown":
            attempt.status = "UNKNOWN"
        else:
            db.delete(attempt)
        db.commit()
    assert_no_remote_access(monkeypatch, delivery_case)
    response = repair(authenticated, run_id)
    assert response.status_code in {409, 412}, response.text
    with database() as db:
        run = db.get(Run, run_id)
        assert (run.status, run.decision) == ("SUCCESS", "COMMITTED")
        assert run.metrics["evidence_status"] == "PENDING_REPAIR"
        assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.kind == "DELIVERY_RECEIPT")) == 0


@pytest.mark.parametrize("status", ["FAILED", "UNKNOWN", "RUNNING", "FAILED_PRECONDITION"])
def test_repair_rejects_non_committed_runs(authenticated, database, delivery_case, monkeypatch, status):
    run_id, _ = completed_run(authenticated, database, delivery_case, monkeypatch, pending=True)
    with database() as db:
        run = db.get(Run, run_id)
        run.status, run.decision = status, status
        db.commit()
    assert repair(authenticated, run_id).status_code == 409


def test_disabled_deleted_or_renamed_destination_does_not_block_local_repair(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, _ = completed_run(authenticated, database, delivery_case, monkeypatch, pending=True)
    with database() as db:
        destination = db.get(DeliveryDestination, delivery_case.destination["id"])
        destination.enabled, destination.deleted, destination.name = False, True, "Later display name"
        db.commit()
    assert_no_remote_access(monkeypatch, delivery_case)
    response = repair(authenticated, run_id)
    assert response.status_code == 200, response.text
    receipt = authenticated.get(f"/api/v1/delivery/runs/{run_id}/receipt").json()
    assert receipt["destination_name"] == "Certified warehouse"


def test_concurrent_sqlite_repairs_publish_one_logical_evidence_pair(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, _ = completed_run(authenticated, database, delivery_case, monkeypatch, pending=True)
    assert_no_remote_access(monkeypatch, delivery_case)
    barrier = Barrier(2)

    def concurrent_repair():
        with database() as db:
            user = db.get(User, "test-user")
            db.commit()
            barrier.wait(timeout=10)
            return delivery_service.repair_delivery_evidence(db, user, run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: concurrent_repair(), range(2)))
    assert {item["status"] for item in results} == {"REPAIRED", "ALREADY_VALID"}
    assert len({item["receipt_artifact_id"] for item in results}) == 1
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Artifact).where(
            Artifact.kind.in_({"DELIVERY_RECEIPT", "RUN_MANIFEST"}),
        )) == 2


@pytest.mark.parametrize("outcome", ["REMOTE_COMMIT_OBSERVED", "REMOTE_NOT_COMMITTED_OBSERVED", "INCONCLUSIVE"])
def test_unknown_review_is_structured_audited_and_does_not_modify_history_or_replay(
    authenticated, database, delivery_case, monkeypatch, outcome,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, unknown=True)
    with database() as db:
        originals = {model: record_values(db.scalar(select(model).where(
            model.id == run_id if model is Run else model.run_id == run_id,
        ))) for model in (Run, Job, DeliveryAttempt)}
    assert_no_remote_access(monkeypatch, delivery_case)
    verified_at = iso(utcnow())
    body = review_body(attempt_id, outcome=outcome, verified_at=verified_at)
    response = authenticated.post(f"/api/v1/delivery/runs/{run_id}/reviews", json=body)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["outcome"] == outcome
    assert item["reviewer_id"] == "test-user" and item["reviewer_name"] == "Test User"
    assert item["verified_at"] == verified_at
    assert authenticated.get(f"/api/v1/delivery/runs/{run_id}/reviews").json() == {"items": [item], "total": 1}
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        for model, original in originals.items():
            assert record_values(db.get(model, original["id"])) == original
        event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "DELIVERY_UNKNOWN_REVIEWED"))
        assert event.actor_id == "test-user"
        assert event.metadata_json["outcome"] == outcome
        assert event.metadata_json["delivery_attempt_id"] == attempt_id
        assert "note" not in event.metadata_json and body["note"] not in event.message
    assert repair(authenticated, run_id).status_code == 409


def test_review_history_is_append_only_and_reviewer_name_is_a_snapshot(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, unknown=True)
    route = f"/api/v1/delivery/runs/{run_id}/reviews"
    first = authenticated.post(route, json=review_body(attempt_id)).json()
    with database() as db:
        db.get(User, "test-user").name = "Renamed reviewer"
        db.commit()
    second = authenticated.post(route, json=review_body(attempt_id, outcome="REMOTE_COMMIT_OBSERVED")).json()
    assert first["id"] != second["id"]
    items = authenticated.get(route).json()["items"]
    assert items == [first, second]
    assert items[0]["reviewer_name"] == "Test User"
    assert items[1]["reviewer_name"] == "Renamed reviewer"
    assert authenticated.patch(route, json={"outcome": "INCONCLUSIVE"}).status_code == 405
    assert authenticated.delete(route).status_code == 405


@pytest.mark.parametrize("values", [
    {"outcome": "COMMITTED"}, {"outcome": "FAILED"}, {"note": "   "}, {"note": "x" * 4001},
    {"verified_at": "2026-09-01T10:00:00"}, {"verified_at": 12345},
    {"verified_at": "2999-01-01T00:00:00Z"}, {"verified_at": "2000-01-01T00:00:00Z"},
    {"reviewer_id": "untrusted-actor"},
])
def test_review_rejects_invalid_outcomes_dates_notes_and_forged_reviewer(
    authenticated, database, delivery_case, monkeypatch, values,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, unknown=True)
    response = authenticated.post(f"/api/v1/delivery/runs/{run_id}/reviews", json=review_body(attempt_id, **values))
    assert response.status_code == 422, response.text
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DeliveryOperationalReview)) == 0


@pytest.mark.parametrize("role", ["Operations", "Auditor"])
def test_operational_mutations_require_execute_permission(
    authenticated, database, delivery_case, monkeypatch, role,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, unknown=True)
    with database() as db:
        db.get(User, "test-user").role = role
        db.commit()
    assert repair(authenticated, run_id).status_code == 403
    assert authenticated.post(f"/api/v1/delivery/runs/{run_id}/reviews", json=review_body(attempt_id)).status_code == 403
    assert authenticated.get(f"/api/v1/delivery/runs/{run_id}/reviews").status_code == 200


def test_operational_endpoints_require_csrf_and_scope_resources(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, unknown=True)
    token = authenticated.headers.pop("X-CSRF-Token")
    assert repair(authenticated, run_id).status_code == 403
    assert authenticated.post(f"/api/v1/delivery/runs/{run_id}/reviews", json=review_body(attempt_id)).status_code == 403
    authenticated.headers["X-CSRF-Token"] = token
    with database() as db:
        db.get(User, "test-user").organization_id = "different-org"
        db.commit()
    assert repair(authenticated, run_id).status_code == 404
    assert authenticated.get(f"/api/v1/delivery/runs/{run_id}/reviews").status_code == 404
    assert authenticated.post(f"/api/v1/delivery/runs/{run_id}/reviews", json=review_body(attempt_id)).status_code == 404


def test_review_requires_matching_unknown_attempt_and_preserves_unknown_worker_record(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, unknown=True)
    with database() as db:
        attempt = db.get(DeliveryAttempt, attempt_id)
        attempt.error_code, attempt.error_message, attempt.finished_at = None, None, None
        db.commit()
        original = record_values(attempt)
        worker._reconcile_durable_delivery_state(db, db.scalar(select(Job).where(Job.run_id == run_id)), db.get(Run, run_id))
        db.commit()
        assert record_values(attempt) == original
    assert authenticated.post(f"/api/v1/delivery/runs/{run_id}/reviews", json=review_body("different-attempt")).status_code == 404
    with database() as db:
        db.get(DeliveryAttempt, attempt_id).status = "COMMITTED"
        db.commit()
    assert authenticated.post(f"/api/v1/delivery/runs/{run_id}/reviews", json=review_body(attempt_id)).status_code == 409


def test_review_utc_normalization_and_database_constraints(
    authenticated, database, delivery_case, monkeypatch,
):
    run_id, attempt_id = completed_run(authenticated, database, delivery_case, monkeypatch, unknown=True)
    value = utcnow().astimezone(timezone(timedelta(hours=-5))).isoformat()
    body = DeliveryReviewBody.model_validate(review_body(attempt_id, verified_at=value))
    assert body.verified_at.utcoffset() == timedelta(0)
    with database() as db:
        for changes in ({"outcome": "COMMITTED"}, {"reviewer_id": "missing-user"}, {"delivery_attempt_id": "missing-attempt"}, {"run_id": "missing-run"}):
            with pytest.raises(IntegrityError), db.begin_nested():
                db.add(DeliveryOperationalReview(
                    **{"organization_id": db.get(Run, run_id).organization_id,
                       "run_id": run_id, "delivery_attempt_id": attempt_id, "reviewer_id": "test-user",
                       "reviewer_name": "Test User", "outcome": "INCONCLUSIVE", "note": "test",
                       "verified_at": utcnow(), **changes},
                ))
                db.flush()


@pytest.mark.parametrize("unknown", [False, True])
def test_startup_legacy_backfill_does_not_add_noncanonical_delivery_edges(
    authenticated, database, delivery_case, monkeypatch, unknown,
):
    run_id, attempt_id = completed_run(
        authenticated, database, delivery_case, monkeypatch, unknown=unknown,
    )
    with database() as db:
        before_run = record_values(db.get(Run, run_id))
        before_attempt = record_values(db.get(DeliveryAttempt, attempt_id))
        before_links = [record_values(link) for link in db.scalars(
            select(ArtifactLink).order_by(ArtifactLink.id),
        )]
        before_artifacts = [record_values(artifact) for artifact in db.scalars(
            select(Artifact).order_by(Artifact.id),
        )]
        for _ in range(2):
            backfill_artifacts(db)
            db.commit()
        assert record_values(db.get(Run, run_id)) == before_run
        assert record_values(db.get(DeliveryAttempt, attempt_id)) == before_attempt
        assert [record_values(link) for link in db.scalars(
            select(ArtifactLink).order_by(ArtifactLink.id),
        )] == before_links
        assert [record_values(artifact) for artifact in db.scalars(
            select(Artifact).order_by(Artifact.id),
        )] == before_artifacts
