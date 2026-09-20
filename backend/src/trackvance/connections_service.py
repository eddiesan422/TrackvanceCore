"""Source configuration lifecycle, snapshot acquisition and organization-scoped lineage.

Drivers live behind DatasetSource; neither quality rules nor workers open a database
connection. A completed acquisition produces the same immutable canonical Parquet as
file ingestion, and later executions consume that snapshot.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifactstore import link_artifact
from .credential_store import secret_store
from .dataset_sources import ConnectionSettings, SourceError, source_registry
from .db import iso, utcnow
from .manifests import configuration_hash
from .models import (
    Dataset,
    DatasetSourceBinding,
    DatasetVersion,
    ExternalConnection,
    ExternalConnectionVersion,
    User,
    uid,
)
from .services import audit, create_version


class ConnectionOperationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def owned_connection(db: Session, connection_id: str, user: User, *, lock=False) -> ExternalConnection:
    query = select(ExternalConnection).where(
        ExternalConnection.id == connection_id,
        ExternalConnection.organization_id == user.organization_id,
        ExternalConnection.deleted.is_(False),
    )
    if lock:
        query = query.with_for_update()
    connection = db.scalar(query)
    if connection is None:
        raise ConnectionOperationError(404, "NOT_FOUND", "No se encontró la conexión solicitada.")
    return connection


def current_version(db: Session, connection: ExternalConnection) -> ExternalConnectionVersion:
    version = db.scalar(select(ExternalConnectionVersion).where(
        ExternalConnectionVersion.connection_id == connection.id,
        ExternalConnectionVersion.organization_id == connection.organization_id,
        ExternalConnectionVersion.version == connection.version,
    ))
    if version is None:
        raise ConnectionOperationError(409, "CONNECTION_CONFIGURATION_MISSING", "La configuración de la conexión no está disponible.")
    return version


def connection_dto(db: Session, connection: ExternalConnection) -> dict[str, Any]:
    version = current_version(db, connection)
    return {
        "id": connection.id, "name": connection.name, "source_type": connection.source_type,
        "enabled": connection.enabled, "version": connection.version,
        "host": version.host, "port": version.port, "database": version.database,
        "username": version.username, "options": version.options,
        "connection_version_id": version.id, "config_hash": version.config_hash,
        "last_test_status": connection.last_test_status, "last_test_at": iso(connection.last_test_at),
        "last_test_message": connection.last_test_message,
        "created_at": iso(connection.created_at), "updated_at": iso(connection.updated_at),
    }


def settings_for(connection: ExternalConnection, version: ExternalConnectionVersion) -> ConnectionSettings:
    return ConnectionSettings(
        source_type=connection.source_type, host=version.host, port=version.port,
        database=version.database, username=version.username,
        password=secret_store.get(connection.organization_id, version.secret_reference),
        options=version.options,
    )


def require_password_for_endpoint_change(
    connection: ExternalConnection,
    version: ExternalConnectionVersion,
    *,
    host: str,
    port: int,
    database: str,
    username: str,
    options: dict,
) -> None:
    """Do not forward a stored credential to a different authentication endpoint."""
    tls_key = "sslmode" if connection.source_type == "POSTGRESQL" else "encryption"
    previous_endpoint = (
        version.host,
        version.port,
        version.database,
        version.username,
        version.options.get(tls_key, "require"),
    )
    requested_endpoint = (
        host,
        port,
        database,
        username,
        options.get(tls_key, "require"),
    )
    if requested_endpoint != previous_endpoint:
        raise ConnectionOperationError(
            422,
            "PASSWORD_REQUIRED_FOR_ENDPOINT_CHANGE",
            "Vuelve a indicar la contraseña al cambiar el destino, usuario o cifrado de la conexión.",
        )


def saved_source(db: Session, connection: ExternalConnection, *, schema_name=None, object_name=None):
    if not connection.enabled:
        raise ConnectionOperationError(409, "CONNECTION_DISABLED", "Habilita la conexión para consultar su fuente.")
    version = current_version(db, connection)
    return source_registry.create(settings_for(connection, version), schema_name, object_name), version


def test_saved_connection(db: Session, connection: ExternalConnection, user: User) -> dict:
    source, _ = saved_source(db, connection)
    try:
        source.test()
    except SourceError:
        connection.last_test_status = "FAILED"
        connection.last_test_at = utcnow()
        connection.last_test_message = "No se pudo conectar. Revisa acceso, credenciales y disponibilidad."
        audit(db, "CONNECTION_TESTED", "connection", connection.id, "Prueba de conexión fallida", user.name,
              user.organization_id, {"status": "FAILED", "connection_version": connection.version})
        db.commit()
        raise
    connection.last_test_status = "SUCCESS"
    connection.last_test_at = utcnow()
    connection.last_test_message = "Conexión verificada correctamente."
    audit(db, "CONNECTION_TESTED", "connection", connection.id, "Prueba de conexión exitosa", user.name,
          user.organization_id, {"status": "SUCCESS", "connection_version": connection.version})
    db.commit()
    return {"status": "SUCCESS", "message": connection.last_test_message, "tested_at": iso(connection.last_test_at)}


def save_connection(
    db: Session, user: User, values: dict, *, connection: ExternalConnection | None = None,
) -> ExternalConnection:
    """Validate before persisting, and keep every saved configuration immutable."""
    previous = current_version(db, connection) if connection else None
    if connection and values.pop("version") != connection.version:
        raise ConnectionOperationError(409, "VERSION_CONFLICT", "La conexión fue modificada. Actualiza antes de guardar.")
    raw_password = values.pop("password", None)
    password = raw_password.get_secret_value() if raw_password else ""
    source_type = connection.source_type if connection else values["source_type"]
    only_disable = (connection is not None and not password
                    and set(values) <= {"enabled"} and values.get("enabled") is False)
    candidate_host = values.get("host", previous.host if previous else "")
    candidate_port = values.get("port", previous.port if previous else 5432 if source_type == "POSTGRESQL" else 1433)
    candidate_database = values.get("database", previous.database if previous else "")
    candidate_username = values.get("username", previous.username if previous else "")
    candidate_options = values.get("options", previous.options if previous else {})
    if only_disable:
        # Disabling access must remain possible even during a credential-store outage.
        # This placeholder is neither tested nor saved as a credential.
        password = "UNCHANGED_CREDENTIAL"
    elif not password and previous and connection:
        require_password_for_endpoint_change(
            connection,
            previous,
            host=candidate_host,
            port=candidate_port,
            database=candidate_database,
            username=candidate_username,
            options=candidate_options,
        )
        password = secret_store.get(user.organization_id, previous.secret_reference)
    if not password:
        raise ConnectionOperationError(422, "PASSWORD_REQUIRED", "Indica la contraseña de la conexión.")
    settings = ConnectionSettings(
        source_type=source_type,
        host=candidate_host,
        port=candidate_port,
        database=candidate_database,
        username=candidate_username,
        password=password, options=candidate_options,
    )
    if not only_disable:
        source_registry.create(settings).test()
    rotated_secret = previous is None or bool(raw_password and raw_password.get_secret_value())
    if rotated_secret:
        reference = secret_store.put(user.organization_id, password)
    else:
        assert previous is not None
        reference = previous.secret_reference
    try:
        if connection is None:
            connection = ExternalConnection(id=uid(), organization_id=user.organization_id,
                                            name=values["name"], source_type=source_type, version=1)
            db.add(connection)
        else:
            connection.version += 1
            if "name" in values:
                connection.name = values["name"]
        connection.enabled = values.get("enabled", connection.enabled if previous else True)
        connection.updated_at = utcnow()
        if not only_disable:
            connection.last_test_status, connection.last_test_message = "SUCCESS", "Conexión verificada correctamente."
            connection.last_test_at = utcnow()
        config = {"source_type": source_type, "host": settings.host, "port": settings.port,
                  "database": settings.database, "username": settings.username,
                  "options": settings.options, "credential_revision": reference}
        version = ExternalConnectionVersion(
            organization_id=user.organization_id, connection_id=connection.id, version=connection.version,
            host=settings.host, port=settings.port, database=settings.database, username=settings.username,
            options=settings.options, secret_reference=reference, config_hash=configuration_hash(config),
        )
        db.add(version)
        db.flush()
        audit(db, "CONNECTION_UPDATED" if previous else "CONNECTION_CREATED", "connection", connection.id,
              "Configuración de conexión guardada", user.name, user.organization_id,
              {"source_type": source_type, "version": connection.version, "config_hash": version.config_hash,
               "enabled": connection.enabled})
        db.commit()
    except Exception:
        db.rollback()
        if rotated_secret:
            secret_store.delete(user.organization_id, reference)
        raise
    return connection


def acquire_snapshot(
    db: Session, user: User, connection: ExternalConnection, binding: DatasetSourceBinding,
    dataset: Dataset,
) -> DatasetVersion:
    source, configuration = saved_source(db, connection, schema_name=binding.schema_name, object_name=binding.object_name)
    result = source.read()
    source_identity = {
        "source_type": connection.source_type, "connection_id": connection.id,
        "connection_version_id": configuration.id, "connection_version": configuration.version,
        "config_hash": configuration.config_hash, "schema_name": binding.schema_name,
        "object_name": binding.object_name, "object_kind": binding.object_kind,
        "captured_at": iso(utcnow()),
    }
    result.metadata = {**result.metadata, "source": source_identity}
    prior = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)
                      .order_by(DatasetVersion.version.desc()))
    version = create_version(
        db, dataset, None, f"{binding.schema_name}.{binding.object_name}.parquet", user.name,
        source_type=connection.source_type, column_overrides=binding.column_overrides,
        read_result=result,
    )
    link_artifact(db, user.organization_id, "SOURCE_SNAPSHOT", "DATASET_VERSION", version.id,
                  "CONNECTION_VERSION", configuration.id)
    if prior:
        link_artifact(db, user.organization_id, "REFRESH_OF", "DATASET_VERSION", version.id,
                      "DATASET_VERSION", prior.id)
    audit(db, "SOURCE_SNAPSHOT_CREATED", "dataset_version", version.id, "Snapshot de fuente externa creado",
          user.name, user.organization_id, {**source_identity, "dataset_id": dataset.id,
                                           "canonical_artifact_id": version.canonical_artifact_id})
    return version
