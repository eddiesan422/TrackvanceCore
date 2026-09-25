"""Application services for immutable Delivery configurations and remote attempts."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import polars as pl
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from . import __version__
from .artifactstore import ArtifactIntegrityError, artifact_dto, link_artifact, storage_provider
from .audit_context import Actor
from .config import MAX_ROWS
from .credential_store import SecretStoreError
from .data_sinks import (
    LOGICAL_TYPES,
    DeliveryError,
    DestinationSettings,
    convert_value,
    decimal_capacity_for_native,
    integer_bounds_for_native,
    sink_registry,
    sqlserver_collation_supports_supplementary,
    string_storage_is_exact,
    technical_type,
    timestamp_policy_for_native,
    utf16_code_units,
)
from .db import iso, require_record, utcnow
from .delivery_credential_store import destination_secret_store
from .delivery_metrics import delivery_metric_semantics
from .delivery_schemas import DeliveryDraft, DeliveryReviewBody
from .jobqueue import JobQueue, job_queue
from .manifests import SCHEMA_VERSION, configuration_hash
from .models import (
    Artifact,
    ArtifactLink,
    Configuration,
    Dataset,
    DatasetVersion,
    DeliveryAttempt,
    DeliveryDestination,
    DeliveryDestinationVersion,
    DeliveryOperationalReview,
    Job,
    Run,
    User,
    uid,
)
from .services import audit, run_actor

DELIVERY_LEASE_FENCE_SECONDS = 90


class DeliveryOperationError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Any | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def owned_destination(
    db: Session,
    destination_id: str,
    user: User,
    *,
    lock: bool = False,
) -> DeliveryDestination:
    query = select(DeliveryDestination).where(
        DeliveryDestination.id == destination_id,
        DeliveryDestination.organization_id == user.organization_id,
        DeliveryDestination.deleted.is_(False),
    )
    if lock:
        query = query.with_for_update()
    destination = db.scalar(query)
    if destination is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró el destino solicitado.")
    return destination


def current_destination_version(
    db: Session, destination: DeliveryDestination
) -> DeliveryDestinationVersion:
    version = db.scalar(
        select(DeliveryDestinationVersion).where(
            DeliveryDestinationVersion.destination_id == destination.id,
            DeliveryDestinationVersion.organization_id == destination.organization_id,
            DeliveryDestinationVersion.version == destination.version,
        )
    )
    if version is None:
        raise DeliveryOperationError(
            409,
            "DESTINATION_CONFIGURATION_MISSING",
            "La configuración del destino no está disponible.",
        )
    return version


def exact_destination_version(
    db: Session,
    destination: DeliveryDestination,
    version_id: str,
) -> DeliveryDestinationVersion:
    version = db.scalar(
        select(DeliveryDestinationVersion).where(
            DeliveryDestinationVersion.id == version_id,
            DeliveryDestinationVersion.destination_id == destination.id,
            DeliveryDestinationVersion.organization_id == destination.organization_id,
        )
    )
    if version is None:
        raise DeliveryOperationError(
            404,
            "DESTINATION_VERSION_NOT_FOUND",
            "No se encontró la revisión inmutable del destino.",
        )
    return version


def destination_dto(db: Session, destination: DeliveryDestination) -> dict[str, Any]:
    version = current_destination_version(db, destination)
    config = version.config
    return {
        "id": destination.id,
        "name": destination.name,
        "sink_type": destination.sink_type,
        "enabled": destination.enabled,
        "version": destination.version,
        "host": config["host"],
        "port": config["port"],
        "database": config["database"],
        "username": config["username"],
        "options": config.get("options", {}),
        "destination_version_id": version.id,
        "config_hash": version.config_hash,
        "last_test_status": destination.last_test_status,
        "last_test_at": iso(destination.last_test_at),
        "last_test_message": destination.last_test_message,
        "created_at": iso(destination.created_at),
        "updated_at": iso(destination.updated_at),
    }


def settings_for(
    destination: DeliveryDestination, version: DeliveryDestinationVersion
) -> DestinationSettings:
    config = version.config
    try:
        password = destination_secret_store.get(
            destination.organization_id, version.secret_reference
        )
    except SecretStoreError:
        raise DeliveryError(
            "DESTINATION_CREDENTIAL_UNAVAILABLE",
            "No fue posible recuperar la credencial del destino.",
        ) from None
    return DestinationSettings(
        sink_type=destination.sink_type,
        host=config["host"],
        port=config["port"],
        database=config["database"],
        username=config["username"],
        password=password,
        options=config.get("options", {}),
    )


def require_password_for_destination_change(
    destination: DeliveryDestination,
    version: DeliveryDestinationVersion,
    candidate: dict[str, Any],
) -> None:
    tls_key = "sslmode" if destination.sink_type == "POSTGRESQL" else "encryption"
    previous = version.config
    prior_endpoint = (
        previous["host"],
        previous["port"],
        previous["database"],
        previous["username"],
        previous.get("options", {}).get(tls_key, "require"),
    )
    requested_endpoint = (
        candidate["host"],
        candidate["port"],
        candidate["database"],
        candidate["username"],
        candidate.get("options", {}).get(tls_key, "require"),
    )
    if requested_endpoint != prior_endpoint:
        raise DeliveryOperationError(
            422,
            "PASSWORD_REQUIRED_FOR_ENDPOINT_CHANGE",
            "Vuelve a indicar la contraseña al cambiar el destino, usuario o cifrado.",
        )


def save_destination(
    db: Session,
    user: User,
    values: dict[str, Any],
    *,
    destination: DeliveryDestination | None = None,
) -> DeliveryDestination:
    previous = current_destination_version(db, destination) if destination else None
    if destination and values.pop("version") != destination.version:
        raise DeliveryOperationError(
            409, "VERSION_CONFLICT", "El destino fue modificado. Actualiza antes de guardar."
        )
    raw_password = values.pop("password", None)
    password = raw_password.get_secret_value() if raw_password else ""
    sink_type = destination.sink_type if destination else values.pop("sink_type")
    if destination and values.get("sink_type") and values["sink_type"] != sink_type:
        raise DeliveryOperationError(
            422, "SINK_TYPE_IMMUTABLE", "El motor de un destino existente no puede cambiar."
        )
    values.pop("sink_type", None)
    prior_config = previous.config if previous else {}
    config = {
        "host": values.get("host", prior_config.get("host", "")),
        "port": values.get("port", prior_config.get("port", 5432 if sink_type == "POSTGRESQL" else 1433)),
        "database": values.get("database", prior_config.get("database", "")),
        "username": values.get("username", prior_config.get("username", "")),
        "options": values.get("options", prior_config.get("options", {})),
    }
    only_disable = (
        destination is not None
        and not password
        and set(values) <= {"enabled"}
        and values.get("enabled") is False
    )
    if only_disable:
        password = "UNCHANGED_CREDENTIAL"
    elif not password and destination and previous:
        require_password_for_destination_change(destination, previous, config)
        password = destination_secret_store.get(user.organization_id, previous.secret_reference)
    if not password:
        raise DeliveryOperationError(422, "PASSWORD_REQUIRED", "Indica la contraseña del destino.")
    settings = DestinationSettings(
        sink_type=sink_type,
        password=password,
        **config,
    )
    if not only_disable:
        sink_registry.create(settings).test()
    rotated = previous is None or bool(raw_password and raw_password.get_secret_value())
    if rotated:
        reference = destination_secret_store.put(user.organization_id, password)
    else:
        assert previous is not None
        reference = previous.secret_reference
    try:
        if destination is None:
            destination = DeliveryDestination(
                id=uid(),
                organization_id=user.organization_id,
                name=str(values["name"]).strip(),
                sink_type=sink_type,
                version=1,
            )
            db.add(destination)
            event = "DESTINATION_CREATED"
        else:
            destination.version += 1
            if "name" in values:
                destination.name = str(values["name"]).strip()
            event = "DESTINATION_UPDATED"
        destination.enabled = values.get(
            "enabled", destination.enabled if previous is not None else True
        )
        destination.updated_at = utcnow()
        if not only_disable:
            destination.last_test_status = "SUCCESS"
            destination.last_test_at = utcnow()
            destination.last_test_message = "Destino verificado correctamente."
        version = DeliveryDestinationVersion(
            organization_id=user.organization_id,
            destination_id=destination.id,
            version=destination.version,
            config=config,
            secret_reference=reference,
            config_hash=configuration_hash({**config, "credential_revision": reference}),
        )
        db.add(version)
        db.flush()
        if not destination.enabled:
            event = "DESTINATION_DISABLED"
        audit(
            db,
            event,
            "delivery_destination",
            destination.id,
            "Configuración de destino guardada",
            user.name,
            user.organization_id,
            {
                "sink_type": sink_type,
                "version": destination.version,
                "config_hash": version.config_hash,
                "enabled": destination.enabled,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        if rotated:
            destination_secret_store.delete(user.organization_id, reference)
        raise
    return destination


def test_saved_destination(
    db: Session, destination: DeliveryDestination, user: User
) -> dict[str, Any]:
    if not destination.enabled:
        raise DeliveryOperationError(409, "DESTINATION_DISABLED", "Habilita el destino para probarlo.")
    version = current_destination_version(db, destination)
    try:
        sink_registry.create(settings_for(destination, version)).test()
    except DeliveryError as error:
        destination.last_test_status = "FAILED"
        destination.last_test_at = utcnow()
        destination.last_test_message = "No se pudo conectar. Revisa acceso y credenciales."
        audit(
            db,
            "DESTINATION_TESTED",
            "delivery_destination",
            destination.id,
            "Prueba de destino fallida",
            user.name,
            user.organization_id,
            {"status": "FAILED", "error_code": error.code, "version": version.version},
        )
        db.commit()
        raise
    destination.last_test_status = "SUCCESS"
    destination.last_test_at = utcnow()
    destination.last_test_message = "Destino verificado correctamente."
    audit(
        db,
        "DESTINATION_TESTED",
        "delivery_destination",
        destination.id,
        "Prueba de destino exitosa",
        user.name,
        user.organization_id,
        {"status": "SUCCESS", "version": version.version},
    )
    db.commit()
    return {
        "status": "SUCCESS",
        "message": destination.last_test_message,
        "tested_at": iso(destination.last_test_at),
    }


def _owned_version(
    db: Session, version_id: str, organization_id: str
) -> tuple[DatasetVersion, Dataset, Artifact, pl.DataFrame]:
    version = db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == version_id,
            DatasetVersion.organization_id == organization_id,
        )
    )
    if version is None:
        raise DeliveryOperationError(
            412, "FAILED_PRECONDITION", "La DatasetVersion ya no está disponible."
        )
    dataset = db.scalar(
        select(Dataset).where(
            Dataset.id == version.dataset_id,
            Dataset.organization_id == organization_id,
        )
    )
    artifact = db.scalar(
        select(Artifact).where(
            Artifact.id == version.canonical_artifact_id,
            Artifact.organization_id == organization_id,
            Artifact.kind.in_({"CANONICAL_PARQUET", "INTAKE_ACCEPTED"}),
        )
    )
    if dataset is None or artifact is None:
        raise DeliveryOperationError(
            412,
            "FAILED_PRECONDITION",
            "La versión no tiene un artifact canónico verificable.",
        )
    try:
        frame = pl.read_parquet(storage_provider.materialize(artifact))
    except Exception as exc:
        raise DeliveryOperationError(
            412,
            "FAILED_PRECONDITION",
            "El artifact canónico no superó la comprobación de integridad.",
        ) from exc
    return version, dataset, artifact, frame


def preview_delivery(
    db: Session, organization_id: str, draft: DeliveryDraft, *, limit: int = 8
) -> dict[str, Any]:
    version, _dataset, _artifact, frame = _owned_version(
        db, draft.dataset_version_id, organization_id
    )
    destination = db.scalar(
        select(DeliveryDestination).where(
            DeliveryDestination.id == draft.destination_id,
            DeliveryDestination.organization_id == organization_id,
            DeliveryDestination.deleted.is_(False),
        )
    )
    if destination is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró el destino solicitado.")
    exact_destination_version(db, destination, draft.destination_version_id)
    source_names = [column.source_name for column in draft.columns]
    missing = set(source_names) - set(frame.columns)
    if missing:
        raise DeliveryOperationError(
            412,
            "FAILED_PRECONDITION",
            "El mapping contiene columnas que no existen en la DatasetVersion.",
            {"missing_columns": sorted(missing)},
        )
    source_type_mismatches = _source_type_mismatches(version, draft)
    if source_type_mismatches:
        raise DeliveryOperationError(
            412,
            "FAILED_PRECONDITION",
            "Data Delivery no puede cambiar el tipo lógico de una columna.",
            {"source_type_mismatches": source_type_mismatches},
        )
    sample = frame.select(source_names).head(limit).to_dicts()
    destination_rows = [
        {
            column.target_name: row.get(column.source_name)
            for column in draft.columns
        }
        for row in sample
    ]
    columns = [
        {
            "source_name": column.source_name,
            "target_name": column.target_name,
            "ordinal": column.ordinal,
            "source_type": next(
                (
                    item.get("logical_type", "STRING")
                    for item in version.schema_json
                    if item.get("name") == column.source_name
                ),
                "STRING",
            ),
            "target_type": technical_type(destination.sink_type, column.model_dump()),
        }
        for column in draft.columns
    ]
    return {
        "dataset_version_id": version.id,
        "columns": columns,
        "source_rows": sample,
        "destination_rows": destination_rows,
        "sampled_rows": len(sample),
    }


def _check(
    checks: list[dict[str, Any]], code: str, passed: bool, message: str
) -> None:
    checks.append({"code": code, "status": "PASS" if passed else "FAIL", "message": message})


def _source_type_mismatches(
    version: DatasetVersion, draft: DeliveryDraft
) -> list[dict[str, str]]:
    source_schema = {
        str(column.get("name")): str(column.get("logical_type", "")).upper()
        for column in version.schema_json
    }
    mismatches: list[dict[str, str]] = []
    for mapping in draft.columns:
        source_type = source_schema.get(mapping.source_name, "")
        if source_type not in LOGICAL_TYPES or source_type != mapping.target_type:
            mismatches.append(
                {
                    "source_name": mapping.source_name,
                    "source_type": source_type or "UNKNOWN",
                    "target_type": mapping.target_type,
                }
            )
    return mismatches


def _matching_upsert_constraints(
    draft: DeliveryDraft, metadata: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        constraint
        for constraint in metadata.get("constraints", [])
        if constraint.get("type") in {"PRIMARY_KEY", "UNIQUE"}
        and len(constraint.get("columns", [])) == len(draft.upsert_keys)
        and set(constraint["columns"]) == set(draft.upsert_keys)
    ]


def _target_compatibility(
    draft: DeliveryDraft,
    metadata: dict[str, Any],
    records: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    sink_type: str,
) -> None:
    if sink_type == "SQLSERVER":
        ignore_duplicate_keys = any(
            bool(constraint.get("ignore_duplicate_keys"))
            for constraint in metadata.get("constraints", [])
        )
        _check(
            checks,
            "SQLSERVER_IGNORE_DUP_KEY",
            not ignore_duplicate_keys,
            (
                "Los índices UNIQUE propagan duplicados como errores."
                if not ignore_duplicate_keys
                else "El target usa IGNORE_DUP_KEY y podría omitir filas silenciosamente."
            ),
        )
    if draft.write_strategy == "OVERWRITE":
        row_security_active = bool(
            metadata.get("row_security_active")
            or metadata.get("filter_security_policy")
        )
        _check(
            checks,
            "OVERWRITE_ROW_SECURITY",
            not row_security_active,
            (
                "DELETE ve el conjunto completo del target."
                if not row_security_active
                else "Una política de seguridad por filas podría ocultar filas a DELETE."
            ),
        )
    target_columns = {column["name"]: column for column in metadata["columns"]}
    mapped_names = {column.target_name for column in draft.columns}
    for mapping in draft.columns:
        target_column = target_columns.get(mapping.target_name)
        if target_column is None:
            _check(
                checks,
                "TARGET_COLUMN_EXISTS",
                False,
                f"No existe la columna destino {mapping.target_name}.",
            )
            continue
        writable = not target_column.get("identity") and not target_column.get("generated")
        _check(
            checks,
            "TARGET_COLUMN_WRITABLE",
            writable,
            f"{mapping.target_name}: la columna admite escritura explícita.",
        )
        decimal_capacity = (
            decimal_capacity_for_native(
                sink_type,
                str(target_column.get("native_type", "")),
                target_column.get("precision"),
                target_column.get("scale"),
            )
            if mapping.target_type == "DECIMAL"
            else None
        )
        timestamp_policy = (
            timestamp_policy_for_native(
                sink_type,
                str(target_column.get("native_type", "")),
                target_column.get("datetime_precision"),
            )
            if mapping.target_type == "TIMESTAMP"
            else None
        )
        string_storage = (
            string_storage_is_exact(
                sink_type, str(target_column.get("native_type", ""))
            )
            if mapping.target_type == "STRING"
            else True
        )
        exact_native_storage = (
            mapping.target_type != "DECIMAL" or decimal_capacity is not None
        ) and (
            mapping.target_type != "TIMESTAMP" or timestamp_policy is not None
        ) and string_storage
        compatible = (
            target_column["logical_type"] == mapping.target_type
            and exact_native_storage
        )
        _check(
            checks,
            "TYPE_COMPATIBLE",
            compatible,
            (
                f"{mapping.target_name}: {mapping.target_type} es compatible."
                if compatible
                else (
                    f"{mapping.target_name}: el target no ofrece almacenamiento decimal exacto."
                    if mapping.target_type == "DECIMAL"
                    and target_column["logical_type"] == "DECIMAL"
                    else (
                        f"{mapping.target_name}: el target no ofrece almacenamiento temporal exacto."
                        if mapping.target_type == "TIMESTAMP"
                        and target_column["logical_type"] == "TIMESTAMP"
                        else (
                            f"{mapping.target_name}: el target no ofrece almacenamiento Unicode variable exacto."
                            if mapping.target_type == "STRING"
                            else f"{mapping.target_name}: el tipo configurado no coincide con el target."
                        )
                    )
                )
            ),
        )
        target_length = target_column.get("length")
        if mapping.target_type == "STRING":
            text_values = [
                str(record[mapping.source_name])
                for record in records
                if record.get(mapping.source_name) is not None
            ]
            observed_length = max(
                (utf16_code_units(value) for value in text_values), default=0
            )
            if target_length not in {None, -1}:
                configured_length = mapping.length or observed_length
                _check(
                    checks,
                    "LENGTH_COMPATIBLE",
                    configured_length <= int(target_length)
                    and observed_length <= int(target_length),
                    f"{mapping.target_name}: longitud UTF-16 validada.",
                )
            supplementary = any(
                ord(character) > 0xFFFF
                for value in text_values
                for character in value
            )
            collation_ok = (
                sink_type != "SQLSERVER"
                or not supplementary
                or sqlserver_collation_supports_supplementary(
                    target_column.get("collation")
                )
            )
            _check(
                checks,
                "STRING_STORAGE_COMPATIBLE",
                bool(string_storage and collation_ok),
                (
                    f"{mapping.target_name}: familia Unicode y collation validadas."
                    if string_storage and collation_ok
                    else f"{mapping.target_name}: el tipo o collation alteraría el texto."
                ),
            )
        if mapping.target_type == "DECIMAL":
            precision = mapping.precision or 38
            scale = mapping.scale if mapping.scale is not None else 10
            decimal_ok = (
                decimal_capacity is not None
                and precision <= decimal_capacity[0]
                and scale <= decimal_capacity[1]
                and precision - scale <= decimal_capacity[0] - decimal_capacity[1]
            )
            _check(
                checks,
                "DECIMAL_COMPATIBLE",
                decimal_ok,
                (
                    f"{mapping.target_name}: precisión, escala y dígitos enteros validados."
                    if decimal_ok
                    else f"{mapping.target_name}: el target no tiene capacidad decimal exacta suficiente."
                ),
            )
        if mapping.target_type == "TIMESTAMP":
            timestamp_ok = timestamp_policy is not None
            if timestamp_policy is not None:
                fractional_digits, offset_required = timestamp_policy
                quantum = 10 ** (6 - min(fractional_digits, 6))
                for record in records:
                    raw_value = record.get(mapping.source_name)
                    if raw_value is None:
                        continue
                    try:
                        converted = convert_value(raw_value, mapping.model_dump())
                    except DeliveryError:
                        timestamp_ok = False
                        break
                    offset_present = (
                        converted.tzinfo is not None
                        and converted.utcoffset() is not None
                    )
                    if (
                        offset_present != offset_required
                        or converted.microsecond % quantum != 0
                    ):
                        timestamp_ok = False
                        break
            _check(
                checks,
                "TIMESTAMP_COMPATIBLE",
                timestamp_ok,
                (
                    f"{mapping.target_name}: precisión temporal e instante validados."
                    if timestamp_ok
                    else f"{mapping.target_name}: el target alteraría precisión o instante temporal."
                ),
            )
        if mapping.target_type == "INT64":
            bounds = integer_bounds_for_native(
                sink_type, str(target_column.get("native_type", ""))
            )
            if bounds is not None:
                lower, upper = bounds
                in_range = all(
                    lower
                    <= convert_value(
                        record.get(mapping.source_name), mapping.model_dump()
                    )
                    <= upper
                    for record in records
                    if record.get(mapping.source_name) is not None
                )
                _check(
                    checks,
                    "INTEGER_RANGE_COMPATIBLE",
                    in_range,
                    f"{mapping.target_name}: rango entero del target validado.",
                )
        contains_null = any(record.get(mapping.source_name) is None for record in records)
        nullability_ok = not contains_null or (
            mapping.nullable and bool(target_column["nullable"])
        )
        if mapping.nullable and not target_column["nullable"]:
            nullability_ok = False
        _check(
            checks,
            "NULLABILITY_COMPATIBLE",
            nullability_ok,
            f"{mapping.target_name}: nulabilidad validada.",
        )
    required_missing = [
        column["name"]
        for column in metadata["columns"]
        if column["name"] not in mapped_names
        and not column["nullable"]
        and not column["has_default"]
        and not column["identity"]
        and not column["generated"]
    ]
    _check(
        checks,
        "REQUIRED_TARGET_COLUMNS",
        not required_missing,
        (
            "Todas las columnas obligatorias del target están cubiertas."
            if not required_missing
            else "Faltan columnas obligatorias del target: " + ", ".join(required_missing)
        ),
    )
    if draft.write_strategy == "UPSERT":
        matching_constraints = _matching_upsert_constraints(draft, metadata)
        unique_ok = bool(matching_constraints) and (
            sink_type != "POSTGRESQL"
            or all(
                isinstance(constraint.get("name"), str)
                and bool(constraint["name"])
                for constraint in matching_constraints
            )
        )
        _check(
            checks,
            "UPSERT_UNIQUE_CONSTRAINT",
            unique_ok,
            "La clave UPSERT está respaldada por una PK o restricción unique.",
        )


def preflight_delivery(
    db: Session,
    organization_id: str,
    draft: DeliveryDraft,
    *,
    raise_on_failure: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    version, dataset, artifact, frame = _owned_version(
        db, draft.dataset_version_id, organization_id
    )
    _check(checks, "DATASET_VERSION", True, "DatasetVersion inmutable disponible.")
    _check(checks, "CANONICAL_ARTIFACT", True, "Artifact canónico íntegro y legible.")
    destination = db.scalar(
        select(DeliveryDestination).where(
            DeliveryDestination.id == draft.destination_id,
            DeliveryDestination.organization_id == organization_id,
            DeliveryDestination.deleted.is_(False),
        )
    )
    if destination is None or not destination.enabled:
        raise DeliveryOperationError(
            412, "FAILED_PRECONDITION", "El destino no existe o está deshabilitado."
        )
    destination_version = exact_destination_version(
        db, destination, draft.destination_version_id
    )
    _check(checks, "DESTINATION", True, "Destino y revisión inmutable disponibles.")
    sink = sink_registry.create(settings_for(destination, destination_version))
    sink.test()
    _check(checks, "CONNECTION", True, "Conexión al destino verificada.")
    target = draft.target.model_dump()
    source_schema = {column["name"]: column for column in version.schema_json}
    missing_source = [
        column.source_name for column in draft.columns if column.source_name not in frame.columns
    ]
    _check(
        checks,
        "SOURCE_COLUMNS",
        not missing_source,
        (
            "Las columnas seleccionadas existen en la versión."
            if not missing_source
            else "Faltan columnas en la versión: " + ", ".join(missing_source)
        ),
    )
    source_type_mismatches = _source_type_mismatches(version, draft)
    _check(
        checks,
        "SOURCE_TYPE_PRESERVATION",
        not source_type_mismatches,
        (
            "El mapping conserva los tipos lógicos de la DatasetVersion."
            if not source_type_mismatches
            else "Data Delivery no admite conversiones entre tipos lógicos."
        ),
    )
    mapping_ok = not source_type_mismatches
    for column in draft.columns:
        schema_column = source_schema.get(column.source_name, {})
        if schema_column.get("semantic_tag") == "IDENTIFIER" and column.target_type != "STRING":
            mapping_ok = False
            _check(
                checks,
                "IDENTIFIER_PRESERVATION",
                False,
                f"{column.source_name} debe conservarse como texto.",
            )
        try:
            technical_type(destination.sink_type, column.model_dump())
        except DeliveryError as error:
            mapping_ok = False
            _check(checks, error.code, False, error.message)
    _check(checks, "COLUMN_MAPPING", mapping_ok, "Mapping técnico sin ambigüedades.")
    records = (
        frame.select([column.source_name for column in draft.columns]).to_dicts()
        if not missing_source
        else []
    )
    values_ok = not missing_source
    if values_ok:
        try:
            for record in records:
                for column in draft.columns:
                    convert_value(
                        record.get(column.source_name), column.model_dump()
                    )
        except DeliveryError as error:
            values_ok = False
            _check(checks, error.code, False, error.message)
    _check(
        checks,
        "VALUES_COMPATIBLE",
        values_ok,
        "Los valores son compatibles con el mapping técnico.",
    )
    schemas = sink.schemas()
    if target["mode"] == "CREATE_TABLE":
        schema_exists = target["schema_name"] in schemas
        expected_schema_state = not schema_exists if target["create_schema"] else schema_exists
        _check(
            checks,
            "SCHEMA_STATE",
            expected_schema_state,
            "El estado del schema coincide con la selección.",
        )
        table_exists = (
            target["table_name"] in sink.tables(target["schema_name"])
            if schema_exists
            else False
        )
        _check(
            checks,
            "TARGET_ABSENT",
            not table_exists,
            "La tabla nueva todavía no existe.",
        )
        permissions = sink.permissions(target, draft.write_strategy)
        allowed = bool(
            permissions.get("create_table")
            and (not target["create_schema"] or permissions.get("create_schema"))
        )
        _check(checks, "PERMISSIONS", allowed, "Permisos de creación verificados.")
        metadata = None
    else:
        table_exists = (
            target["schema_name"] in schemas
            and target["table_name"] in sink.tables(target["schema_name"])
        )
        _check(checks, "TARGET_EXISTS", table_exists, "La tabla destino existe.")
        metadata = (
            sink.table_metadata(target["schema_name"], target["table_name"])
            if table_exists
            else None
        )
        if metadata:
            _target_compatibility(
                draft, metadata, records, checks, destination.sink_type
            )
        permissions = sink.permissions(target, draft.write_strategy)
        _check(
            checks,
            "PERMISSIONS",
            bool(permissions.get("allowed")),
            "Permisos de escritura verificados.",
        )
    prepared_target = dict(target)
    if (
        destination.sink_type == "POSTGRESQL"
        and draft.write_strategy == "UPSERT"
        and metadata
    ):
        matching = _matching_upsert_constraints(draft, metadata)
        named = [
            str(constraint["name"])
            for constraint in matching
            if isinstance(constraint.get("name"), str) and constraint["name"]
        ]
        if named:
            prepared_target["_upsert_constraint"] = min(named)
    payload_ok = values_ok
    if payload_ok:
        try:
            sink.prepare(
                records,
                [column.model_dump() for column in draft.columns],
                prepared_target,
                draft.write_strategy,
                draft.upsert_keys,
            )
        except DeliveryError as error:
            payload_ok = False
            _check(checks, error.code, False, error.message)
    _check(
        checks,
        "LOCAL_PAYLOAD",
        payload_ok,
        "Identifiers y payload tipado preparados sin iniciar escritura remota.",
    )
    if draft.write_strategy == "UPSERT":
        mapping_by_target = {column.target_name: column for column in draft.columns}
        key_values: list[tuple[Any, ...]] = []
        keys_valid = True
        for record in records:
            values = tuple(
                convert_value(
                    record.get(mapping_by_target[key].source_name),
                    mapping_by_target[key].model_dump(),
                )
                for key in draft.upsert_keys
            )
            if any(value is None for value in values):
                keys_valid = False
            key_values.append(values)
        if len(set(key_values)) != len(key_values):
            keys_valid = False
        _check(
            checks,
            "UPSERT_SOURCE_KEYS",
            keys_valid,
            "Las claves UPSERT de la fuente no tienen nulls ni duplicados.",
        )
    resource_ok = (
        frame.height == version.row_count
        and frame.width == version.column_count
        and frame.height <= MAX_ROWS
        and frame.width <= 100
    )
    _check(
        checks,
        "LOCAL_RESOURCES",
        resource_ok,
        "Volumen y estructura dentro de los límites certificados del prototipo.",
    )
    failures = [check for check in checks if check["status"] == "FAIL"]
    result = {
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
        "source": {
            "dataset_id": dataset.id,
            "dataset_name": dataset.name,
            "dataset_version_id": version.id,
            "version": version.version,
            "row_count": version.row_count,
            "canonical_artifact_id": artifact.id,
            "canonical_sha256": artifact.sha256,
        },
        "destination": {
            "destination_id": destination.id,
            "destination_version_id": destination_version.id,
            "sink_type": destination.sink_type,
            "config_hash": destination_version.config_hash,
        },
        "target": {**target, "metadata": metadata},
    }
    if failures and raise_on_failure:
        raise DeliveryOperationError(
            412,
            "FAILED_PRECONDITION",
            failures[0]["message"],
            {"checks": checks},
        )
    return result


def create_delivery_configuration(
    db: Session,
    user: User,
    draft: DeliveryDraft,
    *,
    name: str,
    owner: str,
    description: str,
    previous: Configuration | None = None,
) -> Configuration:
    preflight_delivery(db, user.organization_id, draft)
    version, dataset, _artifact, _frame = _owned_version(
        db, draft.dataset_version_id, user.organization_id
    )
    if previous:
        if previous.organization_id != user.organization_id or previous.module != "DELIVERY":
            raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró la configuración.")
        if db.scalar(select(Configuration).where(Configuration.previous_version_id == previous.id)):
            raise DeliveryOperationError(
                409, "VERSION_CONFLICT", "Ya existe una versión posterior de la configuración."
            )
        config_version = previous.version + 1
        previous_id = previous.id
        name = previous.name
        owner = previous.owner
    else:
        config_version = 1
        previous_id = None
    config = Configuration(
        organization_id=user.organization_id,
        name=name.strip(),
        module="DELIVERY",
        version=config_version,
        dataset_id=dataset.id,
        owner=owner.strip(),
        description=description,
        config=draft.snapshot(),
        previous_version_id=previous_id,
    )
    db.add(config)
    db.flush()
    audit(
        db,
        "DELIVERY_CONFIGURATION_PUBLISHED",
        "configuration",
        config.id,
        "Configuración de entrega publicada",
        user.name,
        user.organization_id,
        {
            "module": "DELIVERY",
            "version": config.version,
            "dataset_version_id": version.id,
            "destination_id": draft.destination_id,
            "destination_version_id": draft.destination_version_id,
            "write_strategy": draft.write_strategy,
        },
    )
    db.commit()
    return config


def delivery_config_dto(db: Session, config: Configuration) -> dict[str, Any]:
    dataset = require_record(db, Dataset, config.dataset_id)
    draft = DeliveryDraft.model_validate(config.config)
    destination = require_record(db, DeliveryDestination, draft.destination_id)
    destination_version = require_record(
        db, DeliveryDestinationVersion, draft.destination_version_id
    )
    latest = db.scalar(
        select(Run).where(Run.config_id == config.id).order_by(Run.created_at.desc())
    )
    from .services import run_dto

    return {
        "id": config.id,
        "name": config.name,
        "module": config.module,
        "version": config.version,
        "dataset_id": config.dataset_id,
        "dataset_name": dataset.name,
        "dataset_version_id": draft.dataset_version_id,
        "destination_id": destination.id,
        "destination_name": destination.name,
        "destination_version_id": destination_version.id,
        "destination_version": destination_version.version,
        "owner": config.owner,
        "description": config.description,
        "status": config.status,
        "config": config.config,
        "previous_version_id": config.previous_version_id,
        "created_at": iso(config.created_at),
        "latest_run": run_dto(db, latest) if latest else None,
    }


def enqueue_delivery(
    db: Session,
    config: Configuration,
    source: DatasetVersion,
    actor: User,
    *,
    queue: JobQueue | None = None,
) -> Run:
    if config.module != "DELIVERY" or config.organization_id != actor.organization_id:
        raise DeliveryOperationError(422, "WRONG_MODULE", "La configuración no es de Delivery.")
    draft = DeliveryDraft.model_validate(config.config)
    if source.id != draft.dataset_version_id or source.dataset_id != config.dataset_id:
        raise DeliveryOperationError(
            422,
            "DATASET_VERSION_MISMATCH",
            "La ejecución debe usar exactamente la DatasetVersion publicada.",
        )
    destination = db.get(DeliveryDestination, draft.destination_id)
    destination_version = db.get(
        DeliveryDestinationVersion, draft.destination_version_id
    )
    if (
        destination is None
        or destination.organization_id != config.organization_id
        or destination_version is None
        or destination_version.organization_id != config.organization_id
        or destination_version.destination_id != destination.id
    ):
        raise DeliveryOperationError(
            412,
            "FAILED_PRECONDITION",
            "La identidad versionada del destino ya no está disponible.",
        )
    canonical = db.get(Artifact, source.canonical_artifact_id)
    if canonical is None or canonical.organization_id != config.organization_id:
        raise DeliveryOperationError(412, "FAILED_PRECONDITION", "El artifact canónico no está disponible.")
    run = Run(
        id=uid(),
        organization_id=config.organization_id,
        module="DELIVERY",
        name=config.name,
        config_id=config.id,
        dataset_version_id=source.id,
        initiated_by=actor.name,
        initiated_by_type="USER",
        initiated_by_id=actor.id,
        execution_plan={
            "engine": "DATA_SINK",
            "lane": "DELIVERY",
            "schema_version": draft.schema_version,
            "destination_id": draft.destination_id,
            "destination_name": destination.name,
            "destination_version_id": draft.destination_version_id,
            "destination_version": destination_version.version,
            "sink_type": destination.sink_type,
            "target": draft.target.model_dump(),
            "write_strategy": draft.write_strategy,
            "config_hash": configuration_hash(draft.snapshot()),
            "evidence_engine_version": __version__,
            "source_identity": {
                "dataset_version_id": source.id, "source_sha256": source.sha256,
                "schema_hash": source.schema_hash, "canonical_artifact_id": canonical.id,
                "canonical_sha256": canonical.sha256,
            },
            "allowed": True,
            "reason_code": "DELIVERY_PREFLIGHT_REQUIRED_AT_EXECUTION",
        },
    )
    db.add(run)
    db.flush()
    (queue or job_queue).submit(db, run, lane="DELIVERY")
    link_artifact(
        db,
        run.organization_id,
        "DELIVERY_INPUT",
        "DATASET_VERSION",
        source.id,
        "RUN",
        run.id,
    )
    link_artifact(
        db,
        run.organization_id,
        "DELIVERED_TO",
        "RUN",
        run.id,
        "DELIVERY_DESTINATION_VERSION",
        draft.destination_version_id,
    )
    audit(
        db,
        "DELIVERY_RUN_QUEUED",
        "run",
        run.id,
        "Entrega encolada en lane DELIVERY",
        actor.name,
        run.organization_id,
        {
            "dataset_version_id": source.id,
            "destination_id": draft.destination_id,
            "destination_version_id": draft.destination_version_id,
            "write_strategy": draft.write_strategy,
        },
        run_id=run.id,
    )
    return run


def attempt_dto(attempt: DeliveryAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "run_id": attempt.run_id,
        "destination_version_id": attempt.destination_version_id,
        "attempt_number": attempt.attempt_number,
        "idempotency_key": attempt.idempotency_key,
        "status": attempt.status,
        "target_locator": attempt.target_locator,
        "rows_attempted": attempt.rows_attempted,
        "rows_written": attempt.rows_written,
        "rows_inserted": attempt.rows_inserted,
        "rows_updated": attempt.rows_updated,
        "bytes_sent": attempt.bytes_sent,
        "remote_reference": attempt.remote_reference,
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "started_at": iso(attempt.started_at),
        "finished_at": iso(attempt.finished_at),
        "created_at": iso(attempt.created_at),
    }


def _delivery_idempotency(run: Run, config: Configuration) -> str:
    value = f"{run.organization_id}:{config.id}:{run.dataset_version_id}:{run.id}".encode()
    return hashlib.sha256(value).hexdigest()


def _record_remote_failure(
    db: Session,
    run: Run,
    attempt: DeliveryAttempt,
    error: DeliveryError,
    actor: Actor,
) -> None:
    status = "UNKNOWN" if error.ambiguous else "FAILED"
    attempt.status = status
    attempt.error_code = error.code
    attempt.error_message = error.message
    attempt.finished_at = utcnow()
    run.status = status
    run.decision = status
    run.error = error.message
    run.progress_stage = (
        "Confirmación remota desconocida" if status == "UNKNOWN" else "Entrega fallida"
    )
    run.finished_at = utcnow()
    audit(
        db,
        f"DELIVERY_{status}",
        "delivery_attempt",
        attempt.id,
        error.message,
        actor,
        run.organization_id,
        {
            "run_id": run.id,
            "attempt_number": attempt.attempt_number,
            "status": status,
            "error_code": error.code,
            "write_strategy": run.execution_plan.get("write_strategy"),
        },
        run_id=run.id,
    )
    db.commit()


def _fence_delivery_job(db: Session, run: Run, lease_owner: str | None) -> None:
    if lease_owner is None:
        return
    now = utcnow()
    fenced = db.execute(
        update(Job)
        .where(
            Job.run_id == run.id,
            Job.lane == "DELIVERY",
            Job.status == "RUNNING",
            Job.lease_owner == lease_owner,
            Job.lease_until.is_not(None),
            Job.lease_until > now,
        )
        .values(lease_until=now + timedelta(seconds=DELIVERY_LEASE_FENCE_SECONDS))
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult, fenced).rowcount != 1:
        raise DeliveryOperationError(
            409, "WORKER_LEASE_LOST", "El delivery-worker ya no posee esta ejecución."
        )


def _fence_delivery_start(db: Session, run: Run, lease_owner: str | None) -> None:
    # Lock Run first, matching cancellation's write order. Whichever transaction
    # wins this row determines whether STARTED may be persisted.
    locked_run = db.scalar(
        select(Run)
        .where(Run.id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        locked_run is None
        or locked_run.cancel_requested
        or locked_run.status == "CANCELLED"
    ):
        raise DeliveryOperationError(
            409, "WORKER_LEASE_LOST", "El delivery-worker ya no posee esta ejecución."
        )
    _fence_delivery_job(db, run, lease_owner)


def _fence_delivery_result(
    db: Session,
    run: Run,
    attempt: DeliveryAttempt,
    lease_owner: str | None,
) -> None:
    # Once remote I/O started, a later cancellation cannot erase a known
    # COMMITTED/FAILED result. Ownership and the durable STARTED marker remain
    # mandatory, but cancellation is deliberately not part of this fence.
    _fence_delivery_job(db, run, lease_owner)
    db.refresh(attempt)
    if attempt.status != "STARTED":
        raise DeliveryOperationError(
            409, "WORKER_LEASE_LOST", "El delivery-worker ya no posee esta ejecución."
        )


def _write_json_artifact(
    db: Session,
    run: Run,
    kind: str,
    name: str,
    payload: dict[str, Any],
    *,
    artifact_id: str | None = None,
    newline: str = "\n",
) -> Artifact:
    temporary = storage_provider.temporary_path(".json")
    try:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2).replace("\n", newline)
        temporary.write_bytes(serialized.encode("utf-8"))
        return storage_provider.put_file(
            db,
            temporary,
            kind,
            run.organization_id,
            name,
            artifact_id=artifact_id,
            media_type="application/json",
        )
    finally:
        Path(temporary).unlink(missing_ok=True)


def _evidence_metrics(run: Run) -> dict[str, Any]:
    """Evidence describes the remote operation, not its publication/recovery state."""
    return {
        key: value for key, value in (run.metrics or {}).items()
        if key not in {"evidence_status", "receipt_artifact_id", "manifest_artifact_id"}
    }


def _evidence_identity(run: Run, kind: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"trackvance:delivery:{run.organization_id}:{run.id}:{kind}"))


def _evidence_problem(reason: str) -> DeliveryOperationError:
    return DeliveryOperationError(409, "DELIVERY_EVIDENCE_UNVERIFIABLE", reason)


def _evidence_candidates(db: Session, run: Run, kind: str) -> list[Artifact]:
    identities = {_evidence_identity(run, kind)}
    links = db.scalars(select(ArtifactLink).where(
        ArtifactLink.organization_id == run.organization_id,
        ArtifactLink.source_type == "RUN", ArtifactLink.source_id == run.id,
        ArtifactLink.target_type == "ARTIFACT",
        ArtifactLink.relation.in_({"RUN_OUTPUT", "DELIVERY_RECEIPT"}),
    )).all()
    identities.update(link.target_id for link in links)
    if run.metrics.get("receipt_artifact_id"):
        identities.add(run.metrics["receipt_artifact_id"])
    query = select(Artifact).where(
        Artifact.organization_id == run.organization_id, Artifact.kind == kind,
        (Artifact.id.in_(identities)) | (Artifact.path == run.evidence_path),
    )
    return list(db.scalars(query).all())


def _equivalent_evidence(actual: dict[str, Any], expected: dict[str, Any], kind: str) -> bool:
    # Cosmetic display names and additive descriptions do not invalidate 0.5.0
    # evidence. All operation identities, hashes, configuration and measured
    # values must still match; old bytes are never rewritten just to add fields.
    if kind == "DELIVERY_RECEIPT":
        ignored = {"destination_name", "metric_semantics"}
        return all(actual.get(key) == value for key, value in expected.items() if key not in ignored)
    if actual.get("schema_version") != SCHEMA_VERSION or actual.get("module") != "DELIVERY":
        return False
    for key in ("run_id", "started_at", "finished_at", "initiated_by", "inputs", "configuration"):
        if actual.get(key) != expected.get(key):
            return False
    actual_plan, expected_plan = actual.get("processing"), expected.get("processing")
    if actual_plan != expected_plan:
        return False
    actual_delivery, expected_delivery = actual.get("delivery", {}), expected["delivery"]
    if not isinstance(actual_delivery, dict):
        return False
    for key in ("target", "write_strategy", "attempt"):
        if actual_delivery.get(key) != expected_delivery[key]:
            return False
    for key, value in expected_delivery["destination"].items():
        if key != "destination_name" and actual_delivery.get("destination", {}).get(key) != value:
            return False
    for metrics in (actual.get("metrics"), actual_delivery.get("metrics")):
        if not isinstance(metrics, dict):
            return False
        if any(metrics.get(key) != value for key, value in expected["metrics"].items()
               if key != "metric_semantics"):
            return False
    return actual.get("result_artifacts") == expected["result_artifacts"]


def _publish_evidence_artifact(
    db: Session, run: Run, kind: str, name: str, payload: dict[str, Any],
) -> Artifact:
    candidates = _evidence_candidates(db, run, kind)
    if len(candidates) > 1:
        raise _evidence_problem("Hay más de un artifact de evidencia asociado a esta entrega.")
    if candidates:
        candidate = candidates[0]
        if storage_provider.exists(candidate):
            try:
                actual = json.loads(storage_provider.materialize(candidate).read_text(encoding="utf-8"))
            except (ArtifactIntegrityError, OSError, ValueError):
                raise _evidence_problem("La evidencia existente está corrupta; no se sobrescribe.") from None
            if not isinstance(actual, dict) or not _equivalent_evidence(actual, payload, kind):
                raise _evidence_problem("La evidencia existente no coincide con la operación persistida.")
            return candidate
        # Recreate missing bytes only when the exact registered hash is derivable.
        variants = [payload]
        legacy = json.loads(json.dumps(payload))
        legacy.pop("metric_semantics", None)
        if kind == "RUN_MANIFEST":
            legacy["engine_version"] = "0.5.0"
            legacy["processing"].pop("evidence_engine_version", None)
            legacy["metrics"].pop("metric_semantics", None)
            legacy["delivery"]["metrics"].pop("metric_semantics", None)
        variants.append(legacy)
        for variant in variants:
            # 0.5.0's text writer used platform-native newlines. Both are
            # reconstructible, while all new evidence uses portable LF bytes.
            for newline in ("\n", "\r\n"):
                encoded = json.dumps(variant, ensure_ascii=False, indent=2).replace("\n", newline).encode("utf-8")
                if hashlib.sha256(encoded).hexdigest() == candidate.sha256:
                    return _write_json_artifact(
                        db, run, kind, name, variant, artifact_id=candidate.id, newline=newline,
                    )
        raise _evidence_problem("No se pueden reconstruir los bytes del hash histórico registrado.")
    return _write_json_artifact(
        db, run, kind, name, payload, artifact_id=_evidence_identity(run, kind),
    )


def _publish_delivery_evidence(
    db: Session,
    run: Run,
    config: Configuration,
    source: DatasetVersion,
    source_artifact: Artifact,
    destination: DeliveryDestination,
    destination_version: DeliveryDestinationVersion,
    draft: DeliveryDraft,
    attempt: DeliveryAttempt,
) -> tuple[Artifact, Artifact]:
    target = draft.target.model_dump()
    metrics = _evidence_metrics(run)
    destination_name = run.execution_plan.get("destination_name", destination.name)
    receipt_payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "DELIVERY_RECEIPT",
        "run_id": run.id,
        "dataset_version_id": source.id,
        "source_sha256": source.sha256,
        "canonical_artifact_id": source_artifact.id,
        "canonical_sha256": source_artifact.sha256,
        "destination_id": destination.id,
        "destination_name": destination_name,
        "destination_version_id": destination_version.id,
        "destination_version": destination_version.version,
        "sink_type": destination.sink_type,
        "config_hash": destination_version.config_hash,
        "target": target,
        "write_strategy": draft.write_strategy,
        "rows_attempted": attempt.rows_attempted,
        "rows_written": attempt.rows_written,
        "rows_inserted": attempt.rows_inserted,
        "rows_updated": attempt.rows_updated,
        "bytes_sent": attempt.bytes_sent,
        "started_at": iso(attempt.started_at),
        "finished_at": iso(attempt.finished_at),
        "result": attempt.status,
        "delivery_attempt_id": attempt.id,
        "attempt_number": attempt.attempt_number,
        "metric_semantics": delivery_metric_semantics(),
        **{key: metrics[key] for key in ("preflight_seconds", "write_seconds") if key in metrics},
    }
    receipt = _publish_evidence_artifact(
        db, run, "DELIVERY_RECEIPT", "delivery-receipt.json", receipt_payload
    )
    link_artifact(
        db,
        run.organization_id,
        "DELIVERY_RECEIPT",
        "RUN",
        run.id,
        "ARTIFACT",
        receipt.id,
    )
    link_artifact(
        db,
        run.organization_id,
        "EVIDENCE_OF",
        "ARTIFACT",
        receipt.id,
        "DELIVERY_ATTEMPT",
        attempt.id,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.id,
        "module": "DELIVERY",
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "initiated_by": run_actor(run).as_dict(),
        "initiated_by_legacy": run.initiated_by_legacy,
        "engine_version": run.execution_plan.get("evidence_engine_version", "0.5.0"),
        "processing": run.execution_plan,
        "inputs": [
            {
                "dataset_id": source.dataset_id,
                "dataset_version_id": source.id,
                "version": source.version,
                "source_type": source.source_type,
                "artifact_sha256": source.sha256,
                "schema_hash": source.schema_hash,
                "canonical_artifact_id": source_artifact.id,
                "canonical_sha256": source_artifact.sha256,
            }
        ],
        "configuration": {
            "id": config.id,
            "name": config.name,
            "version": config.version,
            "config": draft.snapshot(),
            "config_hash": configuration_hash(draft.snapshot()),
            "stored_config_hash": configuration_hash(config.config),
        },
        "delivery": {
            "destination": {
                "destination_id": destination.id,
                "destination_name": destination_name,
                "destination_version_id": destination_version.id,
                "destination_version": destination_version.version,
                "sink_type": destination.sink_type,
                "config_hash": destination_version.config_hash,
            },
            "target": target,
            "write_strategy": draft.write_strategy,
            "metrics": metrics,
            "attempt": attempt_dto(attempt),
        },
        "metrics": metrics,
        "metric_semantics": delivery_metric_semantics(),
        "result_artifacts": [artifact_dto(receipt)],
    }
    evidence = _publish_evidence_artifact(db, run, "RUN_MANIFEST", "evidence.json", manifest)
    link_artifact(
        db,
        run.organization_id,
        "RUN_OUTPUT",
        "RUN",
        run.id,
        "ARTIFACT",
        receipt.id,
    )
    link_artifact(
        db,
        run.organization_id,
        "RUN_OUTPUT",
        "RUN",
        run.id,
        "ARTIFACT",
        evidence.id,
    )
    return receipt, evidence


def execute_delivery_run(
    db: Session,
    run: Run,
    *,
    lease_owner: str | None = None,
) -> None:
    if run.module != "DELIVERY":
        raise DeliveryOperationError(422, "WRONG_MODULE", "La Run no pertenece a Delivery.")
    if run.status in {
        "SUCCESS",
        "FAILED",
        "UNKNOWN",
        "CANCELLED",
        "FAILED_PRECONDITION",
    }:
        return
    config = require_record(db, Configuration, run.config_id)
    source = require_record(db, DatasetVersion, run.dataset_version_id)
    draft = DeliveryDraft.model_validate(config.config)
    actor = Actor("WORKER", "trackvance:delivery-worker", "Delivery Worker")
    prior = db.scalar(
        select(DeliveryAttempt)
        .where(DeliveryAttempt.run_id == run.id)
        .order_by(DeliveryAttempt.attempt_number.desc())
    )
    if prior and prior.status == "COMMITTED":
        run.status = "SUCCESS"
        run.decision = "COMMITTED"
        db.commit()
        return
    if prior and prior.status in {"STARTED", "UNKNOWN"}:
        if prior.status == "STARTED":
            prior.status = "UNKNOWN"
            prior.error_code = "WORKER_CONFIRMATION_LOST"
            prior.error_message = (
                "Trackvance perdió la confirmación de un intento remoto iniciado."
            )
            prior.finished_at = utcnow()
        run.status = "UNKNOWN"
        run.decision = "UNKNOWN"
        run.error = prior.error_message
        run.finished_at = utcnow()
        run.progress_stage = "Confirmación remota desconocida"
        audit(
            db,
            "DELIVERY_UNKNOWN",
            "delivery_attempt",
            prior.id,
            prior.error_message or "Confirmación remota desconocida",
            actor,
            run.organization_id,
            {
                "run_id": run.id,
                "attempt_number": prior.attempt_number,
                "status": "UNKNOWN",
                "error_code": prior.error_code,
            },
            run_id=run.id,
        )
        db.commit()
        return
    preflight_started = time.perf_counter()
    try:
        preflight = preflight_delivery(db, run.organization_id, draft)
    except (DeliveryOperationError, DeliveryError) as error:
        code = error.code
        message = error.message
        run.status = "FAILED_PRECONDITION"
        run.decision = "FAILED"
        run.error = message
        run.finished_at = utcnow()
        run.progress_stage = "Preflight fallido"
        audit(
            db,
            "DELIVERY_FAILED",
            "run",
            run.id,
            message,
            actor,
            run.organization_id,
            {"status": "FAILED_PRECONDITION", "error_code": code},
            run_id=run.id,
        )
        db.commit()
        return
    preflight_seconds = time.perf_counter() - preflight_started
    destination = require_record(db, DeliveryDestination, draft.destination_id)
    destination_version = exact_destination_version(
        db, destination, draft.destination_version_id
    )
    try:
        _version, _dataset, source_artifact, frame = _owned_version(
            db, source.id, run.organization_id
        )
        selected_records = frame.select(
            [column.source_name for column in draft.columns]
        ).to_dicts()
        sink = sink_registry.create(settings_for(destination, destination_version))
        prepared_target = draft.target.model_dump()
        if destination.sink_type == "POSTGRESQL" and draft.write_strategy == "UPSERT":
            metadata = preflight["target"].get("metadata") or {}
            matching = _matching_upsert_constraints(draft, metadata)
            if not matching:
                raise DeliveryError(
                    "UPSERT_CONSTRAINT_MISSING",
                    "La restricción UPSERT exacta ya no está disponible.",
                )
            prepared_target["_upsert_constraint"] = min(
                str(constraint["name"]) for constraint in matching
            )
        prepared = sink.prepare(
            selected_records,
            [column.model_dump() for column in draft.columns],
            prepared_target,
            draft.write_strategy,
            draft.upsert_keys,
        )
    except (DeliveryOperationError, DeliveryError) as error:
        run.status = "FAILED_PRECONDITION"
        run.decision = "FAILED"
        run.error = error.message
        run.finished_at = utcnow()
        run.progress_stage = "Preparación local fallida"
        audit(
            db,
            "DELIVERY_FAILED",
            "run",
            run.id,
            error.message,
            actor,
            run.organization_id,
            {"status": "FAILED_PRECONDITION", "error_code": error.code},
            run_id=run.id,
        )
        db.commit()
        return
    # Fence immediately before STARTED. The conditional write holds the Job row
    # through the marker commit, so another worker cannot claim the same Run in
    # the local-preparation/check-to-commit window.
    _fence_delivery_start(db, run, lease_owner)
    attempt_number = (prior.attempt_number + 1) if prior else 1
    target_locator = f"{draft.target.schema_name}.{draft.target.table_name}"
    attempt = DeliveryAttempt(
        organization_id=run.organization_id,
        run_id=run.id,
        destination_version_id=destination_version.id,
        attempt_number=attempt_number,
        idempotency_key=_delivery_idempotency(run, config),
        status="STARTED",
        target_locator=target_locator,
        rows_attempted=source.row_count,
        started_at=utcnow(),
    )
    db.add(attempt)
    db.flush()
    run.status = "RUNNING"
    run.started_at = run.started_at or utcnow()
    run.progress_stage = "Publicando DatasetVersion"
    run.progress_percent = 20
    audit(
        db,
        "DELIVERY_STARTED",
        "delivery_attempt",
        attempt.id,
        "Intento remoto iniciado",
        actor,
        run.organization_id,
        {
            "run_id": run.id,
            "attempt_number": attempt_number,
            "destination_version_id": destination_version.id,
            "write_strategy": draft.write_strategy,
            "target": target_locator,
        },
        run_id=run.id,
    )
    # From this durable marker onward an abrupt worker loss cannot prove whether
    # the immediately following remote transaction started or committed.
    db.commit()
    write_started = time.perf_counter()
    try:
        result = sink.deliver_prepared(prepared)
    except DeliveryError as error:
        _fence_delivery_result(db, run, attempt, lease_owner)
        _record_remote_failure(db, run, attempt, error, actor)
        return
    write_seconds = time.perf_counter() - write_started
    # A successful remote return is not authority to overwrite an outcome that
    # another worker already reconciled. Fence the queue projection and refresh
    # the attempt before recording COMMITTED.
    _fence_delivery_result(db, run, attempt, lease_owner)
    attempt.status = "COMMITTED"
    attempt.rows_attempted = result.rows_attempted
    attempt.rows_written = result.rows_written
    attempt.rows_inserted = result.rows_inserted
    attempt.rows_updated = result.rows_updated
    attempt.bytes_sent = result.bytes_sent
    attempt.remote_reference = result.remote_reference
    attempt.finished_at = utcnow()
    run.metrics = {
        "rows_attempted": result.rows_attempted,
        "rows_written": result.rows_written,
        "rows_inserted": result.rows_inserted,
        "rows_updated": result.rows_updated,
        "bytes_sent": result.bytes_sent,
        "delivery_attempt_id": attempt.id,
        "attempt_number": attempt.attempt_number,
        "destination_id": destination.id,
        "destination_version_id": destination_version.id,
        "sink_type": destination.sink_type,
        "target": draft.target.model_dump(),
        "write_strategy": draft.write_strategy,
        "metric_semantics": delivery_metric_semantics(),
        "preflight_seconds": preflight_seconds,
        "write_seconds": write_seconds,
    }
    run.status = "SUCCESS"
    run.decision = "COMMITTED"
    run.progress_percent = 90
    run.progress_stage = "Transacción remota confirmada"
    run.finished_at = utcnow()
    run.error = None
    audit(
        db,
        "DELIVERY_COMMITTED",
        "delivery_attempt",
        attempt.id,
        "Entrega confirmada por el destino",
        actor,
        run.organization_id,
        {
            "run_id": run.id,
            "attempt_number": attempt.attempt_number,
            "status": "COMMITTED",
            "rows_written": result.rows_written,
            "rows_inserted": result.rows_inserted,
            "rows_updated": result.rows_updated,
            "write_strategy": draft.write_strategy,
            "target": target_locator,
        },
        run_id=run.id,
    )
    db.commit()  # Remote COMMITTED is durable before local evidence authoring.
    try:
        receipt, evidence = _publish_delivery_evidence(
            db,
            run,
            config,
            source,
            source_artifact,
            destination,
            destination_version,
            draft,
            attempt,
        )
        run.evidence_path = evidence.path
        run.metrics = {**run.metrics, "receipt_artifact_id": receipt.id}
        run.progress_percent = 100
        run.progress_stage = "Completado"
        db.commit()
    except Exception:  # noqa: BLE001 - remote COMMITTED must survive any local evidence failure.
        # The remote transaction must never be replayed because local evidence failed.
        db.rollback()
        refreshed = require_record(db, Run, run.id)
        refreshed.status = "SUCCESS"
        refreshed.decision = "COMMITTED"
        refreshed.progress_percent = 100
        refreshed.progress_stage = "Completado; evidencia local pendiente"
        refreshed.metrics = {**(refreshed.metrics or {}), "evidence_status": "PENDING_REPAIR"}
        db.commit()


def receipt_artifact(db: Session, run: Run) -> Artifact:
    link = db.scalar(
        select(ArtifactLink).where(
            ArtifactLink.organization_id == run.organization_id,
            ArtifactLink.relation == "DELIVERY_RECEIPT",
            ArtifactLink.source_type == "RUN",
            ArtifactLink.source_id == run.id,
            ArtifactLink.target_type == "ARTIFACT",
        )
    )
    artifact = db.get(Artifact, link.target_id) if link else None
    if artifact is None or artifact.organization_id != run.organization_id:
        raise DeliveryOperationError(
            409, "RECEIPT_NOT_READY", "El receipt todavía no está disponible."
        )
    storage_provider.materialize(artifact)
    return artifact


def owned_delivery_run(db: Session, user: User, run_id: str, *, lock: bool = False) -> Run:
    scope = (
        Run.id == run_id, Run.organization_id == user.organization_id, Run.module == "DELIVERY",
    )
    if lock:
        # A no-op row write obtains the same transaction-level serialization on
        # SQLite (whose SELECT FOR UPDATE is ignored) and PostgreSQL. Do this
        # before reading the repair projection; concurrent requests re-read the
        # winner's committed evidence instead of racing to publish another copy.
        db.execute(update(Run).where(*scope).values(id=Run.id).execution_options(synchronize_session=False))
    run = db.scalar(select(Run).where(*scope).execution_options(populate_existing=True))
    if run is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró la entrega.")
    return run


def _repair_context(db: Session, run: Run) -> tuple[
    Configuration, DatasetVersion, Artifact, DeliveryDestination,
    DeliveryDestinationVersion, DeliveryDraft, DeliveryAttempt,
]:
    if run.status != "SUCCESS" or run.decision != "COMMITTED":
        raise DeliveryOperationError(
            409, "DELIVERY_REPAIR_NOT_ALLOWED",
            "Sólo se repara evidencia de una entrega SUCCESS con commit remoto confirmado.",
        )
    attempts = db.scalars(select(DeliveryAttempt).where(DeliveryAttempt.run_id == run.id)).all()
    if (len(attempts) != 1 or attempts[0].status != "COMMITTED"
            or attempts[0].organization_id != run.organization_id):
        raise _evidence_problem("Se requiere un único DeliveryAttempt COMMITTED verificable.")
    attempt = attempts[0]
    config = db.get(Configuration, run.config_id)
    if config is None or config.organization_id != run.organization_id or config.module != "DELIVERY":
        raise _evidence_problem("La Configuration DELIVERY original no está disponible.")
    try:
        draft = DeliveryDraft.model_validate(config.config)
    except ValueError:
        raise _evidence_problem("La Configuration original no es legible.") from None
    if (configuration_hash(draft.snapshot()) != run.execution_plan.get("config_hash")
            or draft.dataset_version_id != run.dataset_version_id):
        raise _evidence_problem("El hash o la DatasetVersion de la Configuration no coincide con el Run.")
    source, dataset, source_artifact, frame = _owned_version(db, run.dataset_version_id, run.organization_id)
    source_identity = {
        "dataset_version_id": source.id, "source_sha256": source.sha256,
        "schema_hash": source.schema_hash, "canonical_artifact_id": source_artifact.id,
        "canonical_sha256": source_artifact.sha256,
    }
    if ("source_identity" in run.execution_plan
            and run.execution_plan["source_identity"] != source_identity):
        raise _evidence_problem("La identidad o los hashes del snapshot difieren del Run original.")
    signature = [{"name": column["name"], "logical_type": column["logical_type"]}
                 for column in source.schema_json]
    schema_hash = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    if (dataset.id != config.dataset_id or frame.height != source.row_count
            or frame.width != source.column_count or source.canonical_path != source_artifact.path
            or schema_hash != source.schema_hash
            or [column["name"] for column in source.schema_json] != frame.columns
            or _source_type_mismatches(source, draft)):
        raise _evidence_problem("El snapshot canónico no coincide con la Configuration persistida.")
    destination = db.get(DeliveryDestination, draft.destination_id)
    destination_version = db.get(DeliveryDestinationVersion, draft.destination_version_id)
    if (destination is None or destination_version is None
            or destination.organization_id != run.organization_id
            or destination_version.organization_id != run.organization_id
            or destination_version.destination_id != destination.id):
        raise _evidence_problem("La DestinationVersion original no está disponible en la organización.")
    if destination_version.config_hash != configuration_hash({
        **destination_version.config, "credential_revision": destination_version.secret_reference,
    }):
        raise _evidence_problem("El hash de la DestinationVersion original no coincide.")
    plan_checks = {
        "destination_id": destination.id, "destination_version_id": destination_version.id,
        "destination_version": destination_version.version, "sink_type": destination.sink_type,
        "target": draft.target.model_dump(), "write_strategy": draft.write_strategy,
    }
    if any(run.execution_plan.get(key) != value for key, value in plan_checks.items()):
        raise _evidence_problem("El plan persistido no coincide con el destino y la estrategia originales.")
    if (attempt.destination_version_id != destination_version.id
            or attempt.target_locator != f"{draft.target.schema_name}.{draft.target.table_name}"
            or attempt.idempotency_key != _delivery_idempotency(run, config)
            or not attempt.started_at or not attempt.finished_at
            or not run.started_at or not run.finished_at):
        raise _evidence_problem("La identidad, target o fechas del intento COMMITTED no son verificables.")
    metric_checks = {
        **{key: value for key, value in plan_checks.items() if key != "destination_version"},
        "delivery_attempt_id": attempt.id, "attempt_number": attempt.attempt_number,
    }
    for key in ("rows_attempted", "rows_written", "rows_inserted", "rows_updated", "bytes_sent"):
        value = getattr(attempt, key)
        if value is not None and (type(value) is not int or value < 0):
            raise _evidence_problem("El intento contiene métricas no verificables.")
        metric_checks[key] = value
    if (attempt.rows_attempted != source.row_count
            or any(key not in run.metrics or run.metrics[key] != value
                   for key, value in metric_checks.items())):
        raise _evidence_problem("Las métricas persistidas no coinciden con el intento COMMITTED.")
    for key in ("preflight_seconds", "write_seconds"):
        if key in run.metrics:
            value = run.metrics[key]
            if type(value) not in {float, int} or not math.isfinite(value) or value < 0:
                raise _evidence_problem("Las duraciones persistidas no son verificables.")
    return config, source, source_artifact, destination, destination_version, draft, attempt


def repair_delivery_evidence(db: Session, user: User, run_id: str) -> dict[str, Any]:
    """Reconstruct local evidence only. This path never obtains a sink or secret."""
    run = owned_delivery_run(db, user, run_id, lock=True)
    actor = Actor("USER", user.id, user.name)
    audit(db, "DELIVERY_EVIDENCE_REPAIR_STARTED", "run", run.id,
          "Reparación de evidencia local solicitada", actor, run.organization_id, run_id=run.id)
    try:
        # A failed publication rolls back its metadata, but retains the STARTED
        # audit event and the outer Run lock until FAILED can be committed.
        with db.begin_nested():
            context = _repair_context(db, run)
            before = {
                artifact.id: storage_provider.exists(artifact)
                for kind in ("DELIVERY_RECEIPT", "RUN_MANIFEST")
                for artifact in _evidence_candidates(db, run, kind)
            }
            links_before = db.scalars(select(ArtifactLink).where(
                ArtifactLink.organization_id == run.organization_id,
                ((ArtifactLink.source_type == "RUN") & (ArtifactLink.source_id == run.id))
                | ((ArtifactLink.target_type == "DELIVERY_ATTEMPT")
                   & (ArtifactLink.target_id == context[-1].id)),
            )).all()
            old_link_ids = {link.id for link in links_before}
            old_path, old_metrics = run.evidence_path, dict(run.metrics)
            receipt, manifest = _publish_delivery_evidence(db, run, *context)
            links_after = db.scalars(select(ArtifactLink).where(
                ArtifactLink.organization_id == run.organization_id,
                ((ArtifactLink.source_type == "RUN") & (ArtifactLink.source_id == run.id))
                | ((ArtifactLink.target_type == "DELIVERY_ATTEMPT")
                   & (ArtifactLink.target_id == context[-1].id)),
            )).all()
            already_valid = (
                before.get(receipt.id) is True and before.get(manifest.id) is True
                and old_path == manifest.path and old_metrics.get("receipt_artifact_id") == receipt.id
                and old_metrics.get("evidence_status") != "PENDING_REPAIR"
                and old_link_ids == {link.id for link in links_after}
            )
            # Clear the marker only after both immutable artifacts are materialized
            # and their links have been flushed successfully.
            storage_provider.materialize(receipt)
            storage_provider.materialize(manifest)
            run.evidence_path = manifest.path
            metrics = {**run.metrics, "receipt_artifact_id": receipt.id}
            metrics.pop("evidence_status", None)
            run.metrics = metrics
            run.progress_percent, run.progress_stage = 100, "Completado"
            result = {
                "run_id": run.id, "status": "ALREADY_VALID" if already_valid else "REPAIRED",
                "receipt_artifact_id": receipt.id, "manifest_artifact_id": manifest.id,
            }
        audit(db, "DELIVERY_EVIDENCE_REPAIRED", "run", run.id,
              "Evidencia local verificada; no se contactó el destino", actor, run.organization_id,
              {"status": result["status"], "artifact_ids": [receipt.id, manifest.id]}, run_id=run.id)
        db.commit()
        return result
    except Exception as error:  # noqa: BLE001 - sanitize provider errors; never repeat remote I/O.
        controlled = error if isinstance(error, DeliveryOperationError) else DeliveryOperationError(
            409, "DELIVERY_EVIDENCE_REPAIR_FAILED",
            "No fue posible publicar evidencia local válida. No se contactó el destino.",
        )
        audit(db, "DELIVERY_EVIDENCE_REPAIR_FAILED", "run", run.id,
              "Reparación local detenida; resultado remoto conservado", actor, run.organization_id,
              {"error_code": controlled.code}, run_id=run.id)
        db.commit()
        raise controlled from None


def review_dto(review: DeliveryOperationalReview) -> dict[str, Any]:
    return {
        "id": review.id, "run_id": review.run_id, "delivery_attempt_id": review.delivery_attempt_id,
        "reviewer_id": review.reviewer_id, "reviewer_name": review.reviewer_name,
        "outcome": review.outcome, "note": review.note,
        "verified_at": iso(review.verified_at), "created_at": iso(review.created_at),
    }


def record_delivery_review(
    db: Session, user: User, run_id: str, body: DeliveryReviewBody,
) -> DeliveryOperationalReview:
    run = owned_delivery_run(db, user, run_id, lock=True)
    attempt = db.scalar(select(DeliveryAttempt).where(
        DeliveryAttempt.id == body.delivery_attempt_id,
        DeliveryAttempt.run_id == run.id,
        DeliveryAttempt.organization_id == user.organization_id,
    ))
    reviewer = db.scalar(select(User).where(
        User.id == user.id, User.organization_id == run.organization_id, User.active.is_(True),
    ))
    if attempt is None or reviewer is None:
        raise DeliveryOperationError(404, "NOT_FOUND", "No se encontró el intento o revisor.")
    if run.status != "UNKNOWN" or run.decision != "UNKNOWN" or attempt.status != "UNKNOWN":
        raise DeliveryOperationError(
            409, "DELIVERY_REVIEW_NOT_ALLOWED", "La revisión operacional sólo documenta intentos UNKNOWN.",
        )
    verified_at = body.verified_at or utcnow()
    if verified_at < attempt.started_at.replace(tzinfo=UTC):
        raise DeliveryOperationError(
            422, "INVALID_VERIFICATION_TIME", "La verificación no puede preceder al intento remoto.",
        )
    review = DeliveryOperationalReview(
        organization_id=run.organization_id, run_id=run.id, delivery_attempt_id=attempt.id,
        reviewer_id=reviewer.id, reviewer_name=reviewer.name,
        outcome=body.outcome, note=body.note, verified_at=verified_at,
    )
    db.add(review)
    db.flush()
    audit(db, "DELIVERY_UNKNOWN_REVIEWED", "delivery_review", review.id,
          "Verificación externa documentada; el intento original conserva UNKNOWN",
          Actor("USER", reviewer.id, reviewer.name), run.organization_id,
          {"delivery_attempt_id": attempt.id, "review_id": review.id,
           "outcome": review.outcome, "verified_at": iso(review.verified_at)}, run_id=run.id)
    db.commit()
    return review
