"""Closed, public acquisition diagnostics; parser text never becomes user data."""

import re
from dataclasses import dataclass
from typing import Any

from .processing import ProcessingError


class AcquisitionReadError(ProcessingError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code, self.message, self.details = code, message, details
        super().__init__(f"{code}: {message}")


def limit_error(code: str, name: str, maximum: int, observed: int | None = None) -> AcquisitionReadError:
    labels = {
        "data_rows": "registros de datos", "compressed_bytes": "bytes del archivo comprimido",
        "expanded_bytes": "bytes descomprimidos", "observed_bytes": "bytes de valores observados",
        "columns": "columnas", "cell_bytes": "bytes UTF-8 por celda", "record_bytes": "bytes UTF-8 por registro",
        "batch_bytes": "bytes por lote", "metadata_bytes": "bytes de metadatos",
        "entries": "miembros del paquete", "styles": "estilos", "physical_rows": "filas físicas de la hoja",
        "materialized_cells": "celdas materializadas", "temporary_bytes": "bytes de almacenamiento temporal",
    }
    details: dict[str, Any] = {"limit": name, "maximum": maximum}
    if observed is not None:
        details["observed"] = observed
    label = labels.get(name, "unidades del recurso")
    return AcquisitionReadError(code, f"La fuente supera el límite efectivo de {maximum:,} {label}. No se publicó una versión parcial.", details)


@dataclass(frozen=True)
class AcquisitionDiagnostic:
    code: str
    message: str
    details: dict[str, Any] | None = None


_LEGACY_CODES = {
    "ACQUISITION_UPLOAD_LIMIT": ("ACQUISITION_COMPRESSED_SIZE_LIMIT", "El archivo supera el límite de bytes de recepción."),
    "ACQUISITION_FORMAT_LIMIT": ("ACQUISITION_FORMAT_LIMIT", "El formato no lineal supera los límites de su ruta de adquisición."),
    "ACQUISITION_CELL_LIMIT": ("ACQUISITION_CELL_LIMIT", "Una celda supera el límite de bytes UTF-8."),
    "ACQUISITION_RECORD_LIMIT": ("ACQUISITION_RECORD_LIMIT", "Un registro supera el límite de lectura."),
    "ACQUISITION_BATCH_LIMIT": ("ACQUISITION_BATCH_LIMIT", "Un registro supera el presupuesto de bytes de un lote."),
    "ACQUISITION_SIZE_LIMIT": ("ACQUISITION_SIZE_LIMIT", "La fuente supera los límites efectivos de registros o valores observados."),
    "ACQUISITION_PARQUET_LIMIT": ("ACQUISITION_PARQUET_LIMIT", "El footer Parquet supera el presupuesto de registros o expansión."),
    "ACQUISITION_PARQUET_ROW_GROUP_LIMIT": ("ACQUISITION_PARQUET_ROW_GROUP_LIMIT", "Un grupo Parquet supera el presupuesto de descompresión."),
    "ACQUISITION_INVALID_JSONL": ("ACQUISITION_INVALID_JSONL", "Cada registro NDJSON debe ser un objeto JSON válido."),
    "ACQUISITION_JSON_SCHEMA_CONFLICT": ("ACQUISITION_JSON_SCHEMA_CONFLICT", "La estructura JSON alterna objetos y valores escalares."),
    "CANONICAL_SCHEMA_INVALID": ("CANONICAL_SCHEMA_INVALID", "El esquema materializado no es compatible con el contrato canónico."),
    "SOURCE_SCHEMA_DRIFT": ("SOURCE_SCHEMA_DRIFT", "El esquema de la fuente cambió durante la adquisición."),
    "ACQUISITION_OVERRIDE_INVALID": ("ACQUISITION_OVERRIDE_INVALID", "El tipo solicitado no es válido para la población completa."),
}


def processing_diagnostic(exc: ProcessingError) -> AcquisitionDiagnostic:
    if isinstance(exc, AcquisitionReadError):
        return AcquisitionDiagnostic(exc.code, exc.message, exc.details)
    from .dataset_readers import DatasetCellLimit, UnsupportedDatasetFormat

    if isinstance(exc, DatasetCellLimit):
        return AcquisitionDiagnostic("ACQUISITION_CELL_LIMIT", "Una celda supera el límite de 65,536 bytes UTF-8.", {"limit": "cell_bytes", "maximum": 65_536})
    if isinstance(exc, UnsupportedDatasetFormat):
        return AcquisitionDiagnostic("ACQUISITION_UNSUPPORTED_FORMAT", "El formato de archivo no está soportado para adquisición.")
    # Only explicitly recognized legacy forms are mapped. Arbitrary prefixes,
    # business values and backend paths are not trusted as public diagnostics.
    text = str(exc)
    prefix, separator, _ = text.partition(":")
    if separator and prefix in _LEGACY_CODES:
        code, message = _LEGACY_CODES[prefix]
        return AcquisitionDiagnostic(code, message)
    match = re.fullmatch(r"Este prototipo admite hasta ([\d,]+) filas por archivo\.", text)
    if match:
        maximum = int(match.group(1).replace(",", ""))
        mapped = limit_error("ACQUISITION_ROW_LIMIT", "data_rows", maximum)
        return AcquisitionDiagnostic(mapped.code, mapped.message, mapped.details)
    match = re.fullmatch(r"Este prototipo admite hasta (\d+) columnas\.", text)
    if match:
        mapped = limit_error("ACQUISITION_COLUMN_LIMIT", "columns", int(match.group(1)))
        return AcquisitionDiagnostic(mapped.code, mapped.message, mapped.details)
    if text.startswith(("No existe la hoja seleccionada", "La hoja '")):
        return AcquisitionDiagnostic("ACQUISITION_SHEET_NOT_FOUND", "La hoja seleccionada no existe en el libro.")
    return AcquisitionDiagnostic("ACQUISITION_INVALID_DATA", "La estructura o los valores de la fuente no permiten completar la adquisición.")
