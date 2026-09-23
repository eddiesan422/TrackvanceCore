"""Fast Delivery API/service tests with no external database dependency."""

import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from trackvance import data_sinks, delivery_api, delivery_service
from trackvance.credential_store import EncryptedFileSecretStore
from trackvance.data_sinks import DeliveryError, DeliveryResult
from trackvance.db import utcnow
from trackvance.delivery_schemas import DeliveryDraft
from trackvance.delivery_service import DeliveryOperationError, execute_delivery_run
from trackvance.models import (
    Artifact,
    ArtifactLink,
    AuditEvent,
    Configuration,
    Dataset,
    DatasetVersion,
    DeliveryAttempt,
    DeliveryDestinationVersion,
    IdempotencyKey,
    Job,
    Run,
    User,
)
from trackvance.services import create_version

PASSWORD = "destination-private-credential-42"
ROTATED_PASSWORD = "rotated-destination-credential-73"


def destination_body(sink_type: str = "POSTGRESQL", **values: Any) -> dict[str, Any]:
    return {
        "name": "Certified warehouse",
        "sink_type": sink_type,
        "host": "destination.example.test",
        "port": 5432 if sink_type == "POSTGRESQL" else 1433,
        "database": "warehouse",
        "username": "delivery_writer",
        "password": PASSWORD,
        "options": (
            {"sslmode": "require"}
            if sink_type == "POSTGRESQL"
            else {"encryption": "require"}
        ),
        **values,
    }


def default_metadata() -> dict[str, Any]:
    return {
        "schema_name": "sales",
        "table_name": "orders",
        "columns": [
            {
                "name": "tenant_id",
                "native_type": "varchar",
                "logical_type": "STRING",
                "nullable": False,
                "has_default": False,
                "identity": False,
                "generated": False,
                "length": 16,
                "precision": None,
                "scale": None,
            },
            {
                "name": "external_id",
                "native_type": "varchar",
                "logical_type": "STRING",
                "nullable": False,
                "has_default": False,
                "identity": False,
                "generated": False,
                "length": 32,
                "precision": None,
                "scale": None,
            },
            {
                "name": "amount",
                "native_type": "numeric",
                "logical_type": "DECIMAL",
                "nullable": False,
                "has_default": False,
                "identity": False,
                "generated": False,
                "length": None,
                "precision": 18,
                "scale": 4,
            },
            {
                "name": "label",
                "native_type": "varchar",
                "logical_type": "STRING",
                "nullable": True,
                "has_default": False,
                "identity": False,
                "generated": False,
                "length": 128,
                "precision": None,
                "scale": None,
            },
        ],
        "constraints": [
            {
                "type": "UNIQUE",
                "columns": ["tenant_id", "external_id"],
                "name": "orders_tenant_external_key",
            }
        ],
    }


@pytest.fixture
def delivery_runtime(monkeypatch, tmp_path):
    store = EncryptedFileSecretStore(
        tmp_path / "delivery_credentials", tmp_path / "delivery_keys" / "master.key"
    )
    state = SimpleNamespace(
        store=store,
        settings=[],
        calls=[],
        test_error=None,
        deliver_error=None,
        deliver_hook=None,
        schemas_items=["sales"],
        tables_by_schema={"sales": ["orders"]},
        metadata=default_metadata(),
        permissions={
            "connect": True,
            "schema_exists": True,
            "target_exists": True,
            "allowed": True,
            "create_schema": True,
            "create_table": True,
        },
        result=DeliveryResult(
            rows_attempted=2,
            rows_written=2,
            rows_inserted=1,
            rows_updated=1,
            bytes_sent=73,
            remote_reference="remote-transaction-7",
        ),
    )

    class FakeSink(data_sinks.DatabaseDataSink):
        def __init__(self, sink_settings):
            super().__init__(sink_settings)
            self.sink_type = sink_settings.sink_type

        def test(self):
            state.calls.append(("test",))
            if state.test_error is not None:
                raise state.test_error

        def schemas(self):
            state.calls.append(("schemas",))
            return list(state.schemas_items)

        def tables(self, schema_name):
            state.calls.append(("tables", schema_name))
            return list(state.tables_by_schema.get(schema_name, []))

        def table_metadata(self, schema_name, table_name):
            state.calls.append(("table_metadata", schema_name, table_name))
            metadata = deepcopy(state.metadata)
            if self.settings.sink_type == "SQLSERVER":
                for item in metadata["columns"]:
                    if (
                        item.get("logical_type") == "STRING"
                        and item.get("native_type") == "varchar"
                        and item.get("collation") is None
                    ):
                        item["native_type"] = "nvarchar"
                        item["collation"] = "Latin1_General_100_CI_AS_SC"
            return metadata

        def permissions(self, target, strategy):
            state.calls.append(("permissions", deepcopy(target), strategy))
            return deepcopy(state.permissions)

        def deliver_prepared(self, payload):
            state.calls.append(
                (
                    "deliver",
                    deepcopy(payload.rows),
                    deepcopy(payload.columns),
                    deepcopy(payload.target),
                    payload.strategy,
                    list(payload.upsert_keys),
                )
            )
            if state.deliver_hook is not None:
                state.deliver_hook()
            if state.deliver_error is not None:
                raise state.deliver_error
            return state.result

    def create_sink(sink_settings):
        state.settings.append(sink_settings)
        return FakeSink(sink_settings)

    registry = SimpleNamespace(create=create_sink)
    monkeypatch.setattr(delivery_api, "destination_secret_store", store)
    monkeypatch.setattr(delivery_service, "destination_secret_store", store)
    monkeypatch.setattr(delivery_api, "sink_registry", registry)
    monkeypatch.setattr(delivery_service, "sink_registry", registry)
    return state


def make_dataset(
    database,
    tmp_path,
    *,
    rows: list[str] | None = None,
    name: str = "Delivery source",
) -> dict[str, str]:
    source = tmp_path / (name.casefold().replace(" ", "-") + ".csv")
    records = rows or [
        "T1,A-001,10.25,alpha",
        "T1,A-002,20.50,beta",
    ]
    source.write_text(
        "tenant_id,external_id,amount,note\n" + "\n".join(records) + "\n",
        encoding="utf-8",
    )
    with database() as db:
        user = db.get(User, "test-user")
        dataset = Dataset(
            organization_id=user.organization_id,
            name=name,
            owner="Delivery Team",
        )
        db.add(dataset)
        db.flush()
        version = create_version(db, dataset, source, source.name, actor=user.name)
        db.commit()
        return {
            "dataset_id": dataset.id,
            "version_id": version.id,
            "organization_id": user.organization_id,
        }


def create_destination(authenticated, **values: Any) -> dict[str, Any]:
    response = authenticated.post(
        "/api/v1/delivery/destinations", json=destination_body(**values)
    )
    assert response.status_code == 201, response.text
    return response.json()


def draft_payload(
    version_id: str,
    destination: dict[str, Any],
    *,
    strategy: str = "APPEND",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_version_id": version_id,
        "destination_id": destination["id"],
        "destination_version_id": destination["destination_version_id"],
        "target": {
            "mode": "EXISTING_TABLE",
            "schema_name": "sales",
            "table_name": "orders",
            "create_schema": False,
        },
        "columns": [
            {
                "source_name": "tenant_id",
                "target_name": "tenant_id",
                "target_type": "STRING",
                "ordinal": 0,
                "nullable": False,
                "length": 16,
            },
            {
                "source_name": "external_id",
                "target_name": "external_id",
                "target_type": "STRING",
                "ordinal": 1,
                "nullable": False,
                "length": 32,
            },
            {
                "source_name": "amount",
                "target_name": "amount",
                "target_type": "DECIMAL",
                "ordinal": 2,
                "nullable": False,
                "precision": 12,
                "scale": 2,
            },
            {
                "source_name": "note",
                "target_name": "label",
                "target_type": "STRING",
                "ordinal": 3,
                "nullable": True,
                "length": 64,
            },
        ],
        "write_strategy": strategy,
        "upsert_keys": ["tenant_id", "external_id"] if strategy == "UPSERT" else [],
    }


@pytest.fixture
def delivery_case(authenticated, database, delivery_runtime, tmp_path):
    source = make_dataset(database, tmp_path)
    destination = create_destination(authenticated)
    delivery_runtime.calls.clear()
    delivery_runtime.settings.clear()
    return SimpleNamespace(
        source=source,
        destination=destination,
        draft=draft_payload(source["version_id"], destination),
        runtime=delivery_runtime,
    )


def publish_configuration(authenticated, case, **values: Any) -> dict[str, Any]:
    body = {
        "name": "Certified Delivery",
        "owner": "Delivery Team",
        "description": "Immutable publication snapshot",
        **case.draft,
        **values,
    }
    response = authenticated.post("/api/v1/delivery/configurations", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def queue_run(authenticated, configuration_id: str, version_id: str, key: str) -> dict[str, Any]:
    response = authenticated.post(
        "/api/v1/delivery/runs",
        json={"configuration_id": configuration_id, "dataset_version_id": version_id},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def check_by_code(result: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [check for check in result["checks"] if check["code"] == code]


def test_delivery_schema_sorts_snapshot_and_rejects_ambiguous_plans(delivery_case):
    payload = deepcopy(delivery_case.draft)
    payload["columns"] = list(reversed(payload["columns"]))
    draft = DeliveryDraft.model_validate(payload)

    assert [item["ordinal"] for item in draft.snapshot()["columns"]] == [0, 1, 2, 3]
    assert draft.snapshot() == draft.model_dump(mode="json")

    invalid_payloads = []
    duplicate_source = deepcopy(payload)
    duplicate_source["columns"][0]["source_name"] = duplicate_source["columns"][1][
        "source_name"
    ]
    invalid_payloads.append(duplicate_source)
    duplicate_target = deepcopy(payload)
    duplicate_target["columns"][0]["target_name"] = duplicate_target["columns"][1][
        "target_name"
    ]
    invalid_payloads.append(duplicate_target)
    duplicate_ordinal = deepcopy(payload)
    duplicate_ordinal["columns"][0]["ordinal"] = duplicate_ordinal["columns"][1]["ordinal"]
    invalid_payloads.append(duplicate_ordinal)
    create_with_append = deepcopy(payload)
    create_with_append["target"]["mode"] = "CREATE_TABLE"
    invalid_payloads.append(create_with_append)
    existing_create_and_load = deepcopy(payload)
    existing_create_and_load["write_strategy"] = "CREATE_AND_LOAD"
    invalid_payloads.append(existing_create_and_load)
    upsert_without_keys = deepcopy(payload)
    upsert_without_keys["write_strategy"] = "UPSERT"
    invalid_payloads.append(upsert_without_keys)
    append_with_keys = deepcopy(payload)
    append_with_keys["upsert_keys"] = ["external_id"]
    invalid_payloads.append(append_with_keys)

    for invalid in invalid_payloads:
        with pytest.raises(ValidationError):
            DeliveryDraft.model_validate(invalid)


@pytest.mark.parametrize("invalid_value", [True, "18", 18.0])
def test_delivery_schema_rejects_coerced_technical_numbers(
    delivery_case, invalid_value
):
    payload = deepcopy(delivery_case.draft)
    payload["columns"][2]["precision"] = invalid_value

    with pytest.raises(ValidationError):
        DeliveryDraft.model_validate(payload)


def test_destination_create_requires_password(authenticated):
    body = destination_body()
    del body["password"]

    response = authenticated.post("/api/v1/delivery/destinations", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_destination_versions_rotate_secrets_and_never_expose_them(
    authenticated, database, delivery_runtime
):
    first = create_destination(authenticated)
    assert first["version"] == 1
    assert PASSWORD not in json.dumps(first)
    assert "secret_reference" not in first

    renamed = authenticated.patch(
        f"/api/v1/delivery/destinations/{first['id']}",
        json={
            "version": 1,
            "name": "Renamed warehouse",
            "options": {"sslmode": "require", "query_timeout": 45},
        },
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["version"] == 2
    rotated = authenticated.patch(
        f"/api/v1/delivery/destinations/{first['id']}",
        json={"version": 2, "password": ROTATED_PASSWORD},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["version"] == 3

    responses = [
        authenticated.get("/api/v1/delivery/destinations"),
        authenticated.get(f"/api/v1/delivery/destinations/{first['id']}"),
        authenticated.get("/api/v1/audit-events"),
    ]
    for response in responses:
        assert PASSWORD not in response.text
        assert ROTATED_PASSWORD not in response.text
        assert "secret_reference" not in response.text

    with database() as db:
        versions = db.scalars(
            select(DeliveryDestinationVersion).order_by(DeliveryDestinationVersion.version)
        ).all()
        assert [version.version for version in versions] == [1, 2, 3]
        assert versions[0].config["host"] == "destination.example.test"
        assert versions[1].config["options"]["query_timeout"] == 45
        assert versions[0].secret_reference == versions[1].secret_reference
        assert versions[2].secret_reference != versions[1].secret_reference
        assert delivery_runtime.store.get(
            versions[0].organization_id, versions[0].secret_reference
        ) == PASSWORD
        assert delivery_runtime.store.get(
            versions[2].organization_id, versions[2].secret_reference
        ) == ROTATED_PASSWORD
        serialized = json.dumps(
            [
                {
                    column.name: getattr(version, column.name)
                    for column in version.__table__.columns
                }
                for version in versions
            ],
            default=str,
        )
        assert PASSWORD not in serialized
        assert ROTATED_PASSWORD not in serialized


def test_delivery_endpoints_enforce_csrf_and_central_rbac(
    authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    csrf = authenticated.headers.pop("X-CSRF-Token")
    for path, body in [
        ("/api/v1/delivery/preview", delivery_case.draft),
        (
            "/api/v1/delivery/runs",
            {
                "configuration_id": config["id"],
                "dataset_version_id": delivery_case.source["version_id"],
            },
        ),
        ("/api/v1/delivery/destinations", destination_body(name="Blocked by CSRF")),
    ]:
        response = authenticated.post(path, json=body)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"
    authenticated.headers["X-CSRF-Token"] = csrf

    with database() as db:
        db.get(User, "test-user").role = "Auditor"
        db.commit()
    destination_url = f"/api/v1/delivery/destinations/{delivery_case.destination['id']}"
    assert authenticated.get("/api/v1/delivery/destinations").status_code == 200
    assert authenticated.get(destination_url + "/schemas").status_code == 403
    assert authenticated.post(destination_url + "/test").status_code == 403
    assert authenticated.post("/api/v1/delivery/preview", json=delivery_case.draft).status_code == 403
    assert authenticated.post(
        "/api/v1/delivery/runs",
        json={
            "configuration_id": config["id"],
            "dataset_version_id": delivery_case.source["version_id"],
        },
    ).status_code == 403

    with database() as db:
        db.get(User, "test-user").role = "Data Analyst"
        db.commit()
    assert authenticated.get(destination_url + "/schemas").status_code == 200
    assert authenticated.post(
        "/api/v1/delivery/destinations", json=destination_body(name="RBAC denied")
    ).status_code == 403
    assert authenticated.post(
        "/api/v1/delivery/preview", json=delivery_case.draft
    ).status_code == 200
    assert authenticated.post(
        "/api/v1/delivery/runs",
        json={
            "configuration_id": config["id"],
            "dataset_version_id": delivery_case.source["version_id"],
        },
    ).status_code == 202


def test_delivery_resources_are_organization_isolated(
    authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    run = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "organization-isolation",
    )
    with database() as db:
        db.get(User, "test-user").organization_id = "another-organization"
        db.commit()

    destination_url = f"/api/v1/delivery/destinations/{delivery_case.destination['id']}"
    assert authenticated.get("/api/v1/delivery/destinations").json()["items"] == []
    for response in (
        authenticated.get(destination_url),
        authenticated.get(destination_url + "/schemas"),
        authenticated.post(destination_url + "/test"),
        authenticated.patch(destination_url, json={"version": 1, "name": "Not mine"}),
        authenticated.delete(destination_url, params={"version": 1}),
        authenticated.get(f"/api/v1/delivery/runs/{run['id']}/attempts"),
        authenticated.get(f"/api/v1/delivery/runs/{run['id']}/receipt"),
    ):
        assert response.status_code == 404, response.text
    assert authenticated.get("/api/v1/delivery/configurations").json()["items"] == []
    assert authenticated.get("/api/v1/delivery/runs").json()["items"] == []
    preview = authenticated.post("/api/v1/delivery/preview", json=delivery_case.draft)
    assert preview.status_code == 412
    assert preview.json()["error"]["code"] == "FAILED_PRECONDITION"


def test_preview_is_bounded_and_has_no_persistent_or_remote_side_effects(
    authenticated, database, delivery_case
):
    tracked_models = (Artifact, DatasetVersion, Configuration, Run, DeliveryAttempt, AuditEvent)
    with database() as db:
        before = {
            model: db.scalar(select(func.count()).select_from(model)) for model in tracked_models
        }
    response = authenticated.post(
        "/api/v1/delivery/preview", params={"limit": 1}, json=delivery_case.draft
    )

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["sampled_rows"] == 1
    assert preview["source_rows"][0]["external_id"] == "A-001"
    assert preview["destination_rows"][0]["label"] == "alpha"
    assert [item["target_type"] for item in preview["columns"]] == [
        "VARCHAR(16)",
        "VARCHAR(32)",
        "NUMERIC(12,2)",
        "VARCHAR(64)",
    ]
    assert delivery_case.runtime.calls == []
    with database() as db:
        after = {
            model: db.scalar(select(func.count()).select_from(model)) for model in tracked_models
        }
    assert after == before


def test_preflight_success_is_read_only_and_validates_composite_upsert(
    database, delivery_case
):
    payload = deepcopy(delivery_case.draft)
    payload["write_strategy"] = "UPSERT"
    payload["upsert_keys"] = ["tenant_id", "external_id"]
    draft = DeliveryDraft.model_validate(payload)
    with database() as db:
        result = delivery_service.preflight_delivery(
            db, delivery_case.source["organization_id"], draft
        )

    assert result["status"] == "PASS"
    assert all(check["status"] == "PASS" for check in result["checks"])
    assert check_by_code(result, "UPSERT_UNIQUE_CONSTRAINT")[0]["status"] == "PASS"
    assert check_by_code(result, "UPSERT_SOURCE_KEYS")[0]["status"] == "PASS"
    assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)
    assert delivery_case.runtime.settings[-1].password == PASSWORD


def test_create_target_preflight_checks_schema_permissions_without_writes(
    database, delivery_case
):
    payload = deepcopy(delivery_case.draft)
    payload["target"] = {
        "mode": "CREATE_TABLE",
        "schema_name": "new_delivery_schema",
        "table_name": "new_orders",
        "create_schema": True,
    }
    payload["write_strategy"] = "CREATE_AND_LOAD"
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db, delivery_case.source["organization_id"], draft
        )

    assert result["status"] == "PASS"
    assert check_by_code(result, "SCHEMA_STATE")[0]["status"] == "PASS"
    assert check_by_code(result, "TARGET_ABSENT")[0]["status"] == "PASS"
    assert check_by_code(result, "PERMISSIONS")[0]["status"] == "PASS"
    assert not any(call[0] in {"table_metadata", "deliver"} for call in delivery_case.runtime.calls)


def test_preflight_rejects_string_to_decimal_functional_conversion(
    authenticated, database, delivery_runtime, tmp_path
):
    source = make_dataset(
        database,
        tmp_path,
        rows=["T1,A-001,10.25,42.50 "],
        name="String is not parsed by Delivery",
    )
    destination = create_destination(authenticated)
    payload = draft_payload(source["version_id"], destination)
    payload["target"] = {
        "mode": "CREATE_TABLE",
        "schema_name": "sales",
        "table_name": "forbidden_conversion",
        "create_schema": False,
    }
    payload["write_strategy"] = "CREATE_AND_LOAD"
    payload["columns"][3] = {
        "source_name": "note",
        "target_name": "numeric_note",
        "target_type": "DECIMAL",
        "ordinal": 3,
        "nullable": False,
        "precision": 12,
        "scale": 2,
    }
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db, source["organization_id"], draft, raise_on_failure=False
        )
        with pytest.raises(DeliveryOperationError) as preflight_error:
            delivery_service.preflight_delivery(db, source["organization_id"], draft)
        with pytest.raises(DeliveryOperationError) as preview_error:
            delivery_service.preview_delivery(db, source["organization_id"], draft)

    assert result["status"] == "FAIL"
    assert check_by_code(result, "SOURCE_TYPE_PRESERVATION")[0]["status"] == "FAIL"
    assert preflight_error.value.status == 412
    assert preflight_error.value.code == "FAILED_PRECONDITION"
    assert "SOURCE_TYPE_PRESERVATION" in json.dumps(preflight_error.value.details)
    assert preview_error.value.status == 412
    assert preview_error.value.details == {
        "source_type_mismatches": [
            {
                "source_name": "note",
                "source_type": "STRING",
                "target_type": "DECIMAL",
            }
        ]
    }
    assert not any(call[0] == "deliver" for call in delivery_runtime.calls)


@pytest.mark.parametrize(
    "failure_code",
    [
        "TARGET_EXISTS",
        "PERMISSIONS",
        "TYPE_COMPATIBLE",
        "DECIMAL_COMPATIBLE",
        "LENGTH_COMPATIBLE",
        "NULLABILITY_COMPATIBLE",
        "REQUIRED_TARGET_COLUMNS",
        "UPSERT_UNIQUE_CONSTRAINT",
    ],
)
def test_preflight_reports_drift_permissions_and_compatibility_failures(
    database, delivery_case, failure_code
):
    state = delivery_case.runtime
    payload = deepcopy(delivery_case.draft)
    if failure_code == "TARGET_EXISTS":
        state.tables_by_schema["sales"] = []
    elif failure_code == "PERMISSIONS":
        state.permissions["allowed"] = False
    elif failure_code == "TYPE_COMPATIBLE":
        state.metadata["columns"][2]["logical_type"] = "STRING"
    elif failure_code == "DECIMAL_COMPATIBLE":
        state.metadata["columns"][2]["precision"] = 8
    elif failure_code == "LENGTH_COMPATIBLE":
        state.metadata["columns"][3]["length"] = 4
    elif failure_code == "NULLABILITY_COMPATIBLE":
        state.metadata["columns"][3]["nullable"] = False
    elif failure_code == "REQUIRED_TARGET_COLUMNS":
        state.metadata["columns"].append(
            {
                "name": "required_by_target",
                "logical_type": "STRING",
                "nullable": False,
                "has_default": False,
                "identity": False,
                "generated": False,
            }
        )
    else:
        payload["write_strategy"] = "UPSERT"
        payload["upsert_keys"] = ["tenant_id", "external_id"]
        state.metadata["constraints"] = [
            {"type": "UNIQUE", "columns": ["external_id"]}
        ]
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            delivery_case.source["organization_id"],
            draft,
            raise_on_failure=False,
        )
        with pytest.raises(DeliveryOperationError) as error:
            delivery_service.preflight_delivery(
                db, delivery_case.source["organization_id"], draft
            )

    assert result["status"] == "FAIL"
    assert any(check["status"] == "FAIL" for check in check_by_code(result, failure_code))
    assert error.value.status == 412
    assert error.value.code == "FAILED_PRECONDITION"
    assert not any(call[0] == "deliver" for call in state.calls)


def test_postgresql_overwrite_preflight_rejects_active_row_security(
    database, delivery_case
):
    payload = deepcopy(delivery_case.draft)
    payload["write_strategy"] = "OVERWRITE"
    delivery_case.runtime.metadata["row_security_active"] = True

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            delivery_case.source["organization_id"],
            DeliveryDraft.model_validate(payload),
            raise_on_failure=False,
        )

    assert result["status"] == "FAIL"
    assert check_by_code(result, "OVERWRITE_ROW_SECURITY")[0]["status"] == "FAIL"


def test_sqlserver_preflight_rejects_ignore_dup_key_index(
    authenticated, database, delivery_runtime, tmp_path
):
    source = make_dataset(database, tmp_path, name="Ignore duplicate key")
    destination = create_destination(authenticated, sink_type="SQLSERVER")
    payload = draft_payload(source["version_id"], destination)
    delivery_runtime.metadata["constraints"][0]["ignore_duplicate_keys"] = True

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            source["organization_id"],
            DeliveryDraft.model_validate(payload),
            raise_on_failure=False,
        )

    assert result["status"] == "FAIL"
    assert check_by_code(result, "SQLSERVER_IGNORE_DUP_KEY")[0]["status"] == "FAIL"


def test_preflight_rejects_approximate_native_target_for_decimal(
    database, delivery_case
):
    state = delivery_case.runtime
    state.metadata["columns"][2].update(
        {
            "native_type": "double precision",
            "logical_type": "DECIMAL",
            "precision": 53,
            "scale": None,
        }
    )
    draft = DeliveryDraft.model_validate(delivery_case.draft)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            delivery_case.source["organization_id"],
            draft,
            raise_on_failure=False,
        )

    assert result["status"] == "FAIL"
    assert check_by_code(result, "TYPE_COMPATIBLE")[2]["status"] == "FAIL"
    assert check_by_code(result, "DECIMAL_COMPATIBLE")[0]["status"] == "FAIL"


@pytest.mark.parametrize(
    ("sink_type", "native_type", "collation"),
    [
        ("POSTGRESQL", "jsonb", None),
        ("POSTGRESQL", "uuid", None),
        ("SQLSERVER", "varchar", "SQL_Latin1_General_CP1_CI_AS"),
        ("SQLSERVER", "nchar", "Latin1_General_100_CI_AS_SC"),
        ("SQLSERVER", "uniqueidentifier", None),
    ],
)
def test_preflight_rejects_non_exact_native_string_families(
    authenticated,
    database,
    delivery_runtime,
    tmp_path,
    sink_type,
    native_type,
    collation,
):
    source = make_dataset(database, tmp_path)
    destination = create_destination(authenticated, sink_type=sink_type)
    delivery_runtime.metadata["columns"][0].update(
        {
            "native_type": native_type,
            "logical_type": "UNSUPPORTED",
            "collation": collation,
        }
    )

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            source["organization_id"],
            DeliveryDraft.model_validate(
                draft_payload(source["version_id"], destination)
            ),
            raise_on_failure=False,
        )

    assert result["status"] == "FAIL"
    assert check_by_code(result, "TYPE_COMPATIBLE")[0]["status"] == "FAIL"
    assert check_by_code(result, "STRING_STORAGE_COMPATIBLE")[0]["status"] == "FAIL"


@pytest.mark.parametrize(
    ("target_length", "collation", "expected", "failed_check"),
    [
        (1, "Latin1_General_100_CI_AS_SC", "FAIL", "LENGTH_COMPATIBLE"),
        (2, "SQL_Latin1_General_CP1_CI_AS", "FAIL", "STRING_STORAGE_COMPATIBLE"),
        (2, "Latin1_General_100_CI_AS_SC", "PASS", None),
    ],
)
def test_sqlserver_string_preflight_uses_utf16_units_and_sc_collation(
    authenticated,
    database,
    delivery_runtime,
    tmp_path,
    target_length,
    collation,
    expected,
    failed_check,
):
    source = make_dataset(
        database,
        tmp_path,
        rows=["😀,A-001,10.25,alpha"],
        name=f"Supplementary {target_length} {expected}",
    )
    destination = create_destination(authenticated, sink_type="SQLSERVER")
    payload = draft_payload(source["version_id"], destination)
    payload["columns"][0]["length"] = target_length
    delivery_runtime.metadata["columns"][0].update(
        {
            "native_type": "nvarchar",
            "logical_type": "STRING",
            "length": target_length,
            "collation": collation,
        }
    )

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            source["organization_id"],
            DeliveryDraft.model_validate(payload),
            raise_on_failure=False,
        )

    assert result["status"] == expected
    if failed_check is not None:
        assert check_by_code(result, failed_check)[0]["status"] == "FAIL"


def test_preflight_checks_decimal_integer_and_fractional_capacity(
    database, delivery_case
):
    state = delivery_case.runtime
    payload = deepcopy(delivery_case.draft)
    payload["columns"][2].update({"precision": 18, "scale": 2})
    state.metadata["columns"][2].update(
        {"native_type": "numeric", "precision": 18, "scale": 4}
    )
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            delivery_case.source["organization_id"],
            draft,
            raise_on_failure=False,
        )

    assert result["status"] == "FAIL"
    assert check_by_code(result, "DECIMAL_COMPATIBLE")[0]["status"] == "FAIL"


@pytest.mark.parametrize(
    ("sink_type", "native", "precision", "value", "expected"),
    [
        (
            "POSTGRESQL",
            "timestamp without time zone",
            0,
            "2026-09-23T01:02:03.789123",
            "FAIL",
        ),
        (
            "POSTGRESQL",
            "timestamp without time zone",
            6,
            "2026-09-23T01:02:03.789123+00:00",
            "FAIL",
        ),
        (
            "POSTGRESQL",
            "timestamp with time zone",
            0,
            "2026-09-23T01:02:03.789123+00:00",
            "FAIL",
        ),
        (
            "POSTGRESQL",
            "timestamp with time zone",
            3,
            "2026-09-23T01:02:03.123000+00:00",
            "PASS",
        ),
        (
            "POSTGRESQL",
            "timestamp with time zone",
            3,
            "2026-09-23T01:02:03.123456+00:00",
            "FAIL",
        ),
        (
            "POSTGRESQL",
            "timestamp with time zone",
            6,
            "2026-09-23T01:02:03",
            "FAIL",
        ),
        (
            "POSTGRESQL",
            "timestamp with time zone",
            6,
            "2026-09-23T01:02:03+00:00",
            "PASS",
        ),
        (
            "SQLSERVER",
            "datetime2",
            0,
            "2026-09-23T01:02:03.789123",
            "FAIL",
        ),
        (
            "SQLSERVER",
            "datetime2",
            6,
            "2026-09-23T01:02:03.789123+00:00",
            "FAIL",
        ),
        (
            "SQLSERVER",
            "datetime2",
            6,
            "2026-09-23T01:02:03+00:00",
            "FAIL",
        ),
        (
            "SQLSERVER",
            "datetimeoffset",
            0,
            "2026-09-23T01:02:03.789123-05:00",
            "FAIL",
        ),
        (
            "SQLSERVER",
            "datetimeoffset",
            6,
            "2026-09-23T01:02:03-05:00",
            "PASS",
        ),
        (
            "SQLSERVER",
            "datetimeoffset",
            6,
            "2026-09-23T01:02:03",
            "FAIL",
        ),
        (
            "SQLSERVER",
            "datetime",
            3,
            "2026-09-23T01:02:03",
            "FAIL",
        ),
    ],
)
def test_preflight_requires_exact_timestamp_precision_and_offset_semantics(
    authenticated,
    database,
    delivery_runtime,
    tmp_path,
    sink_type,
    native,
    precision,
    value,
    expected,
):
    source = make_dataset(
        database,
        tmp_path,
        rows=[f"T1,A-001,10.25,{value}"],
        name=f"Timestamp {sink_type} {native} {precision} {expected}",
    )
    destination = create_destination(authenticated, sink_type=sink_type)
    payload = draft_payload(source["version_id"], destination)
    payload["columns"][3] = {
        "source_name": "note",
        "target_name": "label",
        "target_type": "TIMESTAMP",
        "ordinal": 3,
        "nullable": False,
    }
    delivery_runtime.metadata["columns"][3].update(
        {
            "native_type": native,
            "logical_type": "TIMESTAMP",
            "nullable": False,
            "length": None,
            "datetime_precision": precision,
        }
    )

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            source["organization_id"],
            DeliveryDraft.model_validate(payload),
            raise_on_failure=False,
        )

    timestamp_check = check_by_code(result, "TIMESTAMP_COMPATIBLE")[0]
    assert timestamp_check["status"] == expected
    assert result["status"] == expected


def test_preflight_rejects_timezone_less_target_even_when_all_values_are_null(
    authenticated, database, delivery_runtime, tmp_path
):
    source = make_dataset(
        database,
        tmp_path,
        rows=["T1,A-001,10.25,"],
        name="Null timestamp target policy",
    )
    destination = create_destination(authenticated)
    payload = draft_payload(source["version_id"], destination)
    payload["columns"][3] = {
        "source_name": "note",
        "target_name": "label",
        "target_type": "TIMESTAMP",
        "ordinal": 3,
        "nullable": True,
    }
    delivery_runtime.metadata["columns"][3].update(
        {
            "native_type": "timestamp without time zone",
            "logical_type": "TIMESTAMP",
            "nullable": True,
            "length": None,
            "datetime_precision": 6,
        }
    )
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db, source["organization_id"], draft, raise_on_failure=False
        )

    assert result["status"] == "FAIL"
    assert check_by_code(result, "TYPE_COMPATIBLE")[3]["status"] == "FAIL"
    assert check_by_code(result, "TIMESTAMP_COMPATIBLE")[0]["status"] == "FAIL"


@pytest.mark.parametrize("sink_type", ["POSTGRESQL", "SQLSERVER"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-23T01:02:03.123456-05:00", "PASS"),
        ("2026-09-23T01:02:03.123456", "FAIL"),
    ],
)
def test_create_target_preflight_enforces_offset_aware_timestamp_contract(
    authenticated,
    database,
    delivery_runtime,
    tmp_path,
    sink_type,
    value,
    expected,
):
    source = make_dataset(
        database,
        tmp_path,
        rows=[f"T1,A-001,10.25,{value}"],
        name=f"Create timestamp {sink_type} {expected}",
    )
    destination = create_destination(authenticated, sink_type=sink_type)
    payload = draft_payload(source["version_id"], destination)
    payload["target"] = {
        "mode": "CREATE_TABLE",
        "schema_name": "sales",
        "table_name": "new_timestamp_target",
        "create_schema": False,
    }
    payload["write_strategy"] = "CREATE_AND_LOAD"
    payload["columns"][3] = {
        "source_name": "note",
        "target_name": "label",
        "target_type": "TIMESTAMP",
        "ordinal": 3,
        "nullable": False,
    }

    with database() as db:
        result = delivery_service.preflight_delivery(
            db,
            source["organization_id"],
            DeliveryDraft.model_validate(payload),
            raise_on_failure=False,
        )

    assert result["status"] == expected


def test_invalid_timestamp_preflight_returns_controlled_failure(
    authenticated, database, delivery_runtime, tmp_path
):
    source = make_dataset(
        database,
        tmp_path,
        rows=["T1,A-001,10.25,2026-09-23T01:02:03.1234567+00:00"],
        name="Invalid timestamp precision",
    )
    destination = create_destination(authenticated)
    payload = draft_payload(source["version_id"], destination)
    payload["columns"][3] = {
        "source_name": "note",
        "target_name": "label",
        "target_type": "TIMESTAMP",
        "ordinal": 3,
        "nullable": False,
    }
    delivery_runtime.metadata["columns"][3].update(
        {
            "native_type": "timestamp with time zone",
            "logical_type": "TIMESTAMP",
            "nullable": False,
            "length": None,
            "datetime_precision": 6,
        }
    )
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db, source["organization_id"], draft, raise_on_failure=False
        )
        with pytest.raises(DeliveryOperationError) as error:
            delivery_service.preflight_delivery(
                db, source["organization_id"], draft
            )

    assert result["status"] == "FAIL"
    assert error.value.status == 412
    assert error.value.code == "FAILED_PRECONDITION"
    assert "VALUE_TYPE_MISMATCH" in json.dumps(error.value.details)


def test_preflight_missing_source_column_is_a_controlled_failure(database, delivery_case):
    payload = deepcopy(delivery_case.draft)
    payload["columns"][0]["source_name"] = "column_removed_after_publish"
    draft = DeliveryDraft.model_validate(payload)

    with database() as db, pytest.raises(DeliveryOperationError) as error:
        delivery_service.preflight_delivery(
            db, delivery_case.source["organization_id"], draft
        )

    assert error.value.status == 412
    assert error.value.code == "FAILED_PRECONDITION"
    assert "SOURCE_COLUMNS" in json.dumps(error.value.details)


def test_preflight_storage_integrity_failure_is_controlled_and_sanitized(
    monkeypatch, database, delivery_case
):
    def corrupt_artifact(_artifact):
        raise ValueError("private-storage-path and source row must not escape")

    monkeypatch.setattr(
        delivery_service.storage_provider, "materialize", corrupt_artifact
    )
    with database() as db, pytest.raises(DeliveryOperationError) as error:
        delivery_service.preflight_delivery(
            db,
            delivery_case.source["organization_id"],
            DeliveryDraft.model_validate(delivery_case.draft),
        )

    assert error.value.status == 412
    assert error.value.code == "FAILED_PRECONDITION"
    assert "private-storage-path" not in error.value.message
    assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)


def test_preflight_detects_composite_keys_duplicate_after_type_conversion(
    authenticated, database, delivery_runtime, tmp_path
):
    source = make_dataset(
        database,
        tmp_path,
        rows=["T1,A-001,10.25,true", "T2,A-001,20.50,1"],
        name="Normalized duplicate keys",
    )
    destination = create_destination(authenticated)
    payload = draft_payload(source["version_id"], destination, strategy="UPSERT")
    payload["columns"][3].update(
        {"target_type": "BOOLEAN", "length": None, "nullable": False}
    )
    payload["upsert_keys"] = ["external_id", "label"]
    delivery_runtime.metadata["columns"][3].update(
        {
            "native_type": "boolean",
            "logical_type": "BOOLEAN",
            "length": None,
            "nullable": False,
        }
    )
    delivery_runtime.metadata["constraints"] = [
        {"type": "UNIQUE", "columns": ["external_id", "label"]}
    ]
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db, source["organization_id"], draft, raise_on_failure=False
        )

    assert result["status"] == "FAIL"
    assert check_by_code(result, "UPSERT_SOURCE_KEYS")[0]["status"] == "FAIL"


def test_preflight_rejects_values_outside_the_real_target_integer_range(
    authenticated, database, delivery_runtime, tmp_path
):
    source = make_dataset(
        database,
        tmp_path,
        rows=["T1,A-001,10.25,1", "T2,A-002,20.50,40000"],
        name="Native integer range",
    )
    destination = create_destination(authenticated)
    payload = draft_payload(source["version_id"], destination)
    payload["columns"][3] = {
        "source_name": "note",
        "target_name": "label",
        "target_type": "INT64",
        "ordinal": 3,
        "nullable": False,
    }
    delivery_runtime.metadata["columns"][3].update(
        {
            "native_type": "smallint",
            "logical_type": "INT64",
            "nullable": False,
            "length": None,
        }
    )
    draft = DeliveryDraft.model_validate(payload)

    with database() as db:
        result = delivery_service.preflight_delivery(
            db, source["organization_id"], draft, raise_on_failure=False
        )
        with pytest.raises(DeliveryOperationError) as error:
            delivery_service.preflight_delivery(db, source["organization_id"], draft)

    assert check_by_code(result, "INTEGER_RANGE_COMPATIBLE")[0]["status"] == "FAIL"
    assert error.value.status == 412
    assert error.value.code == "FAILED_PRECONDITION"
    assert "INTEGER_RANGE_COMPATIBLE" in json.dumps(error.value.details)


def test_configuration_persists_exact_sorted_snapshot_and_immutable_version(
    authenticated, database, delivery_case
):
    requested = deepcopy(delivery_case.draft)
    requested["columns"] = list(reversed(requested["columns"]))
    expected = DeliveryDraft.model_validate(requested).snapshot()
    config = publish_configuration(authenticated, delivery_case, **requested)

    assert config["config"] == expected
    with database() as db:
        stored = db.get(Configuration, config["id"])
        assert stored.config == expected
        original_snapshot = deepcopy(stored.config)
    updated_destination = authenticated.patch(
        f"/api/v1/delivery/destinations/{delivery_case.destination['id']}",
        json={"version": 1, "name": "Later destination name"},
    )
    assert updated_destination.status_code == 200
    with database() as db:
        assert db.get(Configuration, config["id"]).config == original_snapshot


def test_http_idempotency_creates_one_delivery_run_and_delivery_lane_job(
    authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    assert config["destination_name"] == "Certified warehouse"
    assert config["destination_version"] == 1
    first = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "delivery-once",
    )
    replay = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "delivery-once",
    )

    assert replay["id"] == first["id"]
    conflict = authenticated.post(
        "/api/v1/delivery/runs",
        json={"configuration_id": config["id"], "dataset_version_id": "different-version"},
        headers={"Idempotency-Key": "delivery-once"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(func.count()).select_from(IdempotencyKey)) == 1
        run = db.get(Run, first["id"])
        assert run.execution_plan["destination_name"] == "Certified warehouse"
        assert run.execution_plan["destination_version"] == 1
        job = db.scalar(select(Job).where(Job.run_id == first["id"]))
        assert job.lane == "DELIVERY"
        links = db.scalars(select(ArtifactLink).where(ArtifactLink.source_id.in_(
            [delivery_case.source["version_id"], first["id"]]
        ))).all()
        assert {(link.relation, link.source_type, link.target_type) for link in links} >= {
            ("DELIVERY_INPUT", "DATASET_VERSION", "RUN"),
            ("DELIVERED_TO", "RUN", "DELIVERY_DESTINATION_VERSION"),
        }


def execute_queued(database, run_id: str) -> None:
    with database() as db:
        execute_delivery_run(db, db.get(Run, run_id))


def test_committed_attempt_publishes_sanitized_receipt_manifest_lineage_and_audit(
    authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "committed-delivery",
    )
    delivery_case.runtime.calls.clear()
    execute_queued(database, run_response["id"])
    deliveries = [call for call in delivery_case.runtime.calls if call[0] == "deliver"]
    assert len(deliveries) == 1
    execute_queued(database, run_response["id"])
    assert [call for call in delivery_case.runtime.calls if call[0] == "deliver"] == deliveries

    with database() as db:
        run = db.get(Run, run_response["id"])
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run.id))
        assert run.status == "SUCCESS"
        assert run.decision == "COMMITTED"
        assert attempt.status == "COMMITTED"
        assert (attempt.rows_written, attempt.rows_inserted, attempt.rows_updated) == (2, 1, 1)
        assert attempt.remote_reference == "remote-transaction-7"
        artifacts = db.scalars(
            select(Artifact).where(Artifact.kind.in_({"DELIVERY_RECEIPT", "RUN_MANIFEST"}))
        ).all()
        assert {artifact.kind for artifact in artifacts} == {
            "DELIVERY_RECEIPT",
            "RUN_MANIFEST",
        }
        payloads = {
            artifact.kind: json.loads(
                Path(delivery_service.storage_provider.materialize(artifact)).read_text(
                    encoding="utf-8"
                )
            )
            for artifact in artifacts
        }
        receipt = payloads["DELIVERY_RECEIPT"]
        manifest = payloads["RUN_MANIFEST"]
        assert receipt["run_id"] == run.id
        assert receipt["dataset_version_id"] == delivery_case.source["version_id"]
        assert receipt["destination_version_id"] == delivery_case.destination[
            "destination_version_id"
        ]
        assert receipt["destination_name"] == "Certified warehouse"
        assert receipt["destination_version"] == 1
        assert receipt["result"] == "COMMITTED"
        assert receipt["rows_written"] == 2
        assert manifest["schema_version"] == 2
        assert manifest["module"] == "DELIVERY"
        assert manifest["configuration"]["config"] == DeliveryDraft.model_validate(
            delivery_case.draft
        ).snapshot()
        assert manifest["delivery"]["attempt"]["status"] == "COMMITTED"
        assert manifest["delivery"]["destination"]["config_hash"] == (
            delivery_case.destination["config_hash"]
        )
        assert manifest["delivery"]["destination"]["destination_name"] == (
            "Certified warehouse"
        )
        assert manifest["delivery"]["destination"]["destination_version"] == 1
        serialized_evidence = json.dumps(payloads, ensure_ascii=False)
        for forbidden in (
            PASSWORD,
            "secret_reference",
            "A-001",
            "A-002",
            "alpha",
            "beta",
        ):
            assert forbidden not in serialized_evidence
        links = db.scalars(select(ArtifactLink).where(ArtifactLink.organization_id == run.organization_id)).all()
        assert {link.relation for link in links} >= {
            "DELIVERY_INPUT",
            "DELIVERED_TO",
            "DELIVERY_RECEIPT",
            "EVIDENCE_OF",
            "RUN_OUTPUT",
        }
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.run_id == run.id).order_by(AuditEvent.created_at)
        ).all()
        assert {event.event_type for event in events} >= {
            "DELIVERY_RUN_QUEUED",
            "DELIVERY_STARTED",
            "DELIVERY_COMMITTED",
        }
        audit_json = json.dumps(
            [event.metadata_json for event in events], ensure_ascii=False
        )
        assert PASSWORD not in audit_json
        assert "secret_reference" not in audit_json
        queued = next(event for event in events if event.event_type == "DELIVERY_RUN_QUEUED")
        assert queued.metadata_json["destination_version_id"] == (
            delivery_case.destination["destination_version_id"]
        )
        assert queued.metadata_json["write_strategy"] == "APPEND"

    receipt_response = authenticated.get(
        f"/api/v1/delivery/runs/{run_response['id']}/receipt"
    )
    assert receipt_response.status_code == 200
    assert receipt_response.json()["result"] == "COMMITTED"
    assert PASSWORD not in receipt_response.text


@pytest.mark.parametrize(
    ("ambiguous", "expected_status", "expected_event"),
    [
        (False, "FAILED", "DELIVERY_FAILED"),
        (True, "UNKNOWN", "DELIVERY_UNKNOWN"),
    ],
)
def test_remote_failure_records_failed_or_unknown_without_evidence(
    authenticated,
    database,
    delivery_case,
    ambiguous,
    expected_status,
    expected_event,
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        f"remote-{expected_status.casefold()}",
    )
    delivery_case.runtime.deliver_error = DeliveryError(
        "DESTINATION_COMMIT_UNKNOWN" if ambiguous else "DESTINATION_PERMISSION_DENIED",
        "No fue posible confirmar la escritura remota."
        if ambiguous
        else "La cuenta no tiene permisos de escritura.",
        ambiguous=ambiguous,
    )
    delivery_case.runtime.calls.clear()
    execute_queued(database, run_response["id"])

    with database() as db:
        run = db.get(Run, run_response["id"])
        attempts = db.scalars(
            select(DeliveryAttempt).where(DeliveryAttempt.run_id == run.id)
        ).all()
        assert run.status == expected_status
        assert run.decision == expected_status
        assert len(attempts) == 1
        assert attempts[0].status == expected_status
        assert attempts[0].finished_at is not None
        assert db.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.kind == "DELIVERY_RECEIPT")
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.run_id == run.id,
                AuditEvent.event_type == expected_event,
            )
        ) == 1
    deliveries = [call for call in delivery_case.runtime.calls if call[0] == "deliver"]
    assert len(deliveries) == 1
    attempts_response = authenticated.get(
        f"/api/v1/delivery/runs/{run_response['id']}/attempts"
    )
    assert attempts_response.status_code == 200
    assert attempts_response.json()["items"][0]["status"] == expected_status
    assert PASSWORD not in attempts_response.text
    if ambiguous:
        execute_queued(database, run_response["id"])
        deliveries_after = [call for call in delivery_case.runtime.calls if call[0] == "deliver"]
        assert deliveries_after == deliveries


@pytest.mark.parametrize("remote_succeeded", [True, False])
def test_lost_worker_lease_cannot_overwrite_durable_unknown_remote_outcome(
    authenticated, database, delivery_case, remote_succeeded
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        f"lease-race-{remote_succeeded}",
    )
    with database() as db:
        job = db.scalar(select(Job).where(Job.run_id == run_response["id"]))
        assert job is not None
        job.status = "RUNNING"
        job.lease_owner = "worker-a"
        job.lease_until = utcnow() + timedelta(minutes=5)
        job.attempts = 1
        db.commit()

    def reconcile_from_worker_b():
        with database() as db:
            job = db.scalar(select(Job).where(Job.run_id == run_response["id"]))
            run = db.get(Run, run_response["id"])
            attempt = db.scalar(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.run_id == run_response["id"]
                )
            )
            assert job is not None and run is not None and attempt is not None
            assert attempt.status == "STARTED"
            job.lease_owner = "worker-b"
            job.lease_until = utcnow() + timedelta(minutes=5)
            attempt.status = "UNKNOWN"
            attempt.error_code = "WORKER_CONFIRMATION_LOST"
            attempt.error_message = "El segundo worker conservó la incertidumbre."
            attempt.finished_at = utcnow()
            run.status = "UNKNOWN"
            run.decision = "UNKNOWN"
            run.error = attempt.error_message
            db.commit()

    delivery_case.runtime.deliver_hook = reconcile_from_worker_b
    if not remote_succeeded:
        delivery_case.runtime.deliver_error = DeliveryError(
            "DESTINATION_CONSTRAINT_VIOLATION",
            "El destino rechazó la fila y confirmó rollback.",
        )
    delivery_case.runtime.calls.clear()

    with database() as db, pytest.raises(DeliveryOperationError) as error:
        execute_delivery_run(
            db,
            db.get(Run, run_response["id"]),
            lease_owner="worker-a",
        )

    assert error.value.code == "WORKER_LEASE_LOST"
    with database() as db:
        run = db.get(Run, run_response["id"])
        job = db.scalar(select(Job).where(Job.run_id == run_response["id"]))
        attempt = db.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.run_id == run_response["id"]
            )
        )
        assert run is not None and run.status == "UNKNOWN"
        assert run.decision == "UNKNOWN"
        assert job is not None and job.lease_owner == "worker-b"
        assert attempt is not None and attempt.status == "UNKNOWN"
        assert db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.run_id == run.id,
                AuditEvent.event_type.in_(
                    {"DELIVERY_COMMITTED", "DELIVERY_FAILED"}
                ),
            )
        ) == 0


@pytest.mark.parametrize(
    ("remote_succeeded", "expected_status", "expected_decision"),
    [
        (True, "SUCCESS", "COMMITTED"),
        (False, "FAILED", "FAILED"),
    ],
)
def test_cancellation_after_started_preserves_known_remote_outcome(
    authenticated,
    database,
    delivery_case,
    remote_succeeded,
    expected_status,
    expected_decision,
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        f"cancel-after-started-{remote_succeeded}",
    )
    with database() as db:
        job = db.scalar(select(Job).where(Job.run_id == run_response["id"]))
        assert job is not None
        job.status = "RUNNING"
        job.lease_owner = "worker-a"
        job.lease_until = utcnow() + timedelta(minutes=5)
        job.attempts = 1
        db.commit()

    def cancel_while_remote_transaction_is_in_flight():
        with database() as other:
            run = other.get(Run, run_response["id"])
            assert run is not None and run.status == "RUNNING"
            run.cancel_requested = True
            other.commit()

    delivery_case.runtime.deliver_hook = (
        cancel_while_remote_transaction_is_in_flight
    )
    if not remote_succeeded:
        delivery_case.runtime.deliver_error = DeliveryError(
            "DESTINATION_CONSTRAINT_VIOLATION",
            "El destino confirmó rollback.",
        )

    with database() as db:
        execute_delivery_run(
            db,
            db.get(Run, run_response["id"]),
            lease_owner="worker-a",
        )

    with database() as db:
        run = db.get(Run, run_response["id"])
        attempt = db.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.run_id == run_response["id"]
            )
        )
        assert run is not None and run.cancel_requested is True
        assert run.status == expected_status
        assert run.decision == expected_decision
        assert attempt is not None and attempt.status == expected_decision


def test_cancellation_during_local_preparation_prevents_remote_attempt(
    monkeypatch, authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "cancel-during-local-preparation",
    )
    with database() as db:
        job = db.scalar(select(Job).where(Job.run_id == run_response["id"]))
        assert job is not None
        job.status = "RUNNING"
        job.lease_owner = "worker-a"
        job.lease_until = utcnow() + timedelta(minutes=5)
        job.attempts = 1
        db.commit()

    real_settings_for = delivery_service.settings_for
    calls = 0

    def cancel_during_second_settings_lookup(destination, destination_version):
        nonlocal calls
        calls += 1
        if calls == 2:
            with database() as other:
                run = other.get(Run, run_response["id"])
                job = other.scalar(select(Job).where(Job.run_id == run_response["id"]))
                assert run is not None and job is not None
                run.cancel_requested = True
                run.status = "CANCELLED"
                run.progress_stage = "Cancelado"
                run.finished_at = utcnow()
                job.status = "CANCELLED"
                job.lease_until = None
                other.commit()
        return real_settings_for(destination, destination_version)

    monkeypatch.setattr(
        delivery_service, "settings_for", cancel_during_second_settings_lookup
    )
    delivery_case.runtime.calls.clear()

    with database() as db, pytest.raises(DeliveryOperationError) as error:
        execute_delivery_run(
            db,
            db.get(Run, run_response["id"]),
            lease_owner="worker-a",
        )

    assert error.value.code == "WORKER_LEASE_LOST"
    with database() as db:
        run = db.get(Run, run_response["id"])
        assert run is not None and run.status == "CANCELLED"
        assert db.scalar(
            select(func.count())
            .select_from(DeliveryAttempt)
            .where(DeliveryAttempt.run_id == run.id)
        ) == 0
    assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)


def test_recovery_marks_started_attempt_unknown_and_never_replays_remote_write(
    authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "recover-started",
    )
    with database() as db:
        run = db.get(Run, run_response["id"])
        run.status = "RUNNING"
        db.add(
            DeliveryAttempt(
                organization_id=run.organization_id,
                run_id=run.id,
                destination_version_id=delivery_case.destination["destination_version_id"],
                attempt_number=1,
                idempotency_key="f" * 64,
                status="STARTED",
                target_locator="sales.orders",
                rows_attempted=2,
            )
        )
        db.commit()
    delivery_case.runtime.calls.clear()

    execute_queued(database, run_response["id"])
    execute_queued(database, run_response["id"])

    with database() as db:
        run = db.get(Run, run_response["id"])
        attempts = db.scalars(
            select(DeliveryAttempt).where(DeliveryAttempt.run_id == run.id)
        ).all()
        assert run.status == "UNKNOWN"
        assert run.decision == "UNKNOWN"
        assert len(attempts) == 1
        assert attempts[0].status == "UNKNOWN"
        assert attempts[0].error_code == "WORKER_CONFIRMATION_LOST"
        assert attempts[0].finished_at is not None
    assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)


def test_local_evidence_failure_after_commit_never_replays_remote_write(
    monkeypatch, authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "evidence-repair-without-retry",
    )

    def fail_local_evidence(*_args, **_kwargs):
        raise OSError("local evidence disk unavailable")

    monkeypatch.setattr(
        delivery_service, "_publish_delivery_evidence", fail_local_evidence
    )
    delivery_case.runtime.calls.clear()
    execute_queued(database, run_response["id"])
    deliveries = [call for call in delivery_case.runtime.calls if call[0] == "deliver"]
    assert len(deliveries) == 1

    with database() as db:
        run = db.get(Run, run_response["id"])
        attempt = db.scalar(
            select(DeliveryAttempt).where(DeliveryAttempt.run_id == run.id)
        )
        assert run.status == "SUCCESS"
        assert run.decision == "COMMITTED"
        assert run.progress_stage == "Completado; evidencia local pendiente"
        assert run.metrics["evidence_status"] == "PENDING_REPAIR"
        assert attempt.status == "COMMITTED"
        assert db.scalar(
            select(func.count()).select_from(Artifact).where(
                Artifact.kind == "DELIVERY_RECEIPT"
            )
        ) == 0

    execute_queued(database, run_response["id"])
    assert [call for call in delivery_case.runtime.calls if call[0] == "deliver"] == deliveries


def test_execution_rechecks_preflight_and_stops_schema_drift_before_attempt(
    authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "drift-before-write",
    )
    delivery_case.runtime.tables_by_schema["sales"] = []
    delivery_case.runtime.calls.clear()

    execute_queued(database, run_response["id"])

    with database() as db:
        run = db.get(Run, run_response["id"])
        assert run.status == "FAILED_PRECONDITION"
        assert run.progress_stage == "Preflight fallido"
        assert db.scalar(
            select(func.count()).select_from(DeliveryAttempt).where(
                DeliveryAttempt.run_id == run.id
            )
        ) == 0
    assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)


def test_local_preparation_failure_happens_before_remote_attempt(
    monkeypatch, authenticated, database, delivery_case
):
    config = publish_configuration(authenticated, delivery_case)
    run_response = queue_run(
        authenticated,
        config["id"],
        delivery_case.source["version_id"],
        "credential-lost-after-preflight",
    )
    real_settings_for = delivery_service.settings_for
    calls = 0

    def settings_then_disappear(destination, destination_version):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DeliveryError(
                "DESTINATION_CREDENTIAL_UNAVAILABLE",
                "No fue posible recuperar la credencial del destino.",
            )
        return real_settings_for(destination, destination_version)

    monkeypatch.setattr(delivery_service, "settings_for", settings_then_disappear)
    delivery_case.runtime.calls.clear()

    execute_queued(database, run_response["id"])

    with database() as db:
        run = db.get(Run, run_response["id"])
        assert run.status == "FAILED_PRECONDITION"
        assert run.progress_stage == "Preparación local fallida"
        assert db.scalar(
            select(func.count()).select_from(DeliveryAttempt).where(
                DeliveryAttempt.run_id == run.id
            )
        ) == 0
    assert not any(call[0] == "deliver" for call in delivery_case.runtime.calls)
