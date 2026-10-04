"""The real-driver probe fails closed and never permits a population collect."""

import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('connections_streaming_probe', Path(__file__).with_name('connections_streaming_probe.py'))
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


@pytest.mark.parametrize('project,host', [
    ('trackvance-certification', 'source-sqlserver'),
    ('trackvance-connections-e2e-0123456789ab', 'production.example.com'),
    ('trackvance-connections-e2e-not-unique', 'source-sqlserver'),
])
def test_probe_rejects_non_owned_project_and_host_before_driver_import(monkeypatch, project, host):
    monkeypatch.setenv('TRACKVANCE_CERTIFICATION_PROJECT', project)
    with pytest.raises(RuntimeError, match='proyecto Connections propio'):
        probe.certify_sqlserver_streaming(host, 'not-a-secret', 'not-a-secret')


def test_fingerprint_preserves_all_values_and_is_independent_of_database_row_order():
    rows = [probe.fixture_row(i) for i in range(35)]
    assert probe.fingerprint(rows) == probe.fingerprint(reversed(rows))
    assert rows[0][2] is None and rows[1][2] == ''
    assert '東京🙂' in rows[2][2] and rows[2][0] == '0000000002'
    assert probe.fingerprint(rows) != probe.fingerprint([*rows[:-1], (*rows[-1][:2], '', rows[-1][3])])


def test_spy_measures_real_fetches_and_forbids_population_fetchall():
    class Cursor:
        def execute(self, *_arguments):
            return None
        def fetchmany(self, size):
            return list(range(size))
        def close(self):
            self.closed = True
    actual, metrics = Cursor(), probe.CursorMetrics('stream_probe_0123456789ab')
    cursor = probe.CursorSpy(actual, metrics)
    cursor.execute('SELECT * FROM [source_data].[stream_probe_0123456789ab]')
    assert cursor.fetchmany(8) == list(range(8))
    assert metrics.requested == [8] and metrics.fetched == 8
    with pytest.raises(AssertionError, match='toda la población'):
        cursor.fetchall()
    with pytest.raises(AssertionError, match='ajena a lectura'):
        cursor.execute('DELETE FROM [source_data].[stream_probe_0123456789ab]')
    cursor.close()
    cursor.close()
    assert actual.closed and metrics.population_cursors_closed == 1
