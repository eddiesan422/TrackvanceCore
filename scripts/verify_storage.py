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

SCHEMA_VERSION = 2
ACTIVE_STATUSES = frozenset({"QUEUED", "RUNNING"})
POLYMORPHIC_TABLES = {
    "ARTIFACT": "artifacts",
    "DATASET_VERSION": "dataset_versions",
    "RUN": "runs",
    "CONNECTION_VERSION": "external_connection_versions",
    "EXCEPTION": "exceptions",
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
            table = POLYMORPHIC_TABLES.get(entity_type)
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
    return checks


def snapshot() -> dict[str, Any]:
    from sqlalchemy import select, text

    from trackvance.artifactstore import artifact_store
    from trackvance.credential_store import secret_store
    from trackvance.db import Base, SessionLocal
    from trackvance.models import Artifact, ExternalConnectionVersion, Job, Run

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tables": {},
        "verified_artifacts": 0,
        "verified_secrets": 0,
        "validated_relationships": 0,
    }
    with SessionLocal() as session:
        active_run = session.scalar(select(Run.id).where(Run.status.in_(ACTIVE_STATUSES)))
        active_job = session.scalar(select(Job.id).where(Job.status.in_(ACTIVE_STATUSES)))
        if active_run or active_job:
            raise ValueError("Finaliza las ejecuciones pendientes antes de tomar la huella.")

        report["migration"] = session.scalar(text("SELECT version_num FROM alembic_version"))
        rows: dict[str, list[Mapping[str, Any]]] = {}
        for name, table in sorted(Base.metadata.tables.items()):
            table_rows = list(session.execute(select(table)).mappings())
            rows[name] = table_rows
            report["tables"][name] = dict(
                sorted((str(row["id"]), _canonical_hash(row)) for row in table_rows)
            )

        foreign_keys = []
        for name, table in Base.metadata.tables.items():
            for foreign_key in table.foreign_keys:
                foreign_keys.append(
                    (
                        name,
                        foreign_key.parent.name,
                        foreign_key.column.table.name,
                        foreign_key.column.name,
                    )
                )
        report["validated_relationships"] = validate_relationships(rows, foreign_keys)

        for artifact in session.scalars(select(Artifact)):
            artifact_store.verify(artifact)
            report["verified_artifacts"] += 1
        for version in session.scalars(select(ExternalConnectionVersion)):
            # Materialize and immediately discard; neither value nor reference reaches the report.
            secret_store.get(version.organization_id, version.secret_reference)
            report["verified_secrets"] += 1
    return report


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
    comparison = commands.add_parser("compare")
    comparison.add_argument("before", type=Path)
    comparison.add_argument("after", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            print(json.dumps(snapshot(), indent=2, sort_keys=True))
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
