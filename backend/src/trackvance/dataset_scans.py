"""Verified multi-part scans, exact global profiling and transactional publication."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from pathlib import Path

import duckdb
import polars as pl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .acquisition_config import AcquisitionLimits
from .artifactstore import ArtifactIntegrityError, link_artifact, storage_provider
from .audit_context import Actor
from .batch_readers import MAX_CELL_BYTES, RECORD_NUMBER_COLUMN, delimited_rows
from .models import Artifact, Dataset, DatasetVersion, uid
from .portable_engine import PolarsCompiler, compile_rule
from .processing import (
    ProcessingError,
    iso_date,
    iso_timestamp,
    logical_type_from_native,
    money,
    validate_column_overrides,
)


def version_paths(db: Session, version: DatasetVersion) -> list[Path]:
    if version.original_path:
        storage_provider.materialize_reference(version.original_path, expected_sha256=version.sha256,
                                               expected_size=version.size_bytes)
    artifact = db.get(Artifact, version.canonical_artifact_id) if version.canonical_artifact_id else None
    if version.canonical_artifact_id and artifact is None:
        raise ArtifactIntegrityError("ARTIFACT_CANONICAL_UNAVAILABLE: El canónico registrado no está disponible.")
    if artifact is not None:
        if artifact.organization_id != version.organization_id:
            raise ArtifactIntegrityError("ARTIFACT_SCOPE_MISMATCH: El canónico no pertenece a la versión.")
        return storage_provider.dataset_paths(artifact)
    return [storage_provider.materialize_reference(version.canonical_path,
            expected_sha256=version.sha256 if not version.original_path else None,
            expected_size=version.size_bytes if not version.original_path else None)]


def sample_paths(paths: list[Path], columns: list[str], limit: int = 20, *,
                 byte_limit: int = 8 * 1024 * 1024,
                 observed_record_bytes_upper_bound: int | None = None) -> Iterator[dict]:
    """Read only a presentation sample from already verified Parquet parts.

    The persisted profile contains global statistics. No COUNT, DISTINCT or
    analytical connection is needed to present its first records. Arrow batches
    are bounded before conversion to Python; historical unknown widths use one
    record. The caller applies the encoded JSON response budget to these rows.
    """
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    if not 0 <= limit <= 100 or byte_limit < 1:
        raise ValueError("El límite de la muestra debe ser positivo y acotado.")
    width = observed_record_bytes_upper_bound
    batch_rows = (max(1, min(limit or 1, byte_limit // width))
                  if type(width) is int and width > 0 else 1)
    remaining = limit
    for path in paths:
        if not remaining:
            return
        with pq.ParquetFile(path, memory_map=False, pre_buffer=False) as parquet:
            if not set(columns) <= set(parquet.schema_arrow.names):
                raise ArtifactIntegrityError("DATASET_PART_INVALID: Faltan columnas en la muestra.")
            for batch in parquet.iter_batches(batch_size=min(batch_rows, remaining),
                                              columns=columns, use_threads=False):
                for row in batch.to_pylist():
                    yield row
                    remaining -= 1
                    if not remaining:
                        return


@contextmanager
def bounded_scan(paths: list[Path], limits: AcquisitionLimits | None = None):
    effective = limits or AcquisitionLimits.configured()
    temporary = storage_provider.temporary_path(".spill")
    temporary.mkdir()
    try:
        connection = duckdb.connect(":memory:", config={
            "memory_limit": f"{effective.memory_bytes}B", "threads": "1",
            "temp_directory": str(temporary), "max_temp_directory_size": f"{effective.max_observed_bytes}B",
            "preserve_insertion_order": "true",
        })
        try:
            connection.execute("SET enable_progress_bar = false")
            connection.read_parquet([str(path) for path in paths]).create_view("population")
            yield connection
        finally:
            connection.close()
    finally:
        # Exactly the provider-allocated private spill directory, never a root,
        # business artifact, shared staging directory or another job's files.
        shutil.rmtree(temporary, ignore_errors=True)


def iter_parquet_batches(paths: list[Path], batch_rows: int = 5_000,
                          *, columns: list[str] | None = None,
                          limits: AcquisitionLimits | None = None,
                          max_record_bytes: int | None = None) -> Iterator[pl.DataFrame]:
    if not 1 <= batch_rows <= 50_000:
        raise ValueError("El lote debe tener entre 1 y 50.000 registros.")
    effective = limits or AcquisitionLimits.configured()
    for path in paths:
        schema = pl.read_parquet_schema(path)
        selection = columns or list(schema)
        with bounded_scan([path], limits) as connection:
            query = ",".join('"' + name.replace('"', '""') + '"' for name in selection)
            cursor = connection.execute(f"SELECT {query} FROM population")
            width_bound = (max_record_bytes if type(max_record_bytes) is int and max_record_bytes > 0
                           else max(1, len(selection)) * (32 + 4 * MAX_CELL_BYTES))
            safe_rows = min(batch_rows, max(1, effective.batch_bytes // width_bound))
            while rows := cursor.fetchmany(safe_rows):
                yield pl.DataFrame(rows, schema={name: schema[name] for name in selection}, orient="row")


def iter_version_batches(db: Session, version: DatasetVersion, batch_rows: int = 5_000,
                         *, include_record_numbers: bool = False) -> Iterator[pl.DataFrame]:
    paths = version_paths(db, version)
    business = [column["name"] for column in version.schema_json]
    internal = (version.ingestion_metadata or {}).get("record_number_column")
    columns = business + ([internal] if internal and include_record_numbers else [])
    position = 0
    legacy_rows = None
    if include_record_numbers and not internal and version.original_path:
        metadata = version.ingestion_metadata or {}
        if metadata.get("source_format") in {None, "CSV", "TXT"}:
            original = storage_provider.materialize_reference(version.original_path,
                expected_sha256=version.sha256, expected_size=version.size_bytes)
            delimiter = metadata.get("reader_options", {}).get("delimiter")
            if delimiter is None:
                from .dataset_readers import _detect_delimiter, _text_sample
                delimiter = _detect_delimiter(_text_sample(original), csv_legacy_default=True)
            legacy_rows = iter(delimited_rows(original, delimiter))
    width_bound = (version.profile or {}).get("observed_record_bytes_upper_bound")
    if type(width_bound) is not int or width_bound <= 0:
        width_bound = None
    elif include_record_numbers and internal:
        width_bound += 64  # fixed Int64 scalar, Arrow slot and Python conversion.
    for batch in iter_parquet_batches(paths, batch_rows, columns=columns, max_record_bytes=width_bound):
        if include_record_numbers and not internal:
            numbers = []
            for _ in range(batch.height):
                position += 1
                if legacy_rows is not None:
                    row = next(legacy_rows, None)
                    if row is None:
                        raise ProcessingError("RECORD_NUMBER_MISMATCH: El mapa de registros no coincide.")
                    numbers.append(row[1])
                else:
                    numbers.append(position)
            batch = batch.with_columns(pl.Series(RECORD_NUMBER_COLUMN, numbers, dtype=pl.Int64))
        yield batch
    if legacy_rows is not None and next(legacy_rows, None) is not None:
        raise ProcessingError("RECORD_NUMBER_MISMATCH: El mapa de registros no coincide.")


def profile_paths(paths: list[Path], *, column_overrides: dict | None = None,
                  native_types: dict[str, str] | None = None,
                  limits: AcquisitionLimits | None = None,
                  check: Callable[[], None] | None = None) -> tuple[list, dict, str]:
    """Full-population counts and uniqueness spill to disk; Decimal remains exact.

    One column's scalar statistics are accumulated with constant state. Distinct
    values and duplicate groups remain in the bounded analytical engine. No
    global operation is computed independently for each acquisition batch.
    """
    effective, checkpoint = limits or AcquisitionLimits.configured(), check or (lambda: None)
    physical = pl.read_parquet_schema(paths[0])
    names = [name for name in physical if not name.startswith("__tv_")]
    if any(physical[name] not in {pl.String, pl.Null} for name in names):
        raise ProcessingError("CANONICAL_SCHEMA_INVALID: Las columnas canónicas deben ser texto o null.")
    overrides = validate_column_overrides(column_overrides or {}, names)
    native = native_types or {}
    schema, profiles = [], []
    observed_record_bytes_upper_bound = 0
    with bounded_scan(paths, effective) as connection:
        record = connection.execute("SELECT COUNT(*) FROM population").fetchone()
        assert record is not None
        count = int(record[0])
        if count > effective.max_rows:
            raise ProcessingError("ACQUISITION_SIZE_LIMIT: El dataset supera el límite de registros.")
        for name in names:
            checkpoint()
            quoted = '"' + name.replace('"', '""') + '"'
            summary = connection.execute(f"SELECT COUNT({quoted}), COUNT(DISTINCT {quoted}), MAX(OCTET_LENGTH(encode(CAST({quoted} AS VARCHAR)))) FROM population").fetchone()
            unique = connection.execute(f"SELECT COUNT(*) FROM (SELECT {quoted} FROM population WHERE {quoted} IS NOT NULL GROUP BY {quoted} HAVING COUNT(*) = 1)").fetchone()
            assert summary is not None and unique is not None
            nonnull, distinct, singletons = int(summary[0]), int(summary[1]), int(unique[0])
            maximum_utf8_bytes = int(summary[2] or 0)
            if maximum_utf8_bytes > MAX_CELL_BYTES:
                raise ProcessingError("ACQUISITION_CELL_SIZE_LIMIT: Una celda canónica supera el límite observado.")
            null_count = count - nonnull
            all_dates = all_timestamps = all_numbers = True
            date_min = date_max = timestamp_min = timestamp_max = None
            decimal_min = decimal_max = None
            decimal_total, decimal_count, decimal_precision = Decimal(0), 0, 1
            date_count = timestamp_count = 0
            min_length = max_length = None
            override = overrides.get(name, {})
            declared = override.get("logical_type")
            expression = compile_rule({"type": "type", "column": name,
                                       "parameters": {"logical_type": declared}}, datetime.now(UTC)) if declared and declared != "STRING" else None
            cursor = connection.execute(f"SELECT {quoted} FROM population")
            # This is a full-population maximum computed by the bounded SQL
            # engine, never an average or sample. Unicode Python strings use at
            # most four bytes per observed byte plus a fixed scalar allowance.
            safe_rows = min(effective.batch_rows, max(1, effective.batch_bytes // (32 + 4 * maximum_utf8_bytes)))
            while rows := cursor.fetchmany(safe_rows):
                checkpoint()
                values = [row[0] for row in rows]
                if expression is not None:
                    frame = pl.DataFrame({name: values}, schema={name: pl.String})
                    if not all(PolarsCompiler().validate(frame, expression)):
                        raise ProcessingError("ACQUISITION_OVERRIDE_INVALID: Los valores no cumplen un tipo lógico confirmado.")
                for value in values:
                    if value is None:
                        continue
                    if not isinstance(value, str):
                        raise ProcessingError("CANONICAL_SCHEMA_INVALID: Los valores canónicos deben ser texto o null.")
                    length = len(value)
                    min_length = length if min_length is None else min(min_length, length)
                    max_length = length if max_length is None else max(max_length, length)
                    parsed_date, parsed_timestamp, parsed_decimal = iso_date(value), iso_timestamp(value), money(value)
                    all_dates &= parsed_date is not None
                    all_timestamps &= parsed_timestamp is not None
                    all_numbers &= parsed_decimal is not None
                    if parsed_date is not None:
                        date_count += 1
                        date_min = parsed_date if date_min is None else min(date_min, parsed_date)
                        date_max = parsed_date if date_max is None else max(date_max, parsed_date)
                    if parsed_timestamp is not None:
                        timestamp_count += 1
                        timestamp_min = parsed_timestamp if timestamp_min is None else min(timestamp_min, parsed_timestamp)
                        timestamp_max = parsed_timestamp if timestamp_max is None else max(timestamp_max, parsed_timestamp)
                    if parsed_decimal is not None:
                        decimal_count += 1
                        decimal_precision = max(decimal_precision, len(parsed_decimal.as_tuple().digits) + abs(int(parsed_decimal.as_tuple().exponent)))
                        precision = max(28, 2 * decimal_precision + len(str(effective.max_rows)) + 10)
                        with localcontext(Context(prec=precision)):
                            decimal_total += parsed_decimal
                        decimal_min = parsed_decimal if decimal_min is None else min(decimal_min, parsed_decimal)
                        decimal_max = parsed_decimal if decimal_max is None else max(decimal_max, parsed_decimal)
            identifier = override.get("semantic_tag", "IDENTIFIER" if name.lower() == "id" or name.lower().endswith("_id") else None)
            native_logical = logical_type_from_native(native.get(name))
            logical = "STRING"
            if identifier != "IDENTIFIER" and native_logical:
                logical = native_logical
            elif nonnull and identifier != "IDENTIFIER":
                logical = "DATE" if all_dates else "TIMESTAMP" if all_timestamps else "DECIMAL" if all_numbers else "STRING"
            logical = declared or logical
            item = {"name": name, "logical_type": logical, "null_count": null_count,
                    "null_rate": round(null_count / count, 4) if count else 0,
                    "distinct_count": distinct, "distinct_rate": distinct / nonnull if nonnull else 0,
                    "uniqueness_ratio": singletons / nonnull if nonnull else 0,
                    "semantic_tag": identifier, "inference_method": "EXPLICIT_OVERRIDE" if override else
                    "SOURCE_SCHEMA_V1" if native_logical and identifier != "IDENTIFIER" else "OBSERVED_V2"}
            if nonnull and logical in {"DECIMAL", "INT64"}:
                item["parse_error_count"] = nonnull - decimal_count
                if decimal_count:
                    with localcontext(Context(prec=max(28, 2 * decimal_precision + len(str(decimal_count)) + 10))):
                        item.update(min=str(decimal_min), max=str(decimal_max), mean=str(decimal_total / decimal_count))
            elif nonnull and logical in {"DATE", "TIMESTAMP"}:
                valid_count = date_count if logical == "DATE" else timestamp_count
                smallest, largest = (date_min, date_max) if logical == "DATE" else (timestamp_min, timestamp_max)
                item["parse_error_count"] = nonnull - valid_count
                if smallest is not None and largest is not None:
                    item.update(min=smallest.isoformat(), max=largest.isoformat())
            elif nonnull:
                item.update(min_length=min_length, max_length=max_length)
            # Python's Unicode length bounds UTF-8 by four bytes per code point.
            # Include every logical type: a decimal/date is still canonical text.
            observed_record_bytes_upper_bound += 32 + 4 * (max_length or 0)
            profiles.append(item)
            column = {"name": name, "logical_type": logical, "nullable": null_count > 0, "semantic_tag": identifier}
            if native.get(name) and native[name] not in {"String", "JSON scalar"}:
                column["native_type"] = native[name]
            schema.append(column)
    signature = [{"name": column["name"], "logical_type": column["logical_type"]} for column in schema]
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    profile = {"row_count": count, "column_count": len(names), "columns": profiles,
               "profiling_policy": "OBSERVED_EXACT_V2", "metric_method": "EXACT_OBSERVED",
               "metric_definition_version": 2, "null_policy": "NULL_EXCLUDED_EMPTY_PRESERVED",
               "observed_record_bytes_upper_bound": observed_record_bytes_upper_bound}
    return schema, profile, digest


def publish_materialized_version(db: Session, dataset: Dataset, paths: list[Path], *,
                                 filename: str, actor: Actor | str = "Sistema", source_type: str = "INTAKE_OUTPUT",
                                 parent_version_id: str | None = None, source_run_id: str | None = None,
                                 column_overrides: dict | None = None, native_types: dict | None = None,
                                 metadata: dict | None = None, original: Path | None = None,
                                 original_media_type: str | None = None,
                                 limits: AcquisitionLimits | None = None,
                                 check: Callable[[], None] | None = None,
                                 profiled: tuple[list, dict, str] | None = None) -> DatasetVersion:
    """Caller holds its Run/Acquisition+Job fence; this helper never commits.

    Profile before entering the final DB publication transaction when possible.
    `profiled` is exclusively an internal result of profile_paths, not an HTTP
    parameter. Every part is verified again by StorageProvider before metadata.
    """
    from .db import utcnow
    from .services import audit

    checkpoint = check or (lambda: None)
    schema, profile, schema_hash = profiled or profile_paths(paths, column_overrides=column_overrides,
                                                            native_types=native_types, limits=limits, check=checkpoint)
    checkpoint()
    db.scalar(select(Dataset).where(Dataset.id == dataset.id).with_for_update())
    canonical = storage_provider.put_dataset(db, paths, "INTAKE_ACCEPTED" if source_type == "INTAKE_OUTPUT" else "CANONICAL_PARQUET",
                                              dataset.organization_id, metadata=metadata)
    original_artifact = storage_provider.put_file(db, original, "ORIGINAL_UPLOAD", dataset.organization_id,
                                                   filename, media_type=original_media_type) if original else None
    if original_artifact:
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "ARTIFACT", canonical.id, "ARTIFACT", original_artifact.id)
    ordinal = (db.scalar(select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset.id)) or 0) + 1
    source_artifact = original_artifact or canonical
    ingestion = {"reader": {"key": (metadata or {}).get("source_format", source_type), "version": 2},
                 "storage_contract_version": 1, "dataset_descriptor_version": 1, "native_schema": native_types or {},
                 **(metadata or {})}
    descriptor = json.loads(storage_provider.materialize(canonical).read_text(encoding="utf-8"))
    ingestion["canonical_size_bytes"] = descriptor["data_size_bytes"]
    if descriptor["row_count"] != profile["row_count"]:
        raise ArtifactIntegrityError("DATASET_PROFILE_MISMATCH: Las partes no coinciden con la población perfilada.")
    physical_schema = pl.read_parquet_schema(paths[0])
    if RECORD_NUMBER_COLUMN in physical_schema:
        ingestion["record_number_column"] = RECORD_NUMBER_COLUMN
    profile = {**profile, "row_numbering": ingestion.get("row_numbering", "RECORD_NUMBER")}
    version = DatasetVersion(id=uid(), organization_id=dataset.organization_id, dataset_id=dataset.id,
        version=ordinal, filename=filename, source_type=source_type, sha256=source_artifact.sha256,
        schema_hash=schema_hash, size_bytes=source_artifact.size_bytes, row_count=profile["row_count"],
        column_count=len(schema), profile_status="READY", original_path=original_artifact.path if original_artifact else "",
        canonical_path=canonical.path, schema_json=schema, profile=profile, ingestion_metadata=ingestion,
        original_artifact_id=original_artifact.id if original_artifact else None, canonical_artifact_id=canonical.id,
        source_run_id=source_run_id, parent_version_id=parent_version_id, created_at=utcnow())
    db.add(version)
    db.flush()
    if parent_version_id:
        link_artifact(db, dataset.organization_id, "DERIVED_FROM", "DATASET_VERSION", version.id, "DATASET_VERSION", parent_version_id)
        if source_type == "INTAKE_OUTPUT":
            link_artifact(db, dataset.organization_id, "INTAKE_ACCEPTED_FROM", "DATASET_VERSION", version.id, "DATASET_VERSION", parent_version_id)
    if source_run_id:
        link_artifact(db, dataset.organization_id, "RUN_OUTPUT", "RUN", source_run_id, "DATASET_VERSION", version.id)
        link_artifact(db, dataset.organization_id, "RUN_OUTPUT", "RUN", source_run_id, "ARTIFACT", canonical.id)
    audit(db, "DATASET_DERIVED" if source_type == "INTAKE_OUTPUT" else "DATASET_ACQUIRED",
          "dataset_version", version.id, "Versión materializada y perfilada completamente", actor,
          dataset.organization_id, {"dataset_id": dataset.id, "row_count": version.row_count,
          "sha256": version.sha256, "source_type": source_type, "source_run_id": source_run_id})
    checkpoint()
    return version
