"""Fingerprint persisted state and validate artifacts, lineage and encrypted secrets.

Run ``snapshot`` inside the API container while the installation is quiescent.
The JSON contains record identifiers, hashes and aggregate counts only. It never
contains business values, credentials, secret references or filesystem paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 2
LEGACY_MIGRATION = "0007_monitor_scheduling"
CURRENT_MIGRATION = "0008_data_delivery"
ACTIVE_STATUSES = frozenset({"QUEUED", "RUNNING"})
JOB_LANES = frozenset({"DEFAULT", "DELIVERY"})
DELIVERY_ATTEMPT_STATUSES = frozenset({"STARTED", "COMMITTED", "FAILED", "UNKNOWN"})
DELIVERY_TABLES = frozenset(
    {
        "delivery_destinations",
        "delivery_destination_versions",
        "delivery_attempts",
    }
)
POLYMORPHIC_TABLES = {
    "ARTIFACT": "artifacts",
    "DATASET_VERSION": "dataset_versions",
    "RUN": "runs",
    "CONNECTION_VERSION": "external_connection_versions",
    "EXCEPTION": "exceptions",
    "DELIVERY_DESTINATION": "delivery_destinations",
    "DELIVERY_DESTINATION_VERSION": "delivery_destination_versions",
    "DELIVERY_ATTEMPT": "delivery_attempts",
}
LEGACY_POLYMORPHIC_TABLES = {
    key: value
    for key, value in POLYMORPHIC_TABLES.items()
    if not key.startswith("DELIVERY_")
}


def _canonical_hash(row: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(row), sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _row_index(
    rows: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, dict[str, Mapping[str, Any]]]:
    return {
        table: {str(row["id"]): row for row in values}
        for table, values in rows.items()
    }


def _require_same_organization(
    child: Mapping[str, Any], parent: Mapping[str, Any], description: str
) -> None:
    child_org = child.get("organization_id")
    parent_org = parent.get("organization_id")
    if child_org is not None and parent_org is not None and child_org != parent_org:
        raise ValueError(f"Alcance de organización inválido en {description}.")


def validate_relationships(
    rows: Mapping[str, list[Mapping[str, Any]]],
    foreign_keys: Iterable[tuple[str, str, str, str]],
    *,
    compatibility_v2: bool = False,
) -> int:
    """Validate every declared FK plus application-level polymorphic lineage."""

    index = _row_index(rows)
    checks = 0
    for child_table, child_column, parent_table, parent_column in foreign_keys:
        parent_rows = rows.get(parent_table, [])
        parent_index = {str(row[parent_column]): row for row in parent_rows}
        for child in rows.get(child_table, []):
            value = child.get(child_column)
            if value is None:
                continue
            parent = parent_index.get(str(value))
            if parent is None:
                raise ValueError(
                    f"Referencia persistida inválida: {child_table}.{child_column}."
                )
            _require_same_organization(
                child, parent, f"{child_table}.{child_column}->{parent_table}.{parent_column}"
            )
            checks += 1

    # These relationships intentionally have no SQL FK in the prototype schema.
    logical_references = (
        ("dataset_versions", "parent_version_id", "dataset_versions"),
        ("dataset_versions", "source_run_id", "runs"),
        ("runs", "output_version_id", "dataset_versions"),
    )
    for child_table, child_column, parent_table in logical_references:
        for child in rows.get(child_table, []):
            value = child.get(child_column)
            if value is None:
                continue
            parent = index.get(parent_table, {}).get(str(value))
            if parent is None:
                raise ValueError(f"Linaje inválido: {child_table}.{child_column}.")
            _require_same_organization(child, parent, f"{child_table}.{child_column}")
            checks += 1

    for link in rows.get("artifact_links", []):
        for side in ("source", "target"):
            entity_type = str(link[f"{side}_type"])
            polymorphic_tables = (
                LEGACY_POLYMORPHIC_TABLES if compatibility_v2 else POLYMORPHIC_TABLES
            )
            table = polymorphic_tables.get(entity_type)
            if table is None:
                raise ValueError(f"Tipo de linaje no reconocido: {entity_type}.")
            entity = index.get(table, {}).get(str(link[f"{side}_id"]))
            if entity is None:
                raise ValueError(f"Extremo de linaje inexistente: {side} {entity_type}.")
            _require_same_organization(link, entity, f"artifact_links.{side}_id")
            checks += 1

    for version in rows.get("dataset_versions", []):
        source = (version.get("ingestion_metadata") or {}).get("source")
        if not source:
            continue
        connection_id = source.get("connection_id")
        connection_version_id = source.get("connection_version_id")
        connection = index.get("external_connections", {}).get(str(connection_id))
        connection_version = index.get("external_connection_versions", {}).get(
            str(connection_version_id)
        )
        if connection is None or connection_version is None:
            raise ValueError("Identidad de fuente externa inválida en dataset_versions.")
        if str(connection_version.get("connection_id")) != str(connection_id):
            raise ValueError("La versión de conexión no pertenece a la conexión del snapshot.")
        if source.get("config_hash") != connection_version.get("config_hash"):
            raise ValueError("El config_hash del snapshot no coincide con su versión de conexión.")
        _require_same_organization(version, connection, "dataset_versions.source.connection_id")
        _require_same_organization(
            version, connection_version, "dataset_versions.source.connection_version_id"
        )
        checks += 2

    if not compatibility_v2:
        for job in rows.get("jobs", []):
            lane = str(job.get("lane", ""))
            if lane not in JOB_LANES:
                raise ValueError("Lane persistido no reconocido en jobs.")
            run = index.get("runs", {}).get(str(job.get("run_id")))
            if run is None:
                # The declared SQL FK reports the same corruption with a more precise field.
                continue
            expected_lane = (
                "DELIVERY"
                if str(run.get("module", "")).upper() == "DELIVERY"
                else "DEFAULT"
            )
            if lane != expected_lane:
                raise ValueError("La lane del job no coincide con el módulo del Run.")
            checks += 1

        for attempt in rows.get("delivery_attempts", []):
            if str(attempt.get("status", "")) not in DELIVERY_ATTEMPT_STATUSES:
                raise ValueError("Estado persistido no reconocido en delivery_attempts.")
            run = index.get("runs", {}).get(str(attempt.get("run_id")))
            if run is not None and str(run.get("module", "")).upper() != "DELIVERY":
                raise ValueError("Un DeliveryAttempt debe pertenecer a un Run DELIVERY.")
            checks += 1
    return checks


def _table_hashes(
    rows: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, dict[str, str]]:
    return {
        name: dict(sorted((str(row["id"]), _canonical_hash(row)) for row in table_rows))
        for name, table_rows in sorted(rows.items())
    }


def legacy_v2_report(
    rows: Mapping[str, list[Mapping[str, Any]]],
    foreign_keys: Iterable[tuple[str, str, str, str]],
    *,
    current_migration: str | None,
    verified_artifacts: int,
    verified_source_secrets: int,
) -> dict[str, Any]:
    """Recalculate the exact 0.4.1 fingerprint after the deterministic 0008 upgrade."""

    foreign_keys = list(foreign_keys)
    if current_migration != CURRENT_MIGRATION:
        raise ValueError("La compatibilidad 0.4.1 requiere la migración 0008 aplicada.")
    if any(rows.get(table) for table in DELIVERY_TABLES):
        raise ValueError("Un backup 0.4.1 no puede contener registros de Data Delivery.")
    if any(str(run.get("module", "")).upper() == "DELIVERY" for run in rows.get("runs", [])):
        raise ValueError("Un backup 0.4.1 no puede contener Runs DELIVERY.")
    if any(str(job.get("lane", "")) != "DEFAULT" for job in rows.get("jobs", [])):
        raise ValueError("La migración 0008 debe asignar lane DEFAULT a todos los Jobs 0.4.1.")

    # Validate the upgraded database before projecting it back to the legacy schema.
    validate_relationships(rows, foreign_keys)

    legacy_rows: dict[str, list[Mapping[str, Any]]] = {}
    for name, table_rows in rows.items():
        if name in DELIVERY_TABLES:
            continue
        if name == "jobs":
            legacy_rows[name] = [
                {key: value for key, value in dict(row).items() if key != "lane"}
                for row in table_rows
            ]
        else:
            legacy_rows[name] = table_rows
    legacy_foreign_keys = [
        foreign_key
        for foreign_key in foreign_keys
        if foreign_key[0] not in DELIVERY_TABLES
        and foreign_key[2] not in DELIVERY_TABLES
    ]
    return {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "tables": _table_hashes(legacy_rows),
        "verified_artifacts": verified_artifacts,
        "verified_secrets": verified_source_secrets,
        "validated_relationships": validate_relationships(
            legacy_rows,
            legacy_foreign_keys,
            compatibility_v2=True,
        ),
        "migration": LEGACY_MIGRATION,
    }


def _snapshot_inputs() -> tuple[
    str | None,
    dict[str, list[Mapping[str, Any]]],
    list[tuple[str, str, str, str]],
    int,
    int,
    int,
]:
    from sqlalchemy import select, text

    from trackvance.artifactstore import artifact_store
    from trackvance.credential_store import secret_store
    from trackvance.db import Base, SessionLocal
    from trackvance.delivery_credential_store import destination_secret_store
    from trackvance.models import (
        Artifact,
        DeliveryDestinationVersion,
        ExternalConnectionVersion,
        Job,
        Run,
    )

    with SessionLocal() as session:
        active_run = session.scalar(select(Run.id).where(Run.status.in_(ACTIVE_STATUSES)))
        active_job = session.scalar(select(Job.id).where(Job.status.in_(ACTIVE_STATUSES)))
        if active_run or active_job:
            raise ValueError("Finaliza las ejecuciones pendientes antes de tomar la huella.")

        migration = session.scalar(text("SELECT version_num FROM alembic_version"))
        rows: dict[str, list[Mapping[str, Any]]] = {
            name: list(session.execute(select(table)).mappings())
            for name, table in sorted(Base.metadata.tables.items())
        }
        foreign_keys = [
            (
                name,
                foreign_key.parent.name,
                foreign_key.column.table.name,
                foreign_key.column.name,
            )
            for name, table in Base.metadata.tables.items()
            for foreign_key in table.foreign_keys
        ]

        verified_artifacts = 0
        for artifact in session.scalars(select(Artifact)):
            artifact_store.verify(artifact)
            verified_artifacts += 1
        verified_source_secrets = 0
        for version in session.scalars(select(ExternalConnectionVersion)):
            secret_store.get(version.organization_id, version.secret_reference)
            verified_source_secrets += 1
        verified_delivery_secrets = 0
        for version in session.scalars(select(DeliveryDestinationVersion)):
            destination_secret_store.get(version.organization_id, version.secret_reference)
            verified_delivery_secrets += 1
    return (
        migration,
        rows,
        foreign_keys,
        verified_artifacts,
        verified_source_secrets,
        verified_delivery_secrets,
    )


def snapshot() -> dict[str, Any]:
    (
        migration,
        rows,
        foreign_keys,
        verified_artifacts,
        verified_source_secrets,
        verified_delivery_secrets,
    ) = _snapshot_inputs()
    return {
        "schema_version": SCHEMA_VERSION,
        "tables": _table_hashes(rows),
        "verified_artifacts": verified_artifacts,
        "verified_secrets": verified_source_secrets + verified_delivery_secrets,
        "verified_source_secrets": verified_source_secrets,
        "verified_delivery_secrets": verified_delivery_secrets,
        "validated_relationships": validate_relationships(rows, foreign_keys),
        "migration": migration,
    }


def snapshot_legacy_v2() -> dict[str, Any]:
    (
        migration,
        rows,
        foreign_keys,
        verified_artifacts,
        verified_source_secrets,
        verified_delivery_secrets,
    ) = _snapshot_inputs()
    if verified_delivery_secrets:
        raise ValueError("Un backup 0.4.1 no puede contener secretos de Data Delivery.")
    return legacy_v2_report(
        rows,
        foreign_keys,
        current_migration=migration,
        verified_artifacts=verified_artifacts,
        verified_source_secrets=verified_source_secrets,
    )


def compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if before.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Versión del informe previo no reconocida.")
    if after.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Versión del informe posterior no reconocida.")
    if before == after:
        return
    all_tables = sorted(set(before.get("tables", {})) | set(after.get("tables", {})))
    changed = [
        name
        for name in all_tables
        if before.get("tables", {}).get(name) != after.get("tables", {}).get(name)
    ]
    detail = ", ".join(changed) or "migración/artifacts/secretos/linaje"
    raise ValueError("La persistencia cambió: " + detail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot")
    commands.add_parser("snapshot-legacy-v2")
    comparison = commands.add_parser("compare")
    comparison.add_argument("before", type=Path)
    comparison.add_argument("after", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            print(json.dumps(snapshot(), indent=2, sort_keys=True))
        elif args.command == "snapshot-legacy-v2":
            print(json.dumps(snapshot_legacy_v2(), indent=2, sort_keys=True))
        else:
            compare(
                json.loads(args.before.read_text(encoding="utf-8-sig")),
                json.loads(args.after.read_text(encoding="utf-8-sig")),
            )
            print("OK: registros, artifacts, secretos y linaje son idénticos.")
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
