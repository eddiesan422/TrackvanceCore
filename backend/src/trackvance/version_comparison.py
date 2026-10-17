"""Exact full-population comparison with bounded memory and disk-backed multiplicities.

Equality uses complete tagged typed values, never a sample, incidental row order,
physical Parquet bytes or a hash alone. Unproven row correspondence is not inferred.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .operations_common import OperationError
from .portable_temporal import exact_timestamp

MAX_ROWS = 5_000_000
MAX_SPILL_BYTES = 2 * 1024**3
TIMEOUT_SECONDS = 1800


def compact(value: Any) -> str:
    def scalar(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError("Unsupported canonical scalar")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=scalar)


def _decimal(value: Any) -> list:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("non-finite decimal")
    parts = number.as_tuple()
    sign, digits, exponent = parts.sign, list(parts.digits), parts.exponent
    assert isinstance(exponent, int)
    if not any(digits):
        return [0, [0], 0]
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return [sign, digits, exponent]


def tagged(value: Any, kind: str) -> list:
    if value is None:
        return ["NULL"]
    normalized: Any
    try:
        if kind == "DECIMAL":
            normalized = _decimal(value)
        elif kind == "INT64":
            normalized = str(int(str(value)))
            if Decimal(str(value)) != Decimal(normalized):
                raise ValueError("fractional integer")
        elif kind == "BOOLEAN":
            raw = str(value).lower()
            if raw not in {"true", "false", "1", "0"}:
                raise ValueError("invalid boolean")
            normalized = raw in {"true", "1"}
        elif kind == "DATE":
            normalized = date.fromisoformat(str(value)).isoformat()
        elif kind == "TIMESTAMP":
            instant = exact_timestamp(str(value))
            normalized = instant.astimezone(UTC).isoformat()
        else:
            normalized = str(value)
    except (ValueError, InvalidOperation, OverflowError):
        raise OperationError(422, "COMPARISON_INVALID_VALUE", "Un valor no satisface su tipo lógico declarado.") from None
    return [kind, normalized]


def _schema(columns: list[dict]) -> dict[str, str]:
    return {column["name"]: column.get("logical_type", "STRING") for column in columns}


def scope_warnings(before: dict, after: dict) -> list[str]:
    left, right = before.get("population_scope"), after.get("population_scope")
    warnings = []
    document_left, document_right = before.get("document_batch_id") or before.get("batch_id"), after.get("document_batch_id") or after.get("batch_id")
    if (left != right and (left or right)) or (document_left != document_right and (document_left or document_right)):
        warnings.append("INDEPENDENT_SCOPES: Son publicaciones con alcances diferentes; un registro ausente no demuestra eliminación.")
    if not left or not right:
        warnings.append("SCOPE_NOT_DECLARED: El alcance no está declarado en ambas versiones; las ausencias describen sólo estas poblaciones.")
    return warnings


def compare_rows(connection: sqlite3.Connection, before: Iterable[dict], after: Iterable[dict],
                 before_schema: list[dict], after_schema: list[dict], *, keys: list[str],
                 checkpoint: Callable[[], None] = lambda: None,
                 progress: Callable[[int, str], None] = lambda _percent, _stage: None,
                 max_rows: int = MAX_ROWS, max_spill_bytes: int = MAX_SPILL_BYTES,
                 timeout_seconds: int = TIMEOUT_SECONDS) -> dict:
    """Populate a persistent report DB. Each detail contains a multiplicity.

    The caller owns the fresh database and its immutable promotion. Read-side
    filters/pagination query this exact evidence without rescanning source files.
    """
    started = time.monotonic()
    left, right = _schema(before_schema), _schema(after_schema)
    names = sorted(set(left) | set(right))
    schema_changes = [{"column": name, "before": left.get(name), "after": right.get(name),
                       "kind": "ADDED" if name not in left else "ABSENT" if name not in right else "TYPE_CHANGED"}
                      for name in names if left.get(name) != right.get(name)]
    key_valid = bool(keys) and len(set(keys)) == len(keys) and all(k in left and left.get(k) == right.get(k) for k in keys)
    warnings: list[str] = []
    if keys and not key_valid:
        warnings.append("KEY_SCHEMA_INCOMPATIBLE: Las claves deben existir en ambas versiones con el mismo tipo; se compara contenido y multiplicidad.")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA cache_size=-16384")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("CREATE TABLE population(side INTEGER, identity TEXT, content TEXT, raw TEXT)")
    connection.execute("CREATE TABLE details(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, multiplicity INTEGER NOT NULL, before_json TEXT, after_json TEXT, changed_columns TEXT NOT NULL)")

    def check():
        checkpoint()
        if time.monotonic() - started > timeout_seconds:
            raise OperationError(422, "COMPARISON_TIMEOUT", "La comparación excedió su tiempo máximo total.")
        size = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
        if size > max_spill_bytes:
            raise OperationError(422, "COMPARISON_BYTES", "La evidencia excedió el límite de almacenamiento temporal.")

    totals = []
    has_null_key = False
    for side, rows, schema in ((0, before, left), (1, after, right)):
        count = 0
        pending = []
        for row in rows:
            count += 1
            if count > max_rows:
                raise OperationError(422, "COMPARISON_ROWS", "La población excedió el límite de filas por versión.")
            raw = {name: row.get(name) for name in schema}
            # Column order is the frozen sorted union; type tags distinguish
            # schema changes without duplicating every column name per row.
            content = compact([tagged(row.get(name), schema[name]) if name in schema else ["MISSING_COLUMN"] for name in names])
            identity = compact([tagged(row.get(k), schema[k]) for k in keys]) if key_valid else None
            has_null_key = has_null_key or (key_valid and any(row.get(k) is None for k in keys))
            pending.append((side, identity, content, compact(raw)))
            if len(pending) >= 1000:
                connection.executemany("INSERT INTO population VALUES(?,?,?,?)", pending)
                pending.clear()
                check()
                progress(10 + side * 25, f"Leyendo población {'anterior' if side == 0 else 'posterior'}: {count} registros")
        connection.executemany("INSERT INTO population VALUES(?,?,?,?)", pending)
        check()
        totals.append(count)
    connection.execute("CREATE INDEX ix_population_identity ON population(side,identity)")
    if key_valid and (has_null_key or connection.execute("SELECT 1 FROM population GROUP BY side,identity HAVING COUNT(*)>1 LIMIT 1").fetchone()):
        key_valid = False
        warnings.append("AMBIGUOUS_KEYS: Hay claves nulas o repetidas; sólo se demuestran contenido y multiplicidad, sin inferir modificaciones.")
    progress(65, "Comparando poblaciones completas")
    counts = dict.fromkeys(("ADDED", "ABSENT", "MODIFIED", "UNCHANGED"), 0)
    detail_count = 0

    def emit(kind: str, count: int, old: str | None, new: str | None):
        nonlocal detail_count
        if not count:
            return
        old_values, new_values = json.loads(old) if old else {}, json.loads(new) if new else {}
        changed = [name for name in names if (name not in old_values) != (name not in new_values)
                   or (name in old_values and name in new_values and tagged(old_values[name], left[name]) != tagged(new_values[name], right[name]))]
        connection.execute("INSERT INTO details(kind,multiplicity,before_json,after_json,changed_columns) VALUES(?,?,?,?,?)",
                           (kind, count, old, new, compact(changed)))
        detail_count += 1
        counts[kind] += count
        if detail_count % 1000 == 0:
            check()

    if key_valid:
        cursor = connection.execute("SELECT b.content,a.content,b.raw,a.raw FROM population b LEFT JOIN population a ON a.side=1 AND a.identity=b.identity WHERE b.side=0")
        for old_content, new_content, old, new in cursor:
            emit("ABSENT" if new is None else "UNCHANGED" if old_content == new_content else "MODIFIED", 1, old, new)
        for (new,) in connection.execute("SELECT a.raw FROM population a WHERE a.side=1 AND NOT EXISTS(SELECT 1 FROM population b WHERE b.side=0 AND b.identity=a.identity)"):
            emit("ADDED", 1, None, new)
    else:
        connection.execute("CREATE TABLE groups AS SELECT content,MAX(CASE WHEN side=0 THEN raw END) old,MAX(CASE WHEN side=1 THEN raw END) new,SUM(CASE WHEN side=0 THEN 1 ELSE 0 END) n0,SUM(CASE WHEN side=1 THEN 1 ELSE 0 END) n1 FROM population GROUP BY content")
        for old, new, n0, n1 in connection.execute("SELECT old,new,n0,n1 FROM groups ORDER BY content"):
            emit("UNCHANGED", min(n0, n1), old, new)
            emit("ABSENT", max(n0 - n1, 0), old, None)
            emit("ADDED", max(n1 - n0, 0), None, new)
        connection.execute("DROP TABLE groups")
    check()
    if totals[0] != counts["ABSENT"] + counts["MODIFIED"] + counts["UNCHANGED"] or totals[1] != counts["ADDED"] + counts["MODIFIED"] + counts["UNCHANGED"]:
        raise OperationError(500, "COMPARISON_COUNT_MISMATCH", "Los conteos no reconcilian las poblaciones completas.")
    connection.execute("DROP TABLE population")
    connection.execute("CREATE INDEX ix_details_kind ON details(kind,id)")
    connection.commit()
    progress(95, "Sellando informe de evidencia")
    return {"mode": "KEYED" if key_valid else "MULTISET", "keys": keys, "counts": counts,
            "before_rows": totals[0], "after_rows": totals[1], "detail_groups": detail_count,
            "schema_changes": schema_changes, "warnings": warnings, "complete_population": True,
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def report_rows(path: Path, *, kind: str | None = None, search: str = "", offset: int = 0,
                limit: int = 50) -> dict:
    if kind is not None and kind not in {"ADDED", "ABSENT", "MODIFIED", "UNCHANGED"}:
        raise OperationError(422, "COMPARISON_FILTER", "Filtro de comparación no válido.")
    query, params = [], []
    if kind:
        query.append("kind=?")
        params.append(kind)
    if search:
        query.append("instr(lower(COALESCE(before_json,'') || ' ' || COALESCE(after_json,'')),lower(?))>0")
        params.append(search)
    where = " WHERE " + " AND ".join(query) if query else ""
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        total, records = db.execute("SELECT COUNT(*),COALESCE(SUM(multiplicity),0) FROM details" + where, params).fetchone()
        items = [{"id": identity, "kind": status, "multiplicity": count,
                  "before": json.loads(old) if old else None, "after": json.loads(new) if new else None,
                  "changed_columns": json.loads(changed)}
                 for identity, status, count, old, new, changed in db.execute("SELECT id,kind,multiplicity,before_json,after_json,changed_columns FROM details" + where + " ORDER BY id LIMIT ? OFFSET ?", [*params, limit, offset])]
    return {"items": items, "total": total, "records": records, "offset": offset, "limit": limit}
