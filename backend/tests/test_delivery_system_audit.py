"""Audit publication policy and transactional adapter regression coverage."""

from copy import deepcopy

import pytest
from sqlalchemy import delete, select
from test_data_sinks import FakeCursor, attach_connection, column, settings
from test_delivery_service_api import (
    create_destination,
    publish_configuration,
    queue_run,
)
from test_delivery_service_api import delivery_case as delivery_case  # noqa: PLC0414
from test_delivery_service_api import delivery_runtime as delivery_runtime  # noqa: PLC0414

from trackvance.data_sinks import (
    AUDIT_NAMES,
    DeliveryError,
    DeliveryResult,
    PostgreSQLDataSink,
    SQLServerDataSink,
    inspect_audit_columns,
)
from trackvance.delivery_audit import target_identity
from trackvance.delivery_service import execute_delivery_run
from trackvance.models import DeliveryAttempt, DeliveryTargetPolicy, Role, RolePermission, Run, User


def test_fingerprint_ignores_credential_tls_and_destination_identity():
    config = {"host": "EXAMPLE.TEST.", "port": 5432, "database": "Warehouse", "username": "a"}
    target = {"schema_name": "Sales", "table_name": "Orders"}
    original = target_identity("org", "POSTGRESQL", config, target)[0]
    assert original == target_identity("org", "POSTGRESQL", {
        **config, "host": "example.test", "username": "b", "password": "changed",
        "options": {"sslmode": "require"}, "secret_reference": "rotated",
    }, target)[0]
    for key, value in (("host", "another.test"), ("port", 5433), ("database", "other")):
        assert original != target_identity("org", "POSTGRESQL", {**config, key: value}, target)[0]
    assert original != target_identity("another-org", "POSTGRESQL", config, target)[0]
    assert original != target_identity("org", "POSTGRESQL", config, {**target, "table_name": "orders"})[0]
    assert target_identity("org", "SQLSERVER", config, target)[0] == target_identity(
        "org", "SQLSERVER", config, {"schema_name": "sales", "table_name": "ORDERS"}
    )[0]


def audit_metadata(engine, *, timestamp="fechaIngesta", username="usuario"):
    return {"columns": [
        {"name": timestamp, "native_type": "timestamp with time zone" if engine == "POSTGRESQL" else "datetimeoffset",
         "datetime_precision": 6, "nullable": True},
        {"name": username, "native_type": "varchar" if engine == "POSTGRESQL" else "nvarchar", "length": 128, "nullable": True},
    ]}


@pytest.mark.parametrize("engine", ["POSTGRESQL", "SQLSERVER"])
def test_audit_metadata_adoption_missing_types_and_drift(engine):
    metadata = audit_metadata(engine)
    assert inspect_audit_columns(engine, metadata)["missing_columns"] == []
    metadata["columns"].pop()
    assert inspect_audit_columns(engine, metadata)["missing_columns"] == ["usuario"]
    with pytest.raises(DeliveryError, match="perdió") as drift:
        inspect_audit_columns(engine, metadata, materialized=True)
    assert drift.value.code == "AUDIT_COLUMNS_DRIFT"
    metadata = audit_metadata(engine)
    metadata["columns"][1]["length"] = 40
    with pytest.raises(DeliveryError) as incompatible:
        inspect_audit_columns(engine, metadata)
    assert incompatible.value.code == "AUDIT_COLUMNS_INCOMPATIBLE"
    metadata = audit_metadata(engine)
    metadata["columns"][0]["datetime_precision"] = 3
    with pytest.raises(DeliveryError):
        inspect_audit_columns(engine, metadata)
    metadata = audit_metadata(engine, timestamp="fechaingesta", username="USUARIO")
    if engine == "SQLSERVER":
        assert inspect_audit_columns(engine, metadata)["columns"] == {"fecha_ingesta": "fechaingesta", "usuario": "USUARIO"}
    else:
        with pytest.raises(DeliveryError):
            inspect_audit_columns(engine, metadata)


def audit_target(mode="EXISTING_TABLE"):
    return {"mode": mode, "schema_name": "sales", "table_name": "orders", "create_schema": False,
            "_system_audit": {"columns": AUDIT_NAMES, "username": "operator.internal",
             "fecha_ingesta": "2026-09-26T12:34:56.123456+00:00", "materialized": False}}


@pytest.mark.parametrize("engine,adapter_class", [("POSTGRESQL", PostgreSQLDataSink), ("SQLSERVER", SQLServerDataSink)])
@pytest.mark.parametrize("mode", ["EXISTING_TABLE", "CREATE_TABLE"])
def test_audit_columns_share_dml_transaction_and_timestamp(monkeypatch, engine, adapter_class, mode):
    adapter = adapter_class(settings(engine))
    cursor = FakeCursor(fetchone=((True,), (False,)) if engine == "POSTGRESQL" else ((1,), (0,)),
                        fetchall=([], []))
    connection, cursor = attach_connection(monkeypatch, adapter, cursor)
    if engine == "SQLSERVER":
        monkeypatch.setattr(adapter, "_reject_ignore_duplicate_keys", lambda *_: None)
    prepared = adapter.prepare([{"id": 1}, {"id": 2}], [column("id", "id", "INT64")],
                               audit_target(mode), "APPEND" if mode == "EXISTING_TABLE" else "CREATE_AND_LOAD", [])
    assert len(prepared.rows[0]) == 3
    assert prepared.rows[0][-2:] == prepared.rows[1][-2:]
    assert prepared.rows[0][-1] == "operator.internal"
    # Isolate the DML executor while verifying actual transaction/DDL paths.
    calls = []
    monkeypatch.setattr(adapter, "_insert", lambda *_args: calls.append("insert") or 2)
    result = adapter.deliver_prepared(prepared)
    statements = [sql for sql, _ in cursor.executions]
    assert result.audit_columns_created and calls == ["insert"]
    assert connection.commit_count == 1 and connection.rollback_count == 0
    if mode == "EXISTING_TABLE":
        alters = [sql for sql in statements if sql.startswith("ALTER TABLE")]
        assert len(alters) == 2
        assert all(" NULL" in sql and "DEFAULT" not in sql and "NOT NULL" not in sql for sql in alters)
    else:
        ddl = next(sql for sql in statements if sql.startswith("CREATE TABLE"))
        assert "fechaIngesta" in ddl and "usuario" in ddl and ddl.count("NOT NULL") == 2


@pytest.mark.parametrize("engine,adapter_class", [("POSTGRESQL", PostgreSQLDataSink), ("SQLSERVER", SQLServerDataSink)])
def test_audit_ddl_rolls_back_with_failed_dml(monkeypatch, engine, adapter_class):
    adapter = adapter_class(settings(engine))
    connection, _ = attach_connection(monkeypatch, adapter, FakeCursor(fetchall=([],)))
    if engine == "SQLSERVER":
        monkeypatch.setattr(adapter, "_reject_ignore_duplicate_keys", lambda *_: None)
    def fail(*_args):
        raise DeliveryError("DESTINATION_CONSTRAINT_VIOLATION", "Controlled DML failure")
    monkeypatch.setattr(adapter, "_insert", fail)
    with pytest.raises(DeliveryError):
        adapter.deliver([{"id": 1}], [column("id", "id", "INT64")], audit_target(), "APPEND", [])
    assert connection.commit_count == 0 and connection.rollback_count == 1


def test_publish_locks_physical_policy_across_destinations(authenticated, database, delivery_case):
    case = delivery_case
    case.runtime.permissions["alter_table"] = True
    case.draft["audit_columns_enabled"] = True
    config = publish_configuration(authenticated, case)
    with database() as db:
        policy = db.scalar(select(DeliveryTargetPolicy))
        assert policy and policy.materialized_at is None
        assert db.scalar(select(DeliveryAttempt)) is None
    second = create_destination(authenticated, username="other_writer")
    manipulated = {**case.draft, "audit_columns_enabled": False,
                   "destination_id": second["id"], "destination_version_id": second["destination_version_id"]}
    blocked = authenticated.post("/api/v1/delivery/configurations", json={"name": "bypass", **manipulated})
    assert blocked.status_code == 412 and blocked.json()["error"]["code"] == "AUDIT_COLUMNS_REQUIRED"
    policy_response = authenticated.get(f"/api/v1/delivery/destinations/{second['id']}/target-policy",
                                       params={"schema_name": "sales", "table_name": "orders"})
    assert policy_response.json()["audit_columns_required"] is True
    assert config["config"]["audit_columns_enabled"] is True


def test_snapshot_commit_evidence_and_external_drop(authenticated, database, delivery_case):
    case = delivery_case
    case.runtime.permissions["alter_table"] = True
    case.runtime.result = DeliveryResult(2, 2, 2, 0, 123, audit_columns_created=True)
    case.draft["audit_columns_enabled"] = True
    config = publish_configuration(authenticated, case)
    with database() as db:
        user = db.get(User, "test-user")
        username = user.username
    run = queue_run(authenticated, config["id"], case.source["version_id"], "audit-snapshot")
    with database() as db:
        user = db.get(User, "test-user")
        user.username = "renamed.after.queue"
        db.commit()
        execute_delivery_run(db, db.get(Run, run["id"]))
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run["id"]))
        assert attempt.status == "COMMITTED"
        snapshot = attempt.system_audit
        assert snapshot["username"] == username and snapshot["columns_created"]
        assert db.scalar(select(DeliveryTargetPolicy)).materialized_at is not None
    receipt = authenticated.get(f"/api/v1/delivery/runs/{run['id']}/receipt").json()
    assert receipt["system_audit"] == snapshot
    manifest = authenticated.get(f"/api/v1/runs/{run['id']}/evidence").json()
    assert manifest["delivery"]["system_audit"] == snapshot
    failed = authenticated.post("/api/v1/delivery/preflight", json=case.draft)
    assert failed.status_code == 412 and failed.json()["error"]["code"] == "AUDIT_COLUMNS_DRIFT"


def test_unknown_keeps_required_unmaterialized_and_never_replays(authenticated, database, delivery_case):
    case = delivery_case
    case.runtime.permissions["alter_table"] = True
    case.runtime.deliver_error = DeliveryError("COMMIT_LOST", "Confirmación perdida", ambiguous=True)
    case.draft["audit_columns_enabled"] = True
    config = publish_configuration(authenticated, case)
    run = queue_run(authenticated, config["id"], case.source["version_id"], "audit-unknown")
    with database() as db:
        stored = db.get(Run, run["id"])
        execute_delivery_run(db, stored)
        execute_delivery_run(db, stored)
        assert stored.status == "UNKNOWN"
        policy = db.scalar(select(DeliveryTargetPolicy))
        assert policy.audit_columns_required and policy.materialized_at is None
        attempt = db.scalar(select(DeliveryAttempt))
        assert attempt.system_audit["columns_created"] is None
    assert sum(call[0] == "deliver" for call in case.runtime.calls) == 1
    # A deliberate later operation inspects actual compatible columns; it does
    # not rewrite the unknown attempt or assume its transaction outcome.
    case.runtime.metadata["columns"].extend(audit_metadata("POSTGRESQL")["columns"])
    for item in case.runtime.metadata["columns"][-2:]:
        item.update(has_default=False, identity=False, generated=False)
    assert authenticated.post("/api/v1/delivery/preflight", json=case.draft).status_code == 200


def test_audit_collision_and_missing_remote_alter_fail_before_publish(authenticated, database, delivery_case):
    case = delivery_case
    case.draft["audit_columns_enabled"] = True
    response = authenticated.post("/api/v1/delivery/configurations", json={"name": "audit", **case.draft})
    assert response.status_code == 412
    with database() as db:
        assert db.scalar(select(DeliveryTargetPolicy)) is None
    manipulated = deepcopy(case.draft)
    manipulated["columns"][0]["target_name"] = "usuario"
    response = authenticated.post("/api/v1/delivery/preflight", json=manipulated)
    assert response.status_code == 412 and response.json()["error"]["code"] == "AUDIT_MAPPING_COLLISION"


@pytest.mark.parametrize("permission,strategy,audit_enabled", [
    ("delivery:alter_target", "APPEND", True),
    ("delivery:overwrite", "OVERWRITE", False),
])
def test_sensitive_target_permissions_enforced_at_publish_and_enqueue(
    authenticated, database, delivery_case, permission, strategy, audit_enabled,
):
    case = delivery_case
    case.runtime.permissions["alter_table"] = True
    case.draft.update(audit_columns_enabled=audit_enabled, write_strategy=strategy)
    configuration = publish_configuration(authenticated, case)
    with database() as db:
        user = db.get(User, "test-user")
        user.role = "Data Analyst"
        db.flush()
        db.execute(delete(RolePermission).where(
            RolePermission.role_id == user.role_id, RolePermission.permission_code == permission,
        ))
        db.commit()
    blocked = authenticated.post("/api/v1/delivery/configurations", json={"name": "denied", **case.draft})
    assert blocked.status_code == 403
    blocked = authenticated.post("/api/v1/delivery/runs", json={
        "configuration_id": configuration["id"], "dataset_version_id": case.source["version_id"],
    })
    assert blocked.status_code == 403
    with database() as db:
        assert db.scalar(select(Run)) is None


@pytest.mark.parametrize("engine,adapter_class", [("POSTGRESQL", PostgreSQLDataSink), ("SQLSERVER", SQLServerDataSink)])
def test_materialized_drift_under_remote_lock_never_alters_or_inserts(monkeypatch, engine, adapter_class):
    adapter = adapter_class(settings(engine))
    connection, cursor = attach_connection(monkeypatch, adapter, FakeCursor(fetchall=([],)))
    target = audit_target()
    target["_system_audit"]["materialized"] = True
    with pytest.raises(DeliveryError) as failed:
        adapter.deliver([{"id": 1}], [column("id", "id", "INT64")], target, "APPEND", [])
    assert failed.value.code == "AUDIT_COLUMNS_DRIFT"
    assert connection.commit_count == 0 and connection.rollback_count == 1
    assert not any(sql.startswith("ALTER TABLE") for sql, _ in cursor.executions)
    assert not cursor.executemany_calls


def test_receipt_download_requires_delivery_visibility(authenticated, database, delivery_case):
    configuration = publish_configuration(authenticated, delivery_case)
    run = queue_run(authenticated, configuration["id"], delivery_case.source["version_id"], "receipt-rbac")
    with database() as db:
        execute_delivery_run(db, db.get(Run, run["id"]))
        role = Role(name="Artifacts only", normalized_name="artifacts only")
        db.add(role)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_code="artifacts:download"))
        db.get(User, "test-user").role_id = role.id
        db.commit()
    response = authenticated.get(f"/api/v1/delivery/runs/{run['id']}/receipt")
    assert response.status_code == 403
