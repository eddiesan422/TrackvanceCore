"""Measure the real FreeTDS DatasetSource on an owned disposable SQL fixture.

Import this helper inside the isolated API container. Credentials stay in stdin
or private memory; callers publish only its aggregate report. It never starts or
stops Docker. Fixture DDL/DML uses the test administrator, whereas DatasetSource
uses the existing SELECT-only principal and its actual production cursor code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROWS = 30_000
BATCH_ROWS = 257
MODULUS = 1 << 256


def fixture_row(index):
    note = None if index % 17 == 0 else '' if index % 17 == 1 else f'  東京🙂 é e\u0301\n{index}  '
    payload = hashlib.shake_256(f'trackvance-freetds-v070:{index}'.encode()).hexdigest(1024)
    return (f'{index:010d}', format(Decimal(index % 997).scaleb(-4), '.4f'), note, payload)


def fingerprint(rows):
    count, total, xor = 0, 0, 0
    for row in rows:
        digest = int.from_bytes(hashlib.sha256(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode()).digest(), 'big')
        count, total, xor = count + 1, (total + digest) % MODULUS, xor ^ digest
    return {'rows': count, 'sha256': hashlib.sha256(f'{count}:{total:064x}:{xor:064x}'.encode()).hexdigest()}


def rss_bytes():
    try:
        for line in Path('/proc/self/status').read_text(encoding='utf-8').splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


@dataclass
class CursorMetrics:
    table: str
    requested: list[int] = field(default_factory=list)
    fetched: int = 0
    population_cursors: int = 0
    population_cursors_closed: int = 0
    connections_closed: int = 0
    select_statements: int = 0


class CursorSpy:
    def __init__(self, cursor, metrics):
        self.cursor, self.metrics, self.population, self.closed = cursor, metrics, False, False

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.close()

    def execute(self, query, *arguments):
        statement = query.strip().upper()
        if not statement.startswith(('SELECT ', 'SET TRANSACTION ISOLATION LEVEL ')):
            raise AssertionError('La cuenta del lector intentó una operación ajena a lectura/snapshot.')
        self.metrics.select_statements += statement.startswith('SELECT ')
        self.population = f'FROM [source_data].[{self.metrics.table}]' in query
        if self.population:
            self.metrics.population_cursors += 1
        return self.cursor.execute(query, *arguments)

    def fetchmany(self, size):
        result = self.cursor.fetchmany(size)
        if self.population:
            self.metrics.requested.append(size)
            self.metrics.fetched += len(result)
            assert len(result) <= size
        return result

    def fetchall(self):
        if self.population:
            raise AssertionError('DatasetSource intentó acumular toda la población.')
        return self.cursor.fetchall()

    def close(self):
        if not self.closed:
            self.cursor.close()
            if self.population:
                self.metrics.population_cursors_closed += 1
            self.closed = True


class ConnectionSpy:
    def __init__(self, connection, metrics):
        self.connection, self.metrics = connection, metrics

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def cursor(self, *arguments, **keywords):
        return CursorSpy(self.connection.cursor(*arguments, **keywords), self.metrics)

    def close(self):
        self.connection.close()
        self.metrics.connections_closed += 1


def require_fixture_scope(host):
    project = os.environ.get('TRACKVANCE_CERTIFICATION_PROJECT', '')
    if not re.fullmatch(r'trackvance-connections-e2e-(?:[a-z0-9-]+-)?[a-f0-9]{12}', project) or host != 'source-sqlserver':
        raise RuntimeError('La sonda requiere proyecto Connections propio y host source-sqlserver.')
    return project


def certify_sqlserver_streaming(host, admin_password, reader_password):
    project = require_fixture_scope(host)
    import pymssql
    from trackvance.acquisition_config import AcquisitionLimits
    from trackvance.dataset_sources import ConnectionSettings, SQLServerDatasetSource

    table = f'stream_probe_{uuid.uuid4().hex[:12]}'
    actual_connect = pymssql.connect
    admin = actual_connect(server=host, port='1433', database='trackvance_source', user='sa', password=admin_password,
        login_timeout=5, timeout=60, charset='UTF-8', tds_version='7.4', encryption='require')
    began = time.monotonic()
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE TABLE source_data.[{table}] (record_id nvarchar(20) NOT NULL PRIMARY KEY, amount decimal(18,4), note nvarchar(100), payload nvarchar(2048))')
            for start in range(0, ROWS, BATCH_ROWS):
                cursor.executemany(f'INSERT INTO source_data.[{table}] VALUES (%s,%s,%s,%s)',
                    [(row[0], Decimal(row[1]), row[2], row[3]) for row in (fixture_row(i) for i in range(start, min(start + BATCH_ROWS, ROWS)))])
            cursor.execute(f'GRANT SELECT ON source_data.[{table}] TO tv_reader')
        admin.commit()
        settings = ConnectionSettings('SQLSERVER', host, 1433, 'trackvance_source', 'tv_reader', reader_password,
                                      {'encryption': 'require', 'connect_timeout': 5, 'query_timeout': 60})
        source = SQLServerDatasetSource(settings, schema_name='source_data', object_name=table)
        limits = replace(AcquisitionLimits(), batch_rows=BATCH_ROWS, max_rows=ROWS + 1)
        metrics = CursorMetrics(table)
        def connect(**keywords):
            assert keywords['read_only'] is True and keywords['user'] == 'tv_reader'
            return ConnectionSpy(actual_connect(**keywords), metrics)
        expected = fingerprint(fixture_row(index) for index in range(ROWS))
        first_fetched, max_batch, nulls, empties = None, 0, 0, 0
        baseline_rss, peak_rss = rss_bytes(), rss_bytes()
        def records():
            nonlocal first_fetched, max_batch, peak_rss, nulls, empties
            for batch in source.read_batches(limits=limits):
                if first_fetched is None:
                    first_fetched = metrics.fetched
                    assert first_fetched < ROWS
                assert batch.metadata['driver_buffering'] == 'FREETDS_DBNEXTROW'
                assert batch.metadata['snapshot_policy'] == 'SERIALIZABLE_READ_LOCKS_V2'
                assert batch.row_numbering == 'SNAPSHOT_ROW'
                max_batch = max(max_batch, batch.frame.height)
                current = rss_bytes()
                peak_rss = max(peak_rss or 0, current or 0)
                for row in batch.frame.iter_rows():
                    assert row == fixture_row(int(row[0]))
                    nulls += row[2] is None
                    empties += row[2] == ''
                    yield row
        with patch('trackvance.dataset_sources.pymssql.connect', side_effect=connect):
            observed = fingerprint(records())
        assert observed == expected and max_batch <= BATCH_ROWS
        assert metrics.population_cursors == metrics.population_cursors_closed == metrics.connections_closed == 1
        assert metrics.requested and max(metrics.requested) <= 8
        full = {'row_count': observed['rows'], 'logical_fingerprint': observed['sha256'], 'null_count': nulls,
                'empty_string_count': empties, 'first_yield_driver_rows': first_fetched, 'max_batch_rows': max_batch,
                'fetchmany_calls': len(metrics.requested), 'max_fetchmany_rows': max(metrics.requested),
                'select_statements': metrics.select_statements,
                'cursor_closed': True, 'connection_closed': True, 'population_fetchall': False,
                'rss_baseline_bytes': baseline_rss, 'rss_peak_bytes': peak_rss}
        assert baseline_rss is not None and peak_rss - baseline_rss < 64 * 1024 * 1024
        metrics = CursorMetrics(table)
        class CooperativeCancellation(Exception):
            pass
        def cancel():
            if metrics.fetched >= BATCH_ROWS * 2:
                raise CooperativeCancellation
        with patch('trackvance.dataset_sources.pymssql.connect', side_effect=connect):
            try:
                for _batch in source.read_batches(limits=limits, check=cancel):
                    pass
            except CooperativeCancellation:
                pass
            else:
                raise AssertionError('No se ejercitó cancelación sobre el cursor real.')
        assert 0 < metrics.fetched < ROWS
        assert metrics.population_cursors == metrics.population_cursors_closed == metrics.connections_closed == 1
        return {'status': 'PASS', 'project': project, 'source_type': 'SQLSERVER', 'rows': ROWS,
            'observed_fixture_payload_bytes': ROWS * 2048, 'snapshot_policy': 'SERIALIZABLE_READ_LOCKS_V2',
            'driver_buffering': 'FREETDS_DBNEXTROW', 'full_scan': full,
            'cancellation': {'status': 'PASS', 'driver_rows': metrics.fetched, 'cursor_closed': True, 'connection_closed': True},
            'elapsed_seconds': round(time.monotonic() - began, 3), 'business_writes_by_reader': 0}
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(f'DROP TABLE IF EXISTS source_data.[{table}]')
            admin.commit()
        finally:
            admin.close()
