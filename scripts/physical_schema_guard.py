"""Compare a runtime's application tables with its physical SQL catalog.

The default application schema and any schemas explicitly used by the metadata
are checked. Constraint names and dialect-specific SQL type spelling are not
part of this contract. The only permitted non-model table is alembic_version in
the default schema. Runtime Trackvance imports occur only in the standalone CLI.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import Column, ForeignKeyConstraint, MetaData, Table, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError


class PhysicalSchemaMismatch(ValueError):
    """A safe diagnostic without values originating in the physical catalog."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def metadata_before_additions(metadata: MetaData, tables: Iterable[str], columns: Mapping[str, Mapping[str, Any]]) -> MetaData:
    """Build a named historical column/FK contract for isolated migration proofs.

    This does not alter the live guard or infer historical schema by reflection.
    Callers must enumerate the exact additive migration boundary they prove.
    """
    excluded = set(tables)
    if not excluded.issubset(metadata.tables) or any(name not in metadata.tables or not set(fields).issubset(metadata.tables[name].c.keys())
            for name, fields in columns.items()):
        raise PhysicalSchemaMismatch("HISTORICAL_SCHEMA_BOUNDARY_INVALID", "La frontera histórica no corresponde al modelo.")
    previous = MetaData()
    for name, table in metadata.tables.items():
        if name not in excluded:
            Table(table.name, previous, *(Column(column.name, column.type, primary_key=column.primary_key)
                for column in table.columns if column.name not in columns.get(name, {})), schema=table.schema)
    for name, table in metadata.tables.items():
        if name in excluded:
            continue
        for constraint in table.foreign_key_constraints:
            elements = list(constraint.elements)
            if any(element.parent.name in columns.get(name, {}) for element in elements):
                continue
            if any(element.column.table.key in excluded for element in elements):
                raise PhysicalSchemaMismatch("HISTORICAL_SCHEMA_BOUNDARY_INVALID", "Una relación histórica requiere una tabla excluida.")
            previous.tables[name].append_constraint(ForeignKeyConstraint(
                [element.parent.name for element in elements], [element.target_fullname for element in elements],
                ondelete=constraint.ondelete, onupdate=constraint.onupdate, deferrable=constraint.deferrable, initially=constraint.initially))
    return previous


def _schema(schema: str | None, default: str) -> str:
    return schema if schema is not None else default


def _foreign_key(
    columns: Iterable[str], referred_columns: Iterable[str],
    referred_schema: str, referred_table: str, options: Mapping[str, Any],
) -> tuple[Any, ...]:
    # Preserve composite-constraint grouping; order is immaterial when the
    # complete local -> referred mapping is the same. Counter retains duplicates.
    pairs = tuple(sorted(zip(columns, referred_columns, strict=True)))
    if not pairs:
        raise PhysicalSchemaMismatch("PHYSICAL_FOREIGN_KEYS_MISMATCH", "El catálogo de claves foráneas no coincide.")
    deferred = bool(options.get("deferrable"))
    return (
        referred_schema, referred_table, pairs,
        str(options.get("ondelete") or "NO ACTION").upper(),
        str(options.get("onupdate") or "NO ACTION").upper(),
        deferred,
        str(options.get("initially") or "IMMEDIATE").upper() if deferred else "IMMEDIATE",
    )


def validate_physical_schema(bind: Engine | Connection, metadata: MetaData) -> dict[str, int]:
    """Reject missing/extra application tables, columns and foreign keys.

    The caller supplies the metadata of the installed runtime, including an
    authentic historical runtime. No migration, DDL or data mutation occurs.
    Business schemas co-hosted outside the application's modeled namespaces do
    not become application metadata implicitly.
    """
    inspector = inspect(bind)
    default = inspector.default_schema_name
    if default is None:
        raise PhysicalSchemaMismatch("PHYSICAL_SCHEMA_UNAVAILABLE", "El schema de aplicación no se puede verificar.")
    expected = {(_schema(table.schema, default), table.name): table for table in metadata.tables.values()}
    schemas = {default, *(schema for schema, _name in expected)}
    actual = {(schema, name) for schema in schemas for name in inspector.get_table_names(schema=schema)}
    actual.discard((default, "alembic_version"))
    if actual != set(expected):
        raise PhysicalSchemaMismatch("PHYSICAL_TABLES_MISMATCH", "El catálogo físico contiene tablas desconocidas o faltantes.")

    column_count = foreign_key_count = 0
    for (schema, name), table in sorted(expected.items()):
        columns = {item["name"] for item in inspector.get_columns(name, schema=schema)}
        if columns != set(table.columns.keys()):
            raise PhysicalSchemaMismatch("PHYSICAL_COLUMNS_MISMATCH", "El catálogo físico contiene columnas desconocidas o faltantes.")
        column_count += len(columns)
        modeled_keys = Counter()
        for constraint in table.foreign_key_constraints:
            elements = list(constraint.elements)
            target = elements[0].column.table
            modeled_keys[_foreign_key(
                (element.parent.name for element in elements),
                (element.column.name for element in elements),
                _schema(target.schema, default), target.name,
                {"ondelete": constraint.ondelete, "onupdate": constraint.onupdate,
                 "deferrable": constraint.deferrable, "initially": constraint.initially},
            )] += 1
        physical_keys = Counter(_foreign_key(
            item["constrained_columns"], item["referred_columns"],
            _schema(item.get("referred_schema"), default), item["referred_table"],
            item.get("options") or {},
        ) for item in inspector.get_foreign_keys(name, schema=schema))
        if modeled_keys != physical_keys:
            raise PhysicalSchemaMismatch("PHYSICAL_FOREIGN_KEYS_MISMATCH", "El catálogo de claves foráneas no coincide.")
        foreign_key_count += sum(modeled_keys.values())
    return {"tables": len(expected), "columns": column_count, "foreign_keys": foreign_key_count}


def main() -> int:
    try:
        from trackvance import (
            models,  # noqa: F401 - register the installed runtime's mappings.
        )
        from trackvance.db import Base, engine

        report = validate_physical_schema(engine, Base.metadata)
    except PhysicalSchemaMismatch as error:
        print(json.dumps({"status": "FAIL", "error_code": error.code}))
        return 1
    except (SQLAlchemyError, OSError, ValueError, KeyError, ImportError):
        print(json.dumps({"status": "FAIL", "error_code": "PHYSICAL_SCHEMA_INSPECTION_FAILED"}))
        return 1
    print(json.dumps({"status": "PASS", **report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
