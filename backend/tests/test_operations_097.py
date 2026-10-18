"""OPS-097-01/02: personal reading and process projection over real metadata."""
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from test_exception_validation import make_case as make_native_case

from trackvance.acquisition_schedule_models import (
    AcquisitionOccurrence,
    AcquisitionSourceConfiguration,
    AcquisitionSourceRevision,
)
from trackvance.automation_models import InternalNotification, OutboxEvent
from trackvance.db import iso, utcnow
from trackvance.events import append_event, consume_notification
from trackvance.governance import (
    authorize_dataset_content,
    authorize_run_content,
    read_authorization_scope,
)
from trackvance.governance_models import DatasetBlock
from trackvance.models import DatasetVersion, Role, RolePermission, Run, User
from trackvance.operation_processes import process_identity
from trackvance.operations_common import OperationError


def make_case(db, module, **options):
    case, config, candidate, _, version = make_native_case(db, module, **options)
    return case, config, candidate, db.get(Run, case.run_id), version


def notification(db, run, suffix, *, created_at=None, read=False):
    event = append_event(db, organization_id=run.organization_id, dedupe_key=f"ops097:{run.id}:{suffix}",
        event_type="RUN_TERMINAL", aggregate_type="RUN", aggregate_id=run.id, module=run.module,
        payload={"recipient_user_id": "test-user", "status": run.status, "decision": run.decision, "origin": "MANUAL"})
    if created_at:
        event.created_at = created_at
    consume_notification(db, event)
    db.flush()
    item = db.query(InternalNotification).filter_by(event_id=event.id).one()
    if read:
        item.read_at = utcnow()
    return item


def test_grouped_feed_collapses_cases_but_preserves_independent_manual_runs(authenticated, database):
    with database() as db:
        case, _, candidate, origin, _ = make_case(db, "intake", candidate_decision="APPROVED")
        db.commit()
        origin_id, candidate_id, case_id = origin.id, candidate.id, case.id
    response = authenticated.get("/api/v1/operations?grouped=true")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    groups = {item["process_id"]: item for item in items}
    assert set(groups) == {f"run:{origin_id}", f"run:{candidate_id}"}
    assert f"exception:{case_id}" in {stage["id"] for stage in groups[f"run:{origin_id}"]["stages"]}
    rejected = groups[f"run:{origin_id}"]
    assert rejected["technical_state"] == "SUCCESS" and rejected["functional_result"] == "REJECTED"
    assert rejected["intervention_required"] and rejected["impact_level"] == "CONFIRMED"
    with database() as db:
        from trackvance.models import ExceptionCase

        db.get(ExceptionCase, case_id).state = "WAITING_EXTERNAL"
        db.commit()
    waiting = authenticated.get("/api/v1/operations?grouped=true&queue=waiting").json()["items"]
    assert len(waiting) == 1 and waiting[0]["process_id"] == f"run:{origin_id}"
    assert "waiting" in waiting[0]["queues"] and waiting[0]["functional_result"] == "REJECTED"


def test_publication_automatic_stage_requires_exact_event_and_never_parent_history(database):
    with database() as db:
        _, _, candidate, origin, version = make_case(db, "sentinel", candidate_decision="HEALTHY")
        version.source_run_id = origin.id
        # The producer uses this same version in this synthetic fixture. The
        # bounded projection detects the cycle instead of walking arbitrary history.
        db.flush()
        assert process_identity(db, origin.organization_id, "run", candidate.id) == f"run:{candidate.id}"
        event = OutboxEvent(dedupe_key="ops097-publication", event_type="DATASET_VERSION_PUBLISHED",
            aggregate_type="DATASET_VERSION", aggregate_id=version.id, module="datasets", payload={})
        db.add(event)
        db.flush()
        candidate.execution_plan = {"sentinel_automatic": {"event_id": event.id}}
        assert process_identity(db, origin.organization_id, "run", candidate.id) == f"run:{origin.id}"
        candidate.execution_plan = {"sentinel_automatic": {"event_id": "missing-event"}}
        assert process_identity(db, origin.organization_id, "run", candidate.id) == f"run:{candidate.id}"
        origin.execution_plan = candidate.execution_plan = {"sentinel_automatic": {"event_id": event.id}}
        with read_authorization_scope(db):
            db.info["operation_process_identities"][(origin.organization_id, "run", origin.id)] = f"run:{origin.id}"
            assert process_identity(db, origin.organization_id, "run", candidate.id) == f"run:{candidate.id}"
        origin.execution_plan = {}
        version.parent_version_id = "missing-previous-snapshot"
        version.source_run_id = None
        assert process_identity(db, origin.organization_id, "version", version.id) == f"version:{version.id}"


def test_blocked_occurrence_without_acquisition_is_visible_and_personal_without_exception_access(authenticated, database):
    with database() as db:
        _, _, _, _, version = make_case(db, "intake", candidate_decision="APPROVED")
        config = AcquisitionSourceConfiguration(dataset_id=version.dataset_id)
        db.add(config)
        db.flush()
        revision = AcquisitionSourceRevision(configuration_id=config.id, version=1, actor_id="test-user",
            settings={"responsible_user_id": "test-user"})
        db.add(revision)
        db.flush()
        occurrence = AcquisitionOccurrence(configuration_id=config.id, revision_id=revision.id, planned_at=utcnow(),
            status="BLOCKED", reason_code="SOURCE_UNAVAILABLE", reason_message="La conexión está deshabilitada.")
        db.add(occurrence)
        db.flush()
        event = append_event(db, organization_id=occurrence.organization_id, dedupe_key=f"occurrence:{occurrence.id}",
            event_type="ACQUISITION_SCHEDULE_BLOCKED", aggregate_type="ACQUISITION_OCCURRENCE", aggregate_id=occurrence.id,
            module="acquisition", payload={"recipient_user_id": "test-user", "dataset_id": version.dataset_id,
                "origin": "SCHEDULED", "status": "BLOCKED", "reason_message": occurrence.reason_message})
        consume_notification(db, event)
        db.commit()
        identity = occurrence.id
    items = authenticated.get("/api/v1/operations?grouped=true&queue=intervention").json()["items"]
    item = next(item for item in items if item["process_id"] == f"occurrence:{identity}")
    assert item["functional_result"] == "NOT_EXECUTED" and len(item["stages"]) == 1
    assert "no ejecutó una consulta" in item["impact"]
    with database() as db:
        role = Role(name="Personal reader", normalized_name="personal reader")
        db.add(role)
        db.flush()
        for permission in ("notifications:read", "datasets:read"):
            db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.get(User, "test-user").role_id = role.id
        db.commit()
    scoped = authenticated.get("/api/v1/operations?grouped=true")
    assert scoped.status_code == 200 and len(scoped.json()["items"]) == 1
    assert all(not stage["id"].startswith("exception:") for stage in scoped.json()["items"][0]["stages"])
    assert authenticated.get("/api/v1/exceptions").status_code == 403
    response = authenticated.get("/api/v1/notifications/inbox?grouped=true")
    assert response.status_code == 200, response.text
    group = response.json()["groups"][0]
    assert group["process_id"] == f"occurrence:{identity}" and group["unread_count"] == 1
    assert group["items"][0]["detail_url"] == f"/operations?view=news&process=occurrence:{identity}"


def test_snapshot_read_excludes_later_arrivals_even_with_old_event_timestamps(authenticated, database):
    with database() as db:
        _, _, _, origin, _ = make_case(db, "intake", candidate_decision="APPROVED")
        first = notification(db, origin, "first")
        db.commit()
        first_id, run_id = first.id, origin.id
    shown = authenticated.get("/api/v1/notifications/inbox?grouped=true").json()
    snapshot = [item["id"] for group in shown["groups"] for item in group["items"]]
    assert snapshot == [first_id]
    with database() as db:
        later = notification(db, db.get(Run, run_id), "later", created_at=utcnow() - timedelta(days=1))
        db.commit()
        later_id = later.id
    response = authenticated.post("/api/v1/notifications/inbox/read-all", json={"ids": snapshot})
    assert response.status_code == 200 and response.json()["marked_count"] == 1
    assert authenticated.get("/api/v1/notifications/unread-count").json()["unread_count"] == 1
    with database() as db:
        assert db.get(InternalNotification, first_id).read_at and db.get(InternalNotification, later_id).read_at is None
    read_group = authenticated.get("/api/v1/notifications/inbox?grouped=true&read_state=READ").json()["groups"][0]
    assert read_group["unread_count"] == 1  # An earlier stage being read cannot conceal a new personal event.


def test_governance_applies_before_groups_counts_search_links_and_reading(authenticated, database):
    with database() as db:
        _, _, _, origin, version = make_case(db, "intake", candidate_decision="APPROVED")
        item = notification(db, origin, "restricted")
        db.add(DatasetBlock(dataset_id=version.dataset_id, scope="CONTENT", reason="Synthetic restriction", created_by_id="test-user"))
        db.commit()
        identity = item.id
    response = authenticated.get("/api/v1/notifications/inbox?grouped=true")
    assert response.json()["groups"] == [] and response.json()["total"] == 0
    assert authenticated.get("/api/v1/notifications/unread-count").json()["unread_count"] == 0
    assert authenticated.post(f"/api/v1/notifications/inbox/{identity}/read", json={}).status_code == 404
    assert authenticated.post("/api/v1/notifications/inbox/read-all", json={"ids": [identity]}).json()["marked_count"] == 0


def test_group_pagination_and_filters_keep_process_identity_and_native_results(authenticated, database):
    with database() as db:
        _, _, candidate, origin, _ = make_case(db, "intake", candidate_decision="APPROVED")
        notification(db, origin, "old", created_at=utcnow() - timedelta(hours=1))
        notification(db, candidate, "other", created_at=utcnow() - timedelta(minutes=1))
        notification(db, origin, "new")
        db.commit()
    first = authenticated.get("/api/v1/notifications/inbox?grouped=true&limit=1").json()
    second = authenticated.get("/api/v1/notifications/inbox?grouped=true&limit=1&offset=1").json()
    assert first["total"] == second["total"] == 2
    assert len(first["groups"][0]["items"]) == 2
    assert first["groups"][0]["process_id"] != second["groups"][0]["process_id"]
    after = iso(utcnow() - timedelta(minutes=5))
    filtered = authenticated.get("/api/v1/notifications/inbox", params={"grouped": "true", "decision": "REJECTED", "from_date": after}).json()
    assert filtered["total"] == 1 and len(filtered["groups"][0]["items"]) == 1


def test_unregistered_schedule_diagnostic_uses_its_actual_activity_date_and_never_fabricates_occurrence(authenticated, database):
    now = utcnow()
    with database() as db:
        _, _, _, _, version = make_case(db, "intake", candidate_decision="APPROVED")
        config = AcquisitionSourceConfiguration(dataset_id=version.dataset_id, created_at=now - timedelta(days=30),
            dispatch_diagnostic={"code": "SOURCE_REVISION_MISSING", "message": "Falta la revisión guardada.",
                "recorded_at": iso(now), "planned_at": iso(now)})
        db.add(config)
        db.commit()
        identity = config.id
    response = authenticated.get("/api/v1/operations", params={"grouped": "true", "module": "acquisition", "from_date": iso(now - timedelta(days=1))})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["process_id"] == f"acquisition_schedule:{identity}" and item["functional_result"] == "NOT_DISPATCHED"
    assert item["stages"] == [] and item["created_at"] == iso(now)
    direct = authenticated.get("/api/v1/operations", params={"grouped": "true", "process": item["process_id"],
        "from_date": iso(now + timedelta(days=1))})
    assert direct.json()["items"] == []


def test_version_related_view_filters_native_relations_before_paging_and_never_grants_cases(authenticated, database):
    with database() as db:
        case, config, candidate, origin, version = make_case(db, "intake", candidate_decision="APPROVED")
        later = DatasetVersion(dataset_id=version.dataset_id, version=2, filename="later-full-snapshot.parquet",
            source_type="UPLOAD", sha256="c" * 64, schema_hash=version.schema_hash, size_bytes=1, row_count=1,
            column_count=1, original_path="", canonical_path="unused-later.parquet", schema_json=[], profile={})
        db.add(later)
        db.flush()
        producer = Run(module="intake", name="Recorded direct producer", status="SUCCESS", decision="APPROVED",
            config_id=config.id, dataset_version_id=later.id, output_version_id=version.id, initiated_by="Test User")
        db.add(producer)
        db.flush()
        version.source_run_id = producer.id
        db.add_all([Run(module="intake", name=f"Unrelated later execution {index}", status="SUCCESS", decision="APPROVED",
            config_id=config.id, dataset_version_id=later.id, initiated_by="Test User") for index in range(250)])
        role = Role(name="Resource operations reader", normalized_name="resource operations reader")
        db.add(role)
        db.flush()
        for permission in ("datasets:read", "intake:read"):
            db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.get(User, "test-user").role_id = role.id
        db.commit()
        version_id, dataset_id, case_id = version.id, version.dataset_id, case.id
        wanted = {f"run:{run.id}" for run in (origin, candidate, producer)}
    response = authenticated.get("/api/v1/operations", params={"grouped": "true", "dataset_id": dataset_id, "version_id": version_id})
    assert response.status_code == 200, response.text
    result = response.json()
    assert {item["process_id"] for item in result["items"]} == wanted
    assert result["scanned_records"] == 3 and not result["has_more"]
    assert case_id not in response.text and "/exceptions" not in response.text
    assert all(not any(stage["id"].startswith("exception:") for stage in item["stages"]) for item in result["items"])
    assert authenticated.get(f"/api/v1/exceptions/{case_id}").status_code == 403
    assert authenticated.get("/api/v1/notifications/inbox").status_code == 403
    mismatch = authenticated.get("/api/v1/operations", params={"version_id": version_id, "dataset_id": "different-dataset"})
    assert mismatch.json()["items"] == [] and mismatch.json()["scanned_records"] == 0
    with database() as db:
        db.add(DatasetBlock(dataset_id=dataset_id, scope="CONTENT", reason="Synthetic source restriction", created_by_id="test-user"))
        db.commit()
    denied = authenticated.get("/api/v1/operations", params={"grouped": "true", "version_id": version_id})
    assert denied.json()["items"] == [] and denied.json()["scanned_records"] == 0


def test_related_process_anchor_precedes_pagination_for_interleaved_automatic_stages(authenticated, database):
    now = utcnow()
    with database() as db:
        _, config, candidate, producer, version = make_case(db, "sentinel", candidate_decision="HEALTHY")
        other = DatasetVersion(dataset_id=version.dataset_id, version=2, filename="other.parquet", source_type="UPLOAD",
            sha256="d" * 64, schema_hash=version.schema_hash, size_bytes=1, row_count=1, column_count=1,
            original_path="", canonical_path="unused.parquet", schema_json=[], profile={})
        db.add(other)
        db.flush()
        producer.dataset_version_id, candidate.dataset_version_id = other.id, other.id
        producer.output_version_id, version.source_run_id = version.id, producer.id
        producer.created_at = now - timedelta(minutes=4)
        event = OutboxEvent(dedupe_key="ops097-interleaved-publication", event_type="DATASET_VERSION_PUBLISHED",
            aggregate_type="DATASET_VERSION", aggregate_id=version.id, module="datasets", payload={})
        db.add(event)
        db.flush()
        automatic = [Run(module="sentinel", name=f"Automatic stage {index}", status="SUCCESS", decision="HEALTHY",
            config_id=config.id, dataset_version_id=version.id, initiated_by="Test User",
            execution_plan={"sentinel_automatic": {"event_id": event.id}},
            created_at=now - timedelta(minutes=index * 2)) for index in range(2)]
        manual = Run(module="sentinel", name="Independent manual consumer", status="SUCCESS", decision="HEALTHY",
            config_id=config.id, dataset_version_id=version.id, initiated_by="Test User", created_at=now - timedelta(minutes=1))
        db.add_all([*automatic, manual])
        role = Role(name="Related process reader", normalized_name="related process reader")
        db.add(role)
        db.flush()
        for permission in ("datasets:read", "sentinel:read"):
            db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.get(User, "test-user").role_id = role.id
        db.commit()
        version_id, producer_id, manual_id = version.id, producer.id, manual.id
        automatic_ids = {f"run:{run.id}" for run in automatic}
    params = {"grouped": "true", "version_id": version_id, "limit": 1}
    first = authenticated.get("/api/v1/operations", params=params).json()
    second = authenticated.get("/api/v1/operations", params={**params, "offset": first["next_offset"]}).json()
    exhausted = authenticated.get("/api/v1/operations", params={**params, "offset": 2}).json()
    assert [page["items"][0]["process_id"] for page in (first, second)] == [f"run:{producer_id}", f"run:{manual_id}"]
    assert automatic_ids <= {stage["id"] for stage in first["items"][0]["stages"]}
    assert first["has_more"] and first["next_offset"] == 1 and first["scanned_records"] == 1
    assert not second["has_more"] and second["next_offset"] is None and second["scanned_records"] == 1
    assert exhausted["items"] == [] and not exhausted["has_more"] and exhausted["scanned_records"] == 0


def test_populated_personal_process_batches_identity_and_anchor_authorization(authenticated, database):
    with database() as db:
        _, _, _, origin, version = make_case(db, "intake", candidate_decision="APPROVED")
        for index in range(925):
            notification(db, origin, f"populated-{index}")
        db.commit()
        identity, dataset_id = origin.id, version.dataset_id
        engine = db.get_bind()
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for suffix in (f"&process=run:{identity}", ""):
            response = authenticated.get("/api/v1/notifications/inbox?grouped=true" + suffix)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["total"] == 1 and len(result["groups"]) == 1
            group = result["groups"][0]
            assert group["process_id"] == f"run:{identity}"
            assert group["notification_count"] == group["unread_count"] == 925
            assert len(group["items"]) == 25 and group["more_notifications"]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    # Authorization and canonical identity must scale with distinct resources,
    # not execute the complete causal graph once for each personal event.
    assert len(statements) < 240
    assert sum(statement.startswith("WITH RECURSIVE") for statement in statements) <= 2
    with database() as db:
        db.add(DatasetBlock(dataset_id=dataset_id, scope="CONTENT", reason="Synthetic restriction", created_by_id="test-user"))
        db.commit()
    hidden = authenticated.get(f"/api/v1/notifications/inbox?grouped=true&process=run:{identity}").json()
    assert hidden["groups"] == [] and hidden["total"] == 0
    assert authenticated.get("/api/v1/notifications/unread-count").json()["unread_count"] == 0


def test_personal_read_snapshot_never_carries_actor_permissions_or_blocks_between_http_requests(authenticated, database):
    with database() as db:
        _, _, _, origin, version = make_case(db, "intake", candidate_decision="APPROVED")
        notification(db, origin, "actor-first-1")
        notification(db, origin, "actor-first-2")
        roles = [Role(name=f"Personal actor {index}", normalized_name=f"personal actor {index}") for index in range(2)]
        db.add_all(roles)
        db.flush()
        for role in roles:
            for permission in ("notifications:read", "datasets:read", "intake:read"):
                db.add(RolePermission(role_id=role.id, permission_code=permission))
        db.get(User, "test-user").role_id = roles[0].id
        other = User(id="ops097-other", role_id=roles[1].id, name="Other synthetic reader", username="ops097-other",
            email="other097@example.test", password_hash=PasswordHasher(time_cost=1, memory_cost=8192).hash("other-test-password"))
        db.add(other)
        db.flush()
        notice = append_event(db, organization_id=origin.organization_id, dedupe_key="ops097:other-actor",
            event_type="RUN_TERMINAL", aggregate_type="RUN", aggregate_id=origin.id, module="intake",
            payload={"recipient_user_id": other.id, "status": origin.status, "decision": origin.decision, "origin": "MANUAL"})
        consume_notification(db, notice)
        db.commit()
        identity, dataset_id, role_id = origin.id, version.dataset_id, roles[0].id
    first = authenticated.get(f"/api/v1/notifications/inbox?grouped=true&process=run:{identity}").json()
    assert first["groups"][0]["unread_count"] == 2
    assert authenticated.get("/api/v1/notifications/unread-count").json()["unread_count"] == 2
    other_client = TestClient(authenticated.app)
    logged = other_client.post("/api/v1/auth/login", json={"email": "other097@example.test", "password": "other-test-password"})
    assert logged.status_code == 200
    second = other_client.get(f"/api/v1/notifications/inbox?grouped=true&process=run:{identity}").json()
    assert second["groups"][0]["unread_count"] == 1 and len(second["groups"][0]["items"]) == 1
    assert other_client.get("/api/v1/notifications/unread-count").json()["unread_count"] == 1
    assert not {item["id"] for item in first["groups"][0]["items"]} & {item["id"] for item in second["groups"][0]["items"]}
    with database() as db:
        db.execute(delete(RolePermission).where(RolePermission.role_id == role_id, RolePermission.permission_code == "intake:read"))
        db.commit()
    denied = authenticated.get(f"/api/v1/notifications/inbox?grouped=true&process=run:{identity}")
    assert denied.json()["groups"] == [] and denied.json()["total"] == 0 and identity not in denied.text
    assert authenticated.get("/api/v1/notifications/unread-count").json()["unread_count"] == 0
    assert other_client.get("/api/v1/notifications/unread-count").json()["unread_count"] == 1
    with database() as db:
        db.add(DatasetBlock(dataset_id=dataset_id, scope="CONTENT", reason="New request restriction", created_by_id="test-user"))
        db.commit()
    blocked = other_client.get(f"/api/v1/notifications/inbox?grouped=true&process=run:{identity}")
    assert blocked.json()["groups"] == [] and blocked.json()["total"] == 0 and identity not in blocked.text
    assert other_client.get("/api/v1/notifications/unread-count").json()["unread_count"] == 0
    other_client.close()


def test_read_authorization_snapshot_closes_after_error_and_restores_outer_scope(database):
    with database() as db:
        with pytest.raises(RuntimeError, match="synthetic failure"), read_authorization_scope(db):
            assert db.info["read_content_authorization"] == {}
            db.info["operation_process_identities"]["synthetic"] = "temporary"
            raise RuntimeError("synthetic failure")
        assert "read_content_authorization" not in db.info and "operation_process_identities" not in db.info
        previous = {"outer": None}
        db.info["read_content_authorization"] = previous
        with read_authorization_scope(db):
            assert db.info["read_content_authorization"] is not previous
        assert db.info["read_content_authorization"] is previous


def test_missing_native_resource_keeps_closed_error_inside_read_snapshot(database):
    with database() as db:
        user = db.get(User, "test-user")
        for authorize in (authorize_run_content, authorize_dataset_content):
            for cached in (False, True):
                with pytest.raises(OperationError) as error:
                    if cached:
                        with read_authorization_scope(db):
                            authorize(db, user, None)
                    else:
                        authorize(db, user, None)
                assert error.value.status == 404 and error.value.code == "NOT_FOUND"
                assert "operation_process_identities" not in db.info and "read_content_authorization" not in db.info
