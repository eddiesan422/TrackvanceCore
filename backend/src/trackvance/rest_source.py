"""GET tabular JSON acquisition; pages are bounded and never published alone."""

import time
from decimal import Decimal
from urllib.parse import urlencode

import polars as pl

from .acquisition_config import AcquisitionLimits
from .dataset_readers import DatasetReadResult
from .dataset_sources import DatabaseDatasetSource, SourceError
from .rest_transport import RestError, exact_json, json_path, request

TYPES = {"STRING": "String", "INT64": "Int64", "DECIMAL": "Decimal", "DATE": "Date", "TIMESTAMP": "Datetime", "BOOLEAN": "Boolean"}


def declared_overrides(columns, overrides):
    """Explicit REST type choices take precedence over automatic identifier tags."""
    selected = {column["name"]: {"logical_type": column["logical_type"]} for column in columns}
    for name, value in overrides.items():
        selected[name] = {**selected.get(name, {}), **value}
    return selected


class RestDatasetSource(DatabaseDatasetSource):
    source_kind = "REST"
    format_label = "REST JSON"

    def __init__(self, settings, schema_name=None, object_name=None):
        self.settings = settings
        self.options = settings.options
        fields = self.options.get("fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
            raise SourceError("REST_FIELDS_REQUIRED", "Selecciona explícitamente de 1 a 100 campos y sus tipos; no se infieren conversiones.")
        names = set()
        for field in fields:
            if (not isinstance(field, dict) or set(field) - {"name", "path", "logical_type", "nullable"}
                    or not isinstance(field.get("name"), str) or not field["name"].strip()
                    or len(field["name"]) > 128 or field["name"].startswith("__tv_")
                    or field["name"] in names or field.get("logical_type", "STRING") not in TYPES
                    or not isinstance(field.get("path", field["name"]), str)
                    or type(field.get("nullable", True)) is not bool):
                raise SourceError("REST_INVALID_FIELDS", "El mapping REST requiere nombres únicos, rutas de objeto y tipos admitidos.")
            names.add(field["name"])
        pagination = self.options.get("pagination", {"mode": "NONE"})
        if (not isinstance(pagination, dict) or pagination.get("mode", "NONE") not in {"NONE", "PAGE", "OFFSET", "CURSOR"}
                or set(pagination) - {"mode", "parameter", "size_parameter", "start", "next_cursor_path"}):
            raise SourceError("REST_INVALID_PAGINATION", "Configura paginación por página, desplazamiento o cursor.")
        for key in ("parameter", "size_parameter", "next_cursor_path"):
            if key in pagination and (not isinstance(pagination[key], str) or not pagination[key] or len(pagination[key]) > 128):
                raise SourceError("REST_INVALID_PAGINATION", "Los parámetros y rutas de paginación no son válidos.")
        if type(pagination.get("start", 1 if pagination.get("mode") == "PAGE" else 0)) is not int or pagination.get("start", 0) < 0:
            raise SourceError("REST_INVALID_PAGINATION", "El inicio de paginación debe ser un entero no negativo.")
        if pagination.get("mode") == "CURSOR" and not pagination.get("next_cursor_path"):
            raise SourceError("REST_INVALID_PAGINATION", "La paginación por cursor requiere la ruta del siguiente cursor.")
        if not isinstance(self.options.get("collection", ""), str):
            raise SourceError("REST_INVALID_COLLECTION", "La colección REST requiere una ruta de objeto JSON.")

    def schemas(self):
        return ["REST"]

    def objects(self, schema_name):
        return [{"name": "collection", "kind": "TABLE"}] if schema_name == "REST" else []

    def columns(self, schema_name=None, object_name=None):
        return [{"name": f["name"], "native_type": "JSON_" + f.get("logical_type", "STRING"),
                 "logical_type": f.get("logical_type", "STRING"), "nullable": f.get("nullable", True),
                 "numeric": f.get("logical_type") in {"INT64", "DECIMAL"}} for f in self.options["fields"]]

    def test(self):
        self.read({"limit": 1}, inspect=True)

    def _result(self, rows):
        fields = self.options["fields"]
        return DatasetReadResult(pl.DataFrame(rows, schema={f["name"]: pl.String for f in fields}, orient="row"),
            "REST", "REST JSON", "application/vnd.apache.parquet", "SNAPSHOT_ROW",
            native_schema={f["name"]: TYPES[f.get("logical_type", "STRING")] for f in fields},
            metadata={"source_native_schema": self.columns(), "snapshot_policy": "REST_FULL_BOUNDED_V1",
                      "snapshot_consistency": "REMOTE_PAGINATION_NOT_TRANSACTIONAL", "ordering": "REMOTE_PAGINATION"})

    def _cell(self, value, field):
        from .data_sinks import DeliveryError, convert_value
        if value is None:
            if not field.get("nullable", True):
                raise SourceError("REST_NULL_NOT_ALLOWED", "Un campo REST obligatorio contiene nulos.")
            return None
        if isinstance(value, (dict, list)):
            raise SourceError("REST_NESTED_VALUE", "Un campo seleccionado contiene un objeto o arreglo; no se expande ni convierte silenciosamente.")
        logical = field.get("logical_type", "STRING")
        if logical == "STRING":
            rendered = value if isinstance(value, str) else "true" if value is True else "false" if value is False else str(value)
        else:
            rendered = "true" if value is True else "false" if value is False else str(value)
            try:
                convert_value(rendered, {"target_type": logical, "nullable": True, "target_name": field["name"], "source_name": field["name"],
                                         "precision": 38, "scale": 18})
            except DeliveryError:
                raise SourceError("REST_TYPE_MISMATCH", "Un valor REST no cumple el tipo elegido; conserva STRING para identificadores y valores no normalizados.") from None
        if len(rendered.encode("utf-8")) > 65536:
            raise SourceError("SOURCE_SIZE_LIMIT", "Una celda REST supera el límite de 64 KiB.")
        return rendered

    def read_batches(self, *, limits=None, check=None, preview_limit=None):
        limits = limits or AcquisitionLimits.configured()
        checkpoint = check or (lambda: None)
        opts, fields = self.options, self.options["fields"]
        pagination = opts.get("pagination", {})
        mode = pagination.get("mode", "NONE")
        page_size = opts.get("page_size", 1000)
        max_rows = min(opts.get("max_rows", 5000000), limits.max_rows)
        max_bytes = min(opts.get("max_bytes", 2 * 1024**3), limits.max_observed_bytes)
        deadline = time.monotonic() + min(opts.get("total_timeout_seconds", 1800), limits.timeout_seconds)
        page = pagination.get("start", 1 if mode == "PAGE" else 0)
        cursor, seen = None, set()
        total_rows = total_bytes = observed = 0
        pending: list[list[str | None]] = []
        pending_bytes = 0
        try:
            for _ in range(opts.get("max_pages", 10000)):
                checkpoint()
                query = {}
                if mode != "NONE":
                    query[pagination.get("size_parameter", "limit" if mode != "PAGE" else "page_size")] = page_size
                if mode in {"PAGE", "OFFSET"}:
                    query[pagination.get("parameter", "page" if mode == "PAGE" else "offset")] = page
                elif mode == "CURSOR" and cursor is not None:
                    query[pagination.get("parameter", "cursor")] = cursor
                status, body = request(opts, self.settings.password, "GET", query="?" + urlencode(query) if query else "",
                                       max_bytes=min(8 * 1024**2, max_bytes - total_bytes), deadline=deadline, check=checkpoint)
                total_bytes += len(body)
                if status != 200:
                    raise SourceError("REST_READ_FAILED", "La lectura REST requiere una respuesta HTTP 200.")
                document = exact_json(body)
                collection = json_path(document, opts.get("collection", ""))
                if not isinstance(collection, list) or any(not isinstance(row, dict) for row in collection):
                    raise SourceError("REST_NOT_TABULAR", "La colección REST debe ser un arreglo de objetos JSON.")
                for row in collection:
                    values = [self._cell(json_path(row, f.get("path", f["name"])), f) for f in fields]
                    size = sum(len(v.encode()) for v in values if v is not None)
                    observed += size
                    total_rows += 1
                    if total_rows > max_rows or total_bytes > max_bytes or observed > limits.max_observed_bytes or size > limits.batch_bytes:
                        raise SourceError("SOURCE_SIZE_LIMIT", "REST supera los límites efectivos; no se publican datos parciales.")
                    if pending and (len(pending) >= limits.batch_rows or pending_bytes + size > limits.batch_bytes):
                        yield self._result(pending)
                        pending, pending_bytes = [], 0
                    pending.append(values)
                    pending_bytes += size
                    if preview_limit and total_rows >= preview_limit:
                        yield self._result(pending)
                        return
                if mode == "NONE":
                    break
                if mode == "CURSOR":
                    cursor = json_path(document, pagination["next_cursor_path"])
                    if cursor is None or cursor == "":
                        break
                    if type(cursor) not in {str, Decimal} or len(str(cursor)) > 2048 or str(cursor) in seen:
                        raise SourceError("REST_CURSOR_REPEATED", "El cursor REST no es válido o se repitió; extracción abortada.")
                    seen.add(str(cursor))
                elif not collection:
                    break
                else:
                    # Servers may cap the requested page size. Offset advances
                    # by observed records so a smaller page cannot skip rows.
                    page += 1 if mode == "PAGE" else len(collection)
            else:
                raise SourceError("REST_PAGE_LIMIT", "REST alcanzó el límite de páginas sin confirmar el fin de la colección.")
            checkpoint()
            if pending or total_rows == 0:
                yield self._result(pending)
        except RestError as error:
            raise SourceError(error.code, error.message) from None

    def read(self, options=None, *, inspect=False):
        from .config import MAX_ROWS
        from .dataset_sources import MAX_SNAPSHOT_BYTES
        if options and (not inspect or set(options) - {"limit"}):
            raise SourceError("INVALID_SOURCE_OPTIONS", "Opciones de lectura REST no admitidas.")
        limit = (options or {}).get("limit", 100) if inspect else None
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise SourceError("INVALID_SOURCE_OPTIONS", "La muestra REST admite entre 1 y 100 registros.")
        frames = list(self.read_batches(limits=AcquisitionLimits(max_rows=MAX_ROWS, max_observed_bytes=MAX_SNAPSHOT_BYTES), preview_limit=limit))
        result = frames[0]
        result.frame = pl.concat([item.frame for item in frames])
        result.row_count = None if inspect else result.frame.height
        return result
