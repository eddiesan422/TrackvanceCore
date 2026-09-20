"""HTTP/persistence contract independent of the external database driver.

Real PostgreSQL and SQL Server are covered by scripts/tests/connections_cycle.py.
These tests keep transaction, RBAC and historical-snapshot guarantees fast and deterministic.
"""

import json
from types import SimpleNamespace

import polars as pl
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from trackvance import connections_api, connections_service
from trackvance.credential_store import EncryptedFileSecretStore, SecretStoreError
from trackvance.dataset_readers import DatasetReadResult
from trackvance.dataset_sources import SourceError
from trackvance.models import (
    Artifact,
    AuditEvent,
    Configuration,
    Dataset,
    DatasetSourceBinding,
    DatasetVersion,
    ExternalConnection,
    ExternalConnectionVersion,
    User,
)
from trackvance.services import enqueue
from trackvance.worker import process_once

PASSWORD = "private-test-credential-42"


@pytest.fixture
def external(monkeypatch, tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "credentials", tmp_path / "key" / "master.key")
    monkeypatch.setattr(connections_api, "secret_store", store)
    monkeypatch.setattr(connections_service, "secret_store", store)
    state = SimpleNamespace(test_error=None, read_error=None, tested=[], reads=[], settings=[],
                            objects=[{"name": "orders", "kind": "TABLE"},
                                     {"name": "orders_view", "kind": "VIEW"}], store=store)

    class Source:
        def __init__(self, settings, schema_name, object_name):
            self.settings, self.schema_name, self.object_name = settings, schema_name, object_name

        def test(self):
            state.tested.append(self.settings)
            if state.test_error:
                raise state.test_error

        def schemas(self):
            return ["sales"]

        def objects(self, _schema_name):
            return state.objects

        def read(self, options=None, *, inspect=False):
            if state.read_error:
                raise state.read_error
            state.reads.append((self.schema_name, self.object_name, options, inspect))
            frame = pl.DataFrame({"document_id": ["001234", "001234", None],
                                  "amount": ["10.25", "-2", "5"],
                                  "business_date": ["2026-01-01", "2026-01-02", None]})
            if inspect:
                frame = frame.head(options["limit"])
            return DatasetReadResult(
                frame=frame, source_format=self.settings.source_type, format_label="External DB",
                media_type="application/vnd.apache.parquet", row_numbering="SNAPSHOT_ROW",
                native_schema={"document_id": "String", "amount": "Decimal", "business_date": "Date"},
                metadata={"source_native_schema": [
                    {"name": "document_id", "native_type": "varchar", "logical_type": "STRING", "nullable": True, "numeric": False},
                    {"name": "amount", "native_type": "numeric", "logical_type": "DECIMAL", "nullable": True, "numeric": True},
                    {"name": "business_date", "native_type": "date", "logical_type": "DATE", "nullable": True, "numeric": False},
                ]},
            )

    def create(settings, schema_name=None, object_name=None):
        state.settings.append(settings)
        return Source(settings, schema_name, object_name)

    monkeypatch.setattr(connections_service.source_registry, "create", create)
    return state


def body(source_type="POSTGRESQL", **values):
    return {"name": "Source orders", "source_type": source_type, "host": "external-db",
            "port": 5432 if source_type == "POSTGRESQL" else 1433, "database": "warehouse",
            "username": "readonly", "password": PASSWORD,
            "options": {"sslmode": "require"} if source_type == "POSTGRESQL" else {"encryption": "require"},
            **values}


def create(authenticated, source_type="POSTGRESQL"):
    response = authenticated.post("/api/v1/connections", json=body(source_type))
    assert response.status_code == 201, response.text
    return response.json()


def register(authenticated, connection_id, **values):
    return authenticated.post(f"/api/v1/connections/{connection_id}/datasets", json={
        "name": "Connected orders", "schema_name": "sales", "object_name": "orders", **values,
    })


@pytest.mark.parametrize("source_type", ["POSTGRESQL", "SQLSERVER"])
def test_create_discovers_previews_and_never_exposes_credentials(authenticated, database, external, source_type):
    draft = authenticated.post("/api/v1/connections/test", json=body(source_type))
    assert draft.status_code == 200 and draft.json()["status"] == "SUCCESS"
    with database() as db:
        assert db.scalar(select(ExternalConnection)) is None
    connection = create(authenticated, source_type)
    assert connection["version"] == 1 and connection["last_test_status"] == "SUCCESS"
    assert len(external.tested) == 2  # The server re-tests before persisting.
    url = f"/api/v1/connections/{connection['id']}"
    assert authenticated.get(url + "/schemas").json()["items"] == ["sales"]
    assert authenticated.get(url + "/objects", params={"schema_name": "sales"}).json()["items"][1]["kind"] == "VIEW"
    preview = authenticated.get(url + "/preview", params={"schema_name": "sales", "object_name": "orders_view", "limit": 2})
    assert preview.status_code == 200 and preview.json()["sampled_rows"] == 2
    assert preview.json()["rows"][0]["document_id"] == "001234"
    assert preview.json()["columns"][1]["logical_type"] == "DECIMAL"
    assert external.reads == [("sales", "orders_view", {"limit": 2}, True)]
    for response in (authenticated.get(url), authenticated.get("/api/v1/connections"),
                     authenticated.get("/api/v1/audit-events")):
        assert PASSWORD not in response.text and "secret_reference" not in response.text
    with database() as db:
        revision = db.scalar(select(ExternalConnectionVersion))
        assert PASSWORD not in json.dumps({column.name: getattr(revision, column.name)
                                          for column in revision.__table__.columns}, default=str)
        assert external.store.get(revision.organization_id, revision.secret_reference) == PASSWORD
        event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "CONNECTION_PREVIEWED"))
        assert event.actor_id == "test-user" and event.actor_type == "USER" and event.request_id
        assert event.metadata_json["schema_name"] == "sales"
        assert event.metadata_json["connection_version"] == 1


def test_edit_rotates_credentials_preserves_history_and_rejects_stale_writers(authenticated, database, external):
    first = create(authenticated)
    url = f"/api/v1/connections/{first['id']}"
    second = authenticated.patch(url, json={"version": 1, "host": "new-host", "password": "rotated-password"})
    assert second.status_code == 200 and second.json()["version"] == 2
    assert second.json()["config_hash"] != first["config_hash"]
    assert authenticated.patch(url, json={"version": 1, "host": "stale"}).status_code == 409
    reuse = authenticated.post(
        "/api/v1/connections/test",
        json=body(connection_id=first["id"], host="new-host", password=""),
    )
    assert reuse.status_code == 200 and external.tested[-1].password == "rotated-password"
    with database() as db:
        versions = db.scalars(select(ExternalConnectionVersion).order_by(ExternalConnectionVersion.version)).all()
        assert [v.host for v in versions] == ["external-db", "new-host"]
        assert [external.store.get(v.organization_id, v.secret_reference) for v in versions] == [PASSWORD, "rotated-password"]


@pytest.mark.parametrize("operation", ["draft", "patch"])
@pytest.mark.parametrize(("source_type", "change"), [
    ("POSTGRESQL", {"host": "other-host"}),
    ("POSTGRESQL", {"port": 5433}),
    ("POSTGRESQL", {"database": "other-warehouse"}),
    ("POSTGRESQL", {"username": "other-reader"}),
    ("POSTGRESQL", {"options": {"sslmode": "verify-full"}}),
    ("SQLSERVER", {"options": {"encryption": "off"}}),
])
def test_saved_password_is_not_reused_for_endpoint_changes(
    authenticated, database, external, operation, source_type, change,
):
    connection = create(authenticated, source_type)
    tested_before = len(external.tested)
    if operation == "draft":
        response = authenticated.post(
            "/api/v1/connections/test",
            json=body(source_type, connection_id=connection["id"], password="", **change),
        )
    else:
        response = authenticated.patch(
            f"/api/v1/connections/{connection['id']}",
            json={"version": connection["version"], **change},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PASSWORD_REQUIRED_FOR_ENDPOINT_CHANGE"
    assert len(external.tested) == tested_before  # Reject before opening the changed endpoint.
    with database() as db:
        saved = db.get(ExternalConnection, connection["id"])
        assert saved.version == connection["version"]


def test_saved_password_can_be_reused_for_name_and_timeout_changes(authenticated, external):
    connection = create(authenticated)
    options = {"sslmode": "require", "connect_timeout": 8, "query_timeout": 45}
    draft = authenticated.post(
        "/api/v1/connections/test",
        json=body(connection_id=connection["id"], password="", name="Renamed", options=options),
    )
    assert draft.status_code == 200, draft.text
    assert external.tested[-1].password == PASSWORD
    assert external.tested[-1].options == options

    updated = authenticated.patch(
        f"/api/v1/connections/{connection['id']}",
        json={"version": connection["version"], "name": "Renamed", "options": options},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["options"] == options
    assert external.tested[-1].password == PASSWORD


def test_disable_remains_possible_during_outage_and_refresh_fails_closed(authenticated, external, monkeypatch):
    connection = create(authenticated)
    created = register(authenticated, connection["id"]).json()
    url = f"/api/v1/connections/{connection['id']}"
    external.test_error = SourceError("SOURCE_UNAVAILABLE", "Fuente no disponible.")
    monkeypatch.setattr(external.store, "get", lambda *_: (_ for _ in ()).throw(SecretStoreError("Unavailable")))
    disabled = authenticated.patch(url, json={"version": 1, "enabled": False})
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    dataset = authenticated.get(f"/api/v1/datasets/{created['dataset']['id']}").json()
    assert dataset["source_binding"]["connection_state"] == "DISABLED"
    assert dataset["versions"]  # Existing immutable snapshots remain available.
    for path in (url + "/schemas", url + "/objects?schema_name=sales", url + "/preview?schema_name=sales&object_name=orders"):
        assert authenticated.get(path).json()["error"]["code"] == "CONNECTION_DISABLED"
    refresh = authenticated.post(f"/api/v1/datasets/{created['dataset']['id']}/refresh-source")
    assert refresh.status_code == 409 and refresh.json()["error"]["code"] == "CONNECTION_DISABLED"


def test_failed_test_updates_status_without_altering_configuration(authenticated, database, external):
    first = create(authenticated)
    external.test_error = SourceError("SOURCE_AUTH_FAILED", "Credenciales no válidas.")
    url = f"/api/v1/connections/{first['id']}"
    failed = authenticated.post(url + "/test")
    assert failed.status_code == 422
    after = authenticated.get(url).json()
    assert after["last_test_status"] == "FAILED" and after["version"] == first["version"]
    assert after["config_hash"] == first["config_hash"]
    assert authenticated.patch(url, json={"version": 1, "host": "failed-host"}).status_code == 422
    with database() as db:
        assert db.scalar(select(func.count()).select_from(ExternalConnectionVersion)) == 1
    external.test_error = None
    assert authenticated.post(url + "/test").status_code == 200
    assert authenticated.get(url).json()["last_test_status"] == "SUCCESS"


@pytest.mark.parametrize("source_type", ["POSTGRESQL", "SQLSERVER"])
def test_snapshot_refresh_and_intake_preserve_exact_source_lineage(authenticated, database, external, source_type):
    connection = create(authenticated, source_type)
    response = register(authenticated, connection["id"], column_overrides={"amount": {"logical_type": "STRING"}})
    assert response.status_code == 201, response.text
    dataset, first = response.json()["dataset"], response.json()["version"]
    assert dataset["origin"] == source_type
    assert dataset["source_binding"]["connection_id"] == connection["id"]
    assert first["source_type"] == source_type and first["original_artifact_id"] is None
    assert first["row_count"] == 3 and first["canonical_artifact_id"]
    assert {c["name"]: c["logical_type"] for c in first["schema"]}["amount"] == "STRING"
    origin = first["ingestion_metadata"]["source"]
    assert origin["connection_version_id"] == connection["connection_version_id"]
    assert {link["relation"] for link in first["lineage"]} == {"SOURCE_SNAPSHOT"}
    assert authenticated.patch(
        f"/api/v1/connections/{connection['id']}",
        json={"version": 1, "host": "replacement-host", "password": "replacement-password"},
    ).status_code == 200
    second_response = authenticated.post(f"/api/v1/datasets/{dataset['id']}/refresh-source")
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    assert second["version"] == 2 and second["id"] != first["id"]
    assert second["ingestion_metadata"]["source"]["connection_version"] == 2
    assert second["ingestion_metadata"]["source"]["config_hash"] != origin["config_hash"]
    assert any(link["relation"] == "REFRESH_OF" and link["target_id"] == first["id"] for link in second["lineage"])
    with database() as db:
        previous = db.get(DatasetVersion, first["id"])
        assert previous.ingestion_metadata["source"] == origin
        artifact = db.get(Artifact, previous.canonical_artifact_id)
        assert artifact.kind == "CANONICAL_PARQUET"
        assert pl.read_parquet(artifact.path)["document_id"].to_list() == ["001234", "001234", None]
        assert previous.original_path == ""
        config = Configuration(name="Connected Intake", module="intake", dataset_id=dataset["id"],
                               config={"required_columns": ["document_id"], "positive_columns": ["amount"], "max_error_rate": 0})
        db.add(config)
        db.flush()
        run = enqueue(db, config, previous, None, "Test User")
        db.commit()
        run_id = run.id
    external.read_error = SourceError("SOURCE_UNAVAILABLE", "Fuente no disponible.")
    assert process_once("connections-test-worker") is True  # Execution reads only its snapshot.
    run = authenticated.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "SUCCESS" and run["decision"] == "REJECTED"
    assert run["metrics"]["valid_rows"] == 1 and run["metrics"]["error_rows"] == 2
    evidence = authenticated.get(f"/api/v1/runs/{run_id}/evidence").json()
    assert evidence["inputs"][0]["ingestion_metadata"]["source"] == origin
    assert PASSWORD not in json.dumps(evidence)


def test_delete_is_soft_and_keeps_existing_dataset_snapshots(authenticated, database, external):
    connection = create(authenticated)
    result = register(authenticated, connection["id"]).json()
    url = f"/api/v1/connections/{connection['id']}"
    assert authenticated.delete(url, params={"version": connection["version"]}).status_code == 200
    assert authenticated.get(url).status_code == 404
    assert authenticated.get("/api/v1/connections").json()["total"] == 0
    dataset = authenticated.get(f"/api/v1/datasets/{result['dataset']['id']}").json()
    assert dataset["source_binding"]["connection_state"] == "DELETED"
    assert dataset["versions"][0]["id"] == result["version"]["id"]
    assert authenticated.post(f"/api/v1/datasets/{result['dataset']['id']}/refresh-source").status_code == 404
    assert authenticated.get(f"/api/v1/dataset-versions/{result['version']['id']}/profile").status_code == 200
    with database() as db:
        assert db.get(ExternalConnection, connection["id"]).deleted is True
        assert db.scalar(select(ExternalConnectionVersion)) is not None
        assert db.scalar(select(DatasetSourceBinding)) is not None


def test_delete_requires_the_current_optimistic_version(authenticated, database, external):
    connection = create(authenticated)
    url = f"/api/v1/connections/{connection['id']}"
    updated = authenticated.patch(url, json={"version": connection["version"], "name": "Renamed source"})
    assert updated.status_code == 200 and updated.json()["version"] == 2

    assert authenticated.delete(url).status_code == 422
    stale = authenticated.delete(url, params={"version": connection["version"]})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    with database() as db:
        saved = db.get(ExternalConnection, connection["id"])
        assert saved.deleted is False and saved.version == 2

    assert authenticated.delete(url, params={"version": updated.json()["version"]}).status_code == 200


def test_import_failure_rolls_back_dataset_and_version(authenticated, database, external):
    connection = create(authenticated)
    external.read_error = SourceError("SOURCE_SIZE_LIMIT", "Snapshot demasiado grande.")
    response = register(authenticated, connection["id"])
    assert response.status_code == 422
    with database() as db:
        assert db.scalar(select(Dataset)) is None
        assert db.scalar(select(DatasetVersion)) is None
        assert db.scalar(select(DatasetSourceBinding)) is None


def test_invalid_overrides_rollback_without_partial_snapshot(authenticated, database, external):
    connection = create(authenticated)
    response = register(authenticated, connection["id"], column_overrides={"unknown": {"logical_type": "STRING"}})
    assert response.status_code == 422
    with database() as db:
        assert db.scalar(select(Dataset)) is None and db.scalar(select(Artifact)) is None


def test_cross_organization_cannot_discover_test_edit_import_or_refresh(authenticated, database, external):
    connection = create(authenticated)
    result = register(authenticated, connection["id"]).json()
    with database() as db:
        db.get(User, "test-user").organization_id = "other-organization"
        db.commit()
    url = f"/api/v1/connections/{connection['id']}"
    assert authenticated.get("/api/v1/connections").json()["items"] == []
    for response in (authenticated.get(url), authenticated.get(url + "/schemas"),
                     authenticated.post(url + "/test"), authenticated.patch(url, json={"version": 1, "name": "Not mine"}),
                     register(authenticated, connection["id"]), authenticated.delete(url, params={"version": 1}),
                     authenticated.post("/api/v1/connections/test", json=body(connection_id=connection["id"], password="")),
                     authenticated.post(f"/api/v1/datasets/{result['dataset']['id']}/refresh-source")):
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("role,can_use", [("Auditor", False), ("Operations", False), ("Data Analyst", True)])
def test_backend_rbac_enforces_manage_and_source_use(authenticated, database, external, role, can_use):
    connection = create(authenticated)
    with database() as db:
        db.get(User, "test-user").role = role
        db.commit()
    url = f"/api/v1/connections/{connection['id']}"
    assert authenticated.get(url).status_code == 200
    assert authenticated.get(url + "/schemas").status_code == (200 if can_use else 403)
    assert authenticated.post(url + "/test").status_code == (200 if can_use else 403)
    assert register(authenticated, connection["id"]).status_code == (201 if can_use else 403)
    for response in (authenticated.post("/api/v1/connections", json=body()),
                     authenticated.post("/api/v1/connections/test", json=body()),
                     authenticated.patch(url, json={"version": 1, "enabled": False}),
                     authenticated.delete(url, params={"version": 1})):
        assert response.status_code == 403


@pytest.mark.parametrize("values", [
    {"password": ""}, {"options": {"password": PASSWORD}}, {"options": {"connection_string": PASSWORD}},
    {"options": {"sslmode": PASSWORD}}, {"password": PASSWORD * 100},
    {"options": {"sslmode": [PASSWORD]}}, {"options": {"sslmode": {"password": PASSWORD}}},
    {"host": "postgresql://user:" + PASSWORD + "@host/database"},
])
def test_invalid_inputs_never_echo_credentials(authenticated, database, external, values, caplog):
    response = authenticated.post("/api/v1/connections", json=body(**values))
    assert response.status_code == 422
    assert PASSWORD not in response.text and PASSWORD not in caplog.text
    with database() as db:
        assert db.scalar(select(ExternalConnection)) is None


def test_bounds_and_selection_validation_precede_import(authenticated, external):
    connection = create(authenticated)
    url = f"/api/v1/connections/{connection['id']}/preview"
    for limit in (0, 101):
        assert authenticated.get(url, params={"schema_name": "sales", "object_name": "orders", "limit": limit}).status_code == 422
    external.objects = []
    assert register(authenticated, connection["id"]).status_code == 404
    assert external.reads == []


def test_quoted_source_names_are_not_silently_trimmed(authenticated, external):
    connection = create(authenticated)
    external.objects = [{"name": " orders ", "kind": "TABLE"}]
    response = register(authenticated, connection["id"], schema_name=" sales ", object_name=" orders ")
    assert response.status_code == 201
    assert external.reads[0][:2] == (" sales ", " orders ")


def test_credentials_are_not_silently_trimmed(authenticated, external, database):
    password = "  credential with outer spaces  "
    response = authenticated.post("/api/v1/connections", json=body(password=password, username=" reader "))
    assert response.status_code == 201
    assert external.tested[-1].password == password
    assert external.tested[-1].username == " reader "
    with database() as db:
        revision = db.scalar(select(ExternalConnectionVersion))
        assert external.store.get(revision.organization_id, revision.secret_reference) == password


@pytest.mark.parametrize("existing", [False, True])
def test_failed_commit_removes_only_new_credential_and_preserves_history(
    authenticated, database, external, monkeypatch, existing,
):
    connection_id = create(authenticated)["id"] if existing else None
    before = set(external.store.root.glob("*/*.secret"))
    with database() as db:
        user = db.get(User, "test-user")
        connection = db.get(ExternalConnection, connection_id) if connection_id else None
        payload = (connections_api.ConnectionPatch(version=1, password="new-private-credential").model_dump(exclude_none=True)
                   if existing else connections_api.ConnectionBody(**body()).model_dump())

        def fail_commit():
            raise IntegrityError("commit", {}, Exception("simulated persistence failure"))

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(IntegrityError):
            connections_service.save_connection(db, user, payload, connection=connection)
    assert set(external.store.root.glob("*/*.secret")) == before
    with database() as db:
        versions = db.scalars(select(ExternalConnectionVersion)).all()
        assert len(versions) == int(existing)
        if existing:
            assert db.get(ExternalConnection, connection_id).version == 1
            assert external.store.get(versions[0].organization_id, versions[0].secret_reference) == PASSWORD
