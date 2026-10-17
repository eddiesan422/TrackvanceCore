"""Exact typed multiset equality over complete private staging and canonical parts."""
import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow.parquet as pq

from .dataset_scans import version_paths


def _typed(value, logical):
    if value is None:
        return None
    if logical == "INT64":
        return str(int(value))
    if logical == "DECIMAL":
        decimal = Decimal(value)
        sign, tuple_digits, exponent = decimal.as_tuple()
        if not isinstance(exponent, int):
            raise ValueError("DECIMAL no finito no es una celda tipada válida.")
        digits = list(tuple_digits)
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        return [sign if digits else 0, digits, exponent if digits else 0]
    if logical == "BOOLEAN":
        return value.lower() in {"true", "1"}
    if logical == "DATE":
        return date.fromisoformat(value).isoformat()
    if logical == "TIMESTAMP":
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else parsed.isoformat()
    return value


def unchanged(db, previous, paths, schema, work, check):
    if previous.schema_json != schema:
        return False
    columns = [c["name"] for c in schema]
    with sqlite3.connect(work / "multiset.sqlite") as index:
        index.execute("PRAGMA journal_mode=OFF")
        index.execute("PRAGMA cache_size=-4096")
        index.execute("CREATE TABLE cells (value TEXT PRIMARY KEY, balance INTEGER NOT NULL) WITHOUT ROWID")
        for population, delta in ((paths, 1), (version_paths(db, previous), -1)):
            for path in population:
                with pq.ParquetFile(path) as parquet:
                    for batch in parquet.iter_batches(batch_size=1024, columns=columns, use_threads=False):
                        check()
                        rows = batch.to_pydict()
                        normalized = [(json.dumps([_typed(rows[c["name"]][i], c["logical_type"]) for c in schema],
                            ensure_ascii=False, separators=(",", ":")), delta) for i in range(batch.num_rows)]
                        index.executemany("INSERT INTO cells(value,balance) VALUES (?,?) ON CONFLICT(value) DO UPDATE SET balance=balance+excluded.balance", normalized)
        return index.execute("SELECT 1 FROM cells WHERE balance != 0 LIMIT 1").fetchone() is None
