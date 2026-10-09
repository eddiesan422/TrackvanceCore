"""R085-02: additive PK contract, complete checks and transactional sink DDL."""

import json
from copy import deepcopy
from pathlib import Path

import psycopg
import pymssql
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_data_sinks import FakeConnection, FakeCursor, attach_connection, column, settings
from test_delivery_service_api import (
    check_by_code,
    create_destination,
    draft_payload,
    execute_queued,
    make_dataset,
    publish_configuration,
    queue_run,
)
from test_delivery_service_api import (
    delivery_case as delivery_case,  # noqa: PLC0414
)
from test_delivery_service_api import (
    delivery_runtime as delivery_runtime,  # noqa: PLC0414
)

from trackvance import delivery_service
from trackvance.data_sinks import DeliveryError, PostgreSQLDataSink, SQLServerDataSink
from trackvance.delivery_schemas import DeliveryDraft
from trackvance.delivery_streams import PreparedRows
from trackvance.delivery_validation import execute_validation_run, validated_preflight
from trackvance.manifests import configuration_hash
from trackvance.models import Artifact, Run, User


def primary_draft(payload, keys=None):
    value = deepcopy(payload)
    value.update(schema_version=2, write_strategy="CREATE_AND_LOAD", upsert_keys=[],
                 primary_key_mode="DEFINE", primary_key_columns=keys or ["external_id"])
    value["target"].update(mode="CREATE_TABLE", table_name="new_orders")
    return value


def test_legacy_contract_preserves_canonical_hash_and_preparation(delivery_case):
    old = DeliveryDraft.model_validate(delivery_case.draft).snapshot()
    expected = deepcopy(delivery_case.draft)
    for mapping in expected["columns"]:
        for key in ("precision", "scale", "length"):
            mapping.setdefault(key, None)
    assert old == expected
    assert configuration_hash(old) == configuration_hash(expected)
    assert DeliveryDraft.model_validate(old).snapshot() == old
    assert DeliveryDraft.model_validate(old).prepared_target() == expected["target"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("primary_key_mode"),
    lambda d: d.update(primary_key_columns=[]),
    lambda d: d.update(primary_key_columns=["external_id", "external_id"]),
    lambda d: d.update(primary_key_columns=["unknown"]),
    lambda d: d["columns"][1].update(nullable=True),
    lambda d: d.update(primary_key_mode="NONE"),
    lambda d: d.update(upsert_keys=["external_id"]),
    lambda d: d.update(schema_version=1),
    lambda d: d["target"].update(mode="EXISTING_TABLE"),
])
def test_contract_rejects_ambiguous_or_incompatible_pk(delivery_case, mutate):
    value = primary_draft(delivery_case.draft)
    mutate(value)
    with pytest.raises(ValidationError):
        DeliveryDraft.model_validate(value)


def test_primary_order_and_explicit_none_change_hash(delivery_case):
    value = primary_draft(delivery_case.draft, ["tenant_id", "external_id"])
    first = DeliveryDraft.model_validate(value)
    value["primary_key_columns"].reverse()
    second = DeliveryDraft.model_validate(value)
    assert first.primary_key_columns == ["tenant_id", "external_id"]
    assert configuration_hash(first.snapshot()) != configuration_hash(second.snapshot())
    assert second.prepared_target()["_primary_key_columns"] == ["external_id", "tenant_id"]
    value.update(primary_key_mode="NONE", primary_key_columns=[])
    none = DeliveryDraft.model_validate(value)
    assert none.primary_key_columns == []
    assert configuration_hash(none.snapshot()) != configuration_hash(first.snapshot())


@pytest.mark.parametrize("keys,passed", [(["tenant_id"], False), (["external_id"], True), (["tenant_id", "external_id"], True)])
def test_preflight_does_not_infer_customer_uniqueness(database, delivery_case, keys, passed):
    draft = DeliveryDraft.model_validate(primary_draft(delivery_case.draft, keys))
    with database() as db:
        result = delivery_service.preflight_delivery(db, delivery_case.source["organization_id"], draft, raise_on_failure=False)
    assert result["status"] == ("PASS" if passed else "FAIL")
    assert check_by_code(result, "PRIMARY_KEY_SOURCE_KEYS")[0]["status"] == ("PASS" if passed else "FAIL")
    assert result["primary_key"]["columns"] == keys
    assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)
    assert any(call[0] == "validate_primary_key" for call in delivery_case.runtime.calls) is passed


def test_preflight_native_collation_failure_is_diagnosed(database, delivery_case):
    delivery_case.runtime.primary_key_error = DeliveryError("PRIMARY_KEY_DESTINATION_COLLISION", "safe")
    draft = DeliveryDraft.model_validate(primary_draft(delivery_case.draft))
    with database() as db:
        result = delivery_service.preflight_delivery(db, delivery_case.source["organization_id"], draft, raise_on_failure=False)
    assert result["status"] == "FAIL"
    assert check_by_code(result, "PRIMARY_KEY_DESTINATION_COLLISION")[0]["status"] == "FAIL"
    assert check_by_code(result, "PRIMARY_KEY_NATIVE")[0]["status"] == "FAIL"


def test_null_outside_preview_fails_complete_pk(authenticated, database, delivery_runtime, tmp_path):
    source = make_dataset(database, tmp_path, rows=[f"T1,A-{i:03},1.00,note" for i in range(12)] + ["T1,,1.00,note"], name="PK null beyond sample")
    destination = create_destination(authenticated)
    draft = DeliveryDraft.model_validate(primary_draft(draft_payload(source["version_id"], destination)))
    with database() as db:
        result = delivery_service.preflight_delivery(db, source["organization_id"], draft, raise_on_failure=False)
    assert result["status"] == "FAIL"
    assert check_by_code(result, "PRIMARY_KEY_SOURCE_KEYS")[0]["status"] == "FAIL"
    assert not any(call[0] == "validate_primary_key" for call in delivery_runtime.calls)


@pytest.mark.parametrize("adapter_type,sink_type", [(PostgreSQLDataSink, "POSTGRESQL"), (SQLServerDataSink, "SQLSERVER")])
def test_create_pk_emits_one_constraint_in_same_transaction(monkeypatch, adapter_type, sink_type):
    adapter = adapter_type(settings(sink_type))
    connection, cursor = attach_connection(monkeypatch, adapter, FakeCursor(fetchone=((True,), (False,)) if sink_type == "POSTGRESQL" else ((1,), (0,))))
    mappings = [column("customer", "customer_id", "INT64", nullable=False), column("tx", "transaction_id", "STRING", nullable=False, length=50)]
    target = {"mode": "CREATE_TABLE", "schema_name": "sales", "table_name": "orders", "create_schema": False, "_primary_key_columns": ["transaction_id", "customer_id"]}
    adapter.deliver([{"customer": 1, "tx": "00001"}], mappings, target, "CREATE_AND_LOAD", [])
    ddl = [query for query, _ in cursor.executions if query.startswith("CREATE TABLE")]
    assert len(ddl) == 1 and ddl[0].count("PRIMARY KEY") == 1
    assert ddl[0].index("transaction_id", ddl[0].index("PRIMARY KEY")) < ddl[0].index("customer_id", ddl[0].index("PRIMARY KEY"))
    assert not any("CREATE INDEX" in query for query, _ in cursor.executions)
    assert connection.commit_count == 1
    assert len(cursor.executemany_calls[0][1]) == 1


@pytest.mark.parametrize("adapter_type,sink_type", [(PostgreSQLDataSink, "POSTGRESQL"), (SQLServerDataSink, "SQLSERVER")])
def test_native_preflight_is_batched_and_always_rolled_back(monkeypatch, adapter_type, sink_type):
    monkeypatch.setenv("TRACKVANCE_DELIVERY_BATCH_ROWS", "1")
    adapter = adapter_type(settings(sink_type))
    connection, cursor = attach_connection(monkeypatch, adapter)
    result = adapter.validate_primary_key([{"id": "a"}, {"id": "b"}], [column("id", "final_id", "STRING", nullable=False, length=32)], ["final_id"])
    assert result["rows_validated"] == 2 and result["persistent_changes"] is False
    assert connection.commit_count == 0 and connection.rollback_count == 1
    assert len(cursor.executemany_calls) == 2
    assert all("trackvance_primary_key_preflight" in query for query, _ in cursor.executions + cursor.executemany_calls)
    if sink_type == "SQLSERVER":
        assert "COLLATE Latin1_General_100_CI_AS_SC" in cursor.executions[0][0]


@pytest.mark.parametrize("adapter_type,sink_type,driver_error", [
    (PostgreSQLDataSink, "POSTGRESQL", psycopg.errors.UniqueViolation("secret key")),
    (SQLServerDataSink, "SQLSERVER", pymssql.IntegrityError(2627, b"secret key")),
])
def test_native_pk_collision_rolls_back_without_echoing_values(monkeypatch, adapter_type, sink_type, driver_error):
    adapter = adapter_type(settings(sink_type))
    connection, _ = attach_connection(monkeypatch, adapter, FakeCursor(executemany_error=driver_error))
    with pytest.raises(DeliveryError) as error:
        adapter.validate_primary_key([{"id": "SECRET"}], [column("id", "id", "STRING", nullable=False, length=32)], ["id"])
    assert error.value.code == "PRIMARY_KEY_DESTINATION_COLLISION"
    assert "secret" not in error.value.message.casefold()
    assert connection.commit_count == 0 and connection.rollback_count == 1


@pytest.mark.parametrize("adapter_type,sink_type,driver,driver_error,code", [
    (PostgreSQLDataSink, "POSTGRESQL", psycopg, psycopg.errors.UniqueViolation("private key"), "PRIMARY_KEY_DESTINATION_COLLISION"),
    (PostgreSQLDataSink, "POSTGRESQL", psycopg, psycopg.errors.ProgramLimitExceeded("private key"), "PRIMARY_KEY_DESTINATION_LIMIT"),
    (SQLServerDataSink, "SQLSERVER", pymssql, pymssql.IntegrityError(2627, b"private key"), "PRIMARY_KEY_DESTINATION_COLLISION"),
    (SQLServerDataSink, "SQLSERVER", pymssql, pymssql.OperationalError(1946, b"private key"), "PRIMARY_KEY_DESTINATION_LIMIT"),
])
def test_native_pk_error_is_translated_before_real_connection_context_closes(monkeypatch, adapter_type, sink_type, driver, driver_error, code):
    connection = FakeConnection(FakeCursor(executemany_error=driver_error))
    closed = []
    monkeypatch.setattr(connection, "close", lambda: closed.append(connection.rollback_count), raising=False)
    monkeypatch.setattr(driver, "connect", lambda **_kwargs: connection)
    adapter = adapter_type(settings(sink_type))
    with pytest.raises(DeliveryError) as error:
        adapter.validate_primary_key([{"id": "PRIVATE"}], [column("id", "id", "STRING", nullable=False, length=32)], ["id"])
    assert error.value.code == code and "private" not in error.value.message.casefold()
    assert connection.commit_count == 0 and connection.rollback_count == 1
    assert closed == [1]


def test_sqlserver_rejects_unindexable_string_before_connection(monkeypatch):
    adapter = SQLServerDataSink(settings("SQLSERVER"))
    monkeypatch.setattr(adapter, "_connection", lambda: pytest.fail("Must reject before SQL"))
    with pytest.raises(DeliveryError, match="indexable"):
        adapter.validate_primary_key([{"id": "1"}], [column("id", "id", "STRING", nullable=False)], ["id"])


def test_changing_pk_invalidates_reusable_preflight(authenticated, database, delivery_case):
    draft = DeliveryDraft.model_validate(primary_draft(delivery_case.draft, ["tenant_id", "external_id"]))
    response = authenticated.post("/api/v1/delivery/validations", json=draft.snapshot())
    assert response.status_code == 202
    with database() as db:
        run = db.get(Run, response.json()["id"])
        execute_validation_run(db, run)
        db.commit()
        user = db.get(User, "test-user")
        assert validated_preflight(db, user, draft, run.id)["status"] == "PASS"
        changed = draft.snapshot()
        changed["primary_key_columns"].reverse()
        with pytest.raises(delivery_service.DeliveryOperationError) as error:
            validated_preflight(db, user, DeliveryDraft.model_validate(changed), run.id)
        assert error.value.code == "VALIDATION_BINDING_MISMATCH"


def test_pk_order_is_bound_to_sealed_preparation(monkeypatch):
    adapter = PostgreSQLDataSink(settings())
    mappings = [column("a", "a", "INT64", nullable=False), column("b", "b", "INT64", nullable=False)]
    target = {"mode": "CREATE_TABLE", "schema_name": "sales", "table_name": "orders", "_primary_key_columns": ["b", "a"]}
    payload = adapter.prepare_batched([{"a": 1, "b": 2}], mappings, target, "CREATE_AND_LOAD", [], binding={"draft_hash": "sealed"})
    assert isinstance(payload.rows, PreparedRows)
    binding = {"draft_hash": "sealed", "sink_type": "POSTGRESQL", "columns": payload.columns,
               "target": payload.target, "strategy": payload.strategy, "upsert_keys": []}
    try:
        payload.rows.verify(binding)
        changed = deepcopy(binding)
        changed["target"]["_primary_key_columns"].reverse()
        with pytest.raises(ValueError, match="configuración"):
            payload.rows.verify(changed)
    finally:
        payload.rows.remove()


@pytest.mark.parametrize("mode", ["DEFINE", "NONE"])
def test_v2_receipt_and_manifest_retain_explicit_primary_key_contract(authenticated, database, delivery_case, mode):
    value = primary_draft(delivery_case.draft, ["tenant_id", "external_id"])
    if mode == "NONE":
        value.update(primary_key_mode="NONE", primary_key_columns=[])
    config = publish_configuration(authenticated, delivery_case, **value)
    queued = queue_run(authenticated, config["id"], delivery_case.source["version_id"], f"receipt-v2-{mode}")
    execute_queued(database, queued["id"])
    response = authenticated.get(f"/api/v1/delivery/runs/{queued['id']}/receipt")
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["schema_version"] == 2 and receipt["result"] == "COMMITTED"
    assert receipt["primary_key_mode"] == mode
    assert receipt["primary_key_columns"] == value["primary_key_columns"]
    with database() as db:
        run = db.get(Run, queued["id"])
        assert run.execution_plan["primary_key_columns"] == value["primary_key_columns"]
        manifest_artifact = db.scalar(select(Artifact).where(Artifact.kind == "RUN_MANIFEST"))
        manifest = json.loads(Path(delivery_service.storage_provider.materialize(manifest_artifact)).read_text(encoding="utf-8"))
        assert manifest["run_id"] == run.id
        assert manifest["delivery"]["primary_key_mode"] == mode
        assert manifest["delivery"]["primary_key_columns"] == value["primary_key_columns"]
        assert manifest["configuration"]["config"] == DeliveryDraft.model_validate(value).snapshot()
