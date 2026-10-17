"""Bounded, editable draft proposals. No persistence, credential writes or publication."""
import re
import time
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from .rest_retry import read_request
from .rest_transport import RestError, exact_json, json_path, validate_options

MAX_FIELDS, MAX_DEPTH, MAX_CANDIDATES = 100, 8, 16


def _kind(value):
    if type(value) is bool:
        return "BOOLEAN"
    if type(value) is int:
        return "INT64" if -(2**63) <= value < 2**63 else "DECIMAL"
    if isinstance(value, Decimal):
        return "DECIMAL"
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                date.fromisoformat(value)
                return "DATE"
            except ValueError:
                pass
        if "T" in value:
            try:
                from .portable_temporal import exact_timestamp
                exact_timestamp(value)
                return "TIMESTAMP"
            except (ValueError, OverflowError):
                pass
    return "STRING"


def infer_records(records):
    warnings: set[str] = set()
    paths: set[tuple[str, ...]] = set()
    def walk(value, prefix=(), depth=0):
        if depth > MAX_DEPTH:
            warnings.add("DEPTH_LIMIT")
            return
        for key, child in value.items():
            if not key or "." in key:
                warnings.add("AMBIGUOUS_LITERAL_KEY")
                continue
            path = (*prefix, key)
            if len(paths) >= 1000 and path not in paths:
                warnings.add("FIELD_SCAN_LIMIT")
                continue
            if isinstance(child, dict) and child:
                walk(child, path, depth + 1)
            else:
                paths.add(path)
    for row in records:
        walk(row)
    fields, aliases = [], set()
    for path in sorted(paths):
        values, nulls, missing, incompatible, arrays = [], 0, 0, 0, 0
        for row in records:
            value, absent, invalid = row, False, False
            for part in path:
                if not isinstance(value, dict):
                    invalid = True
                    break
                if part not in value:
                    absent = True
                    break
                value = value[part]
            if absent:
                missing += 1
            elif invalid or isinstance(value, dict):
                incompatible += 1
            elif isinstance(value, list):
                arrays += 1
            elif value is None:
                nulls += 1
            else:
                values.append(value)
        notes = []
        if arrays:
            notes.append("ARRAY_EXCLUDED")
        if incompatible:
            notes.append("INCOMPATIBLE_STRUCTURE")
        kinds = {_kind(value) for value in values}
        if kinds <= {"INT64", "DECIMAL"} and kinds:
            logical = "DECIMAL" if "DECIMAL" in kinds else "INT64"
        elif len(kinds) == 1:
            logical = next(iter(kinds))
        else:
            logical = "STRING"
            notes.append("PROVISIONAL_ONLY_NULLS" if not kinds else "MIXED_TYPES_AS_STRING")
        def outside_decimal_domain(value):
            if type(value) is int:
                return len(str(abs(value))) > 38
            if not isinstance(value, Decimal):
                return False
            _, digits, exponent = value.as_tuple()
            return not isinstance(exponent, int) or len(digits) > 38 or len(digits) + exponent > 20 or exponent < -18
        if any(outside_decimal_domain(value) for value in values):
            logical = "STRING"
            notes.append("PRECISION_PRESERVED_AS_STRING")
        base = re.sub(r"[^\w]", "_", "_".join(path))[:110] or "field"
        if base.startswith("__tv_"):
            base = "field_" + base.lstrip("_")
        alias, suffix = base, 1
        while alias in aliases:
            suffix += 1
            alias = base + "_" + str(suffix)
        aliases.add(alias)
        fields.append({"path": ".".join(path), "name": alias, "logical_type": logical,
            "nullable": True, "null_policy": "ALLOW", "missing_policy": "ERROR",
            "value_count": len(values), "null_count": nulls, "missing_count": missing,
            "incompatible_count": incompatible, "array_count": arrays,
            "examples": [str(value)[:120] for value in values[:3]], "warnings": notes,
            "selectable": not arrays and not incompatible})
    if len(fields) > MAX_FIELDS:
        warnings.add("SELECTABLE_FIELD_LIMIT_100")
    return {"fields": fields[:1000], "warnings": sorted(warnings), "sampled_rows": len(records), "max_selected_fields": MAX_FIELDS}


def discover(options, secret, selected_collection=None, *, check=None):
    validate_options(options, secret, source=True)
    # Reuse the production pagination/path validation without requiring draft fields.
    from .rest_source import RestDatasetSource
    RestDatasetSource(SimpleNamespace(options={**options, "mapping_version": 1, "fields": [{"name": "preview"}]}, password=secret))
    deadline = time.monotonic() + min(options.get("total_timeout_seconds", 30), 30)
    pagination = options.get("pagination", {"mode": "NONE"})
    mode = pagination.get("mode", "NONE")
    if mode not in {"NONE", "PAGE", "OFFSET", "CURSOR"}:
        raise RestError("REST_INVALID_PAGINATION", "La paginación configurada no está admitida.")
    query: dict = {}
    candidates: dict[str, list[dict]] = {}
    warnings: set[str] = set()
    page = pagination.get("start", 1 if mode == "PAGE" else 0)
    cursor, seen, received = None, set(), 0
    for ordinal in range(min(options.get("max_pages", 10), 10)):
        if mode != "NONE":
            query[pagination.get("size_parameter", "page_size" if mode == "PAGE" else "limit")] = min(options.get("page_size", 100), 100)
        if mode in {"PAGE", "OFFSET"}:
            query[pagination.get("parameter", "page" if mode == "PAGE" else "offset")] = page
        if mode == "CURSOR" and cursor is not None:
            query[pagination.get("parameter", "cursor")] = cursor
        _, body = read_request(options, secret, deadline=deadline, check=check,
            query="?" + urlencode(query) if query else "", max_bytes=8 * 1024**2 - received)
        received += len(body)
        document = exact_json(body, distinguish_integers=True)
        observed: dict[str, list[dict]] = {}
        def collections(value, prefix=(), depth=0, found=observed):
            if depth > MAX_DEPTH:
                warnings.add("DEPTH_LIMIT")
                return
            if isinstance(value, list):
                if all(isinstance(row, dict) for row in value):
                    found[".".join(prefix)] = value
                return
            if isinstance(value, dict):
                for key, child in list(value.items())[:1000]:
                    if "." in key:
                        warnings.add("AMBIGUOUS_LITERAL_KEY")
                    elif len(found) < MAX_CANDIDATES:
                        collections(child, (*prefix, key), depth + 1)
                    else:
                        warnings.add("COLLECTION_LIMIT")
        collections(document)
        if selected_collection is not None:
            if selected_collection not in observed:
                raise RestError("REST_COLLECTION_MISSING", "La colección seleccionada no existe o no es tabular.")
            observed = {selected_collection: observed[selected_collection]}
        for path, rows in observed.items():
            target = candidates.setdefault(path, [])
            available = 100 - len(target)
            target.extend(rows[:available])
            if len(rows) > available:
                warnings.add("PREVIEW_LIMIT_100")
        if mode == "NONE" or not observed or all(len(rows) >= 100 for rows in candidates.values()):
            break
        if mode == "CURSOR":
            if not pagination.get("next_cursor_path"):
                raise RestError("REST_INVALID_PAGINATION", "Configura la ruta del siguiente cursor.")
            cursor = json_path(document, pagination["next_cursor_path"])
            if cursor is None or cursor == "":
                break
            if type(cursor) not in {str, int, Decimal} or str(cursor) in seen or len(str(cursor)) > 2048:
                raise RestError("REST_CURSOR_REPEATED", "El cursor se repitió o no es válido.")
            seen.add(str(cursor))
        elif not any(observed.values()):
            break
        else:
            page += 1 if mode == "PAGE" else max(map(len, observed.values()))
        if received >= 8 * 1024**2:
            warnings.add("BYTE_LIMIT")
            break
    else:
        warnings.add("DISCOVERY_PAGE_LIMIT_10")
    return {"collections": [{"path": path, **infer_records(rows)} for path, rows in sorted(candidates.items())],
            "warnings": sorted(warnings), "selection_required": len(candidates) > 1,
            "mapping_version": 2, "persisted": False}
