import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from sqlalchemy import func, select

from trackvance import acquisition, acquisition_api, connections_api, connections_service
from trackvance.acquisition_config import AcquisitionLimits
from trackvance.acquisition_models import AcquisitionRun, AcquisitionUpload
from trackvance.artifactstore import artifact_store
from trackvance.credential_store import EncryptedFileSecretStore
from trackvance.dataset_readers import DatasetReadResult
from trackvance.dataset_sources import (
    ConnectionSettings,
    PostgreSQLDatasetSource,
    SourceError,
    SQLServerDatasetSource,
    _column,
)
from trackvance.models import DatasetVersion, Job, User


def test_raw_transfer_inspection_registration_and_persistent_state_are_separate(authenticated, database):
    payload = b'id,value\n001,10\n2,20\n'
    staged = authenticated.post('/api/v1/datasets/uploads/stage?filename=orders.csv', content=payload,
                                headers={'Content-Type': 'application/octet-stream'})
    assert staged.status_code == 201, staged.text
    received = staged.json()
    assert received['upload']['transfer_complete'] is True
    assert received['upload']['size_bytes'] == len(payload)
    assert received['inspection']['sampled'] is True and received['inspection']['sampled_rows'] == 2
    assert 'path' not in received['upload']
    with database() as db:
        assert db.scalar(select(func.count()).select_from(AcquisitionRun)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    dataset = authenticated.post('/api/v1/datasets', json={'name': 'Orders async'}).json()
    route = f'/api/v1/datasets/{dataset["id"]}/acquisitions'
    response = authenticated.post(route, json={'upload_id': received['upload']['id']}, headers={'Idempotency-Key': 'retry-http'})
    assert response.status_code == 202, response.text
    state = response.json()
    assert state['status'] == 'QUEUED' and state['output_version_id'] is None
    assert state['effective_limits']['max_rows'] > 100000
    assert authenticated.post(route, json={'upload_id': received['upload']['id']}, headers={'Idempotency-Key': 'retry-http'}).json()['id'] == state['id']
    assert acquisition.process_once('api-worker')
    complete = authenticated.get(f'/api/v1/acquisitions/{state["id"]}').json()
    assert complete['status'] == 'SUCCESS' and complete['processed_rows'] == 2
    assert complete['output_version_id']
    assert authenticated.get('/api/v1/acquisitions', params={'dataset_id': dataset['id']}).json()['total'] == 1
    version = authenticated.get(f'/api/v1/dataset-versions/{complete["output_version_id"]}/profile')
    assert version.status_code == 200, version.text
    assert version.json()['sample'] == [{'id': '001', 'value': '10'}, {'id': '2', 'value': '20'}]


def test_raw_transfer_limit_applies_before_file_registration_and_cleans_staging(authenticated, database, monkeypatch):
    monkeypatch.setenv('TRACKVANCE_ACQUISITION_MAX_UPLOAD_BYTES', '1024')
    before = set(artifact_store.location('tmp').glob('*'))
    response = authenticated.post('/api/v1/datasets/uploads/stage?filename=large.csv', content=b'id\n' + b'1\n' * 1024)
    assert response.status_code == 413, response.text
    assert response.json()['error']['code'] == 'UPLOAD_TOO_LARGE'
    assert set(artifact_store.location('tmp').glob('*')) == before
    with database() as db:
        assert db.scalar(select(func.count()).select_from(AcquisitionUpload)) == 0


def test_unknown_length_transfer_stops_consuming_stream_at_effective_limit(database, monkeypatch):
    limits = replace(AcquisitionLimits(), max_upload_bytes=1024)
    monkeypatch.setattr(acquisition_api.AcquisitionLimits, 'configured', lambda: limits)
    consumed = []
    class Request:
        def __init__(self):
            self.headers = {}
        async def stream(self):
            for index in range(100):
                consumed.append(index)
                yield b'x' * 600
    before = set(artifact_store.location('tmp').glob('*'))
    with database() as db:
        with pytest.raises(acquisition.AcquisitionOperationError) as error:
            asyncio.run(acquisition_api.stage_file(Request(), 'source.csv', db, db.get(User, 'test-user')))
        assert error.value.code == 'UPLOAD_TOO_LARGE'
    assert consumed == [0, 1]
    assert set(artifact_store.location('tmp').glob('*')) == before


def test_upload_actor_and_org_scope_are_checked_on_inspection_and_registration(authenticated, database):
    staged = authenticated.post('/api/v1/datasets/uploads/stage?filename=orders.csv', content=b'id\n1\n').json()
    with database() as db:
        upload = db.get(AcquisitionUpload, staged['upload']['id'])
        upload.organization_id = 'other-organization'
        db.commit()
    assert authenticated.get(f'/api/v1/datasets/uploads/{staged["upload"]["id"]}/inspect').status_code == 404
    dataset = authenticated.post('/api/v1/datasets', json={'name': 'Scoped'}).json()
    assert authenticated.post(f'/api/v1/datasets/{dataset["id"]}/acquisitions', json={'upload_id': staged['upload']['id']}).status_code == 404


@pytest.fixture
def sql_source(monkeypatch, tmp_path):
    secrets = EncryptedFileSecretStore(tmp_path / 'secrets', tmp_path / 'keys' / 'master.key')
    monkeypatch.setattr(connections_api, 'secret_store', secrets)
    monkeypatch.setattr(connections_service, 'secret_store', secrets)
    state = SimpleNamespace(settings=[], columns=[_column('document_id', 'varchar', False), _column('amount', 'numeric(24,8)', True)], error=None)
    class Source:
        def __init__(self, settings, schema, name):
            state.settings.append(settings)
            self.settings = settings
        def test(self):
            return None
        def objects(self, schema):
            return [{'name': 'orders', 'kind': 'VIEW'}]
        def columns(self, schema, name):
            return list(state.columns)
        def read_batches(self, *, limits, check=None):
            if state.error:
                raise state.error
            for values in [('001', '1234567890123456.12345678'), ('2', None)]:
                if check:
                    check()
                yield DatasetReadResult(pl.DataFrame({'document_id': [values[0]], 'amount': [values[1]]}, schema={'document_id': pl.String, 'amount': pl.String}),
                    self.settings.source_type, 'External DB', 'application/vnd.apache.parquet', 'SNAPSHOT_ROW',
                    native_schema={'document_id': 'String', 'amount': 'Decimal'}, metadata={'source_native_schema': list(state.columns)})
    monkeypatch.setattr(connections_service.source_registry, 'create', lambda settings, schema_name=None, object_name=None: Source(settings, schema_name, object_name))
    return state


@pytest.mark.parametrize('source_type', ['POSTGRESQL', 'SQLSERVER'])
def test_sql_registration_freezes_revision_selection_and_publication_lineage(authenticated, database, sql_source, source_type):
    created = authenticated.post('/api/v1/connections', json={'name': 'Frozen source', 'source_type': source_type,
        'host': 'example.test', 'port': 5432 if source_type == 'POSTGRESQL' else 1433, 'database': 'business',
        'username': 'reader', 'password': 'first-secret-password', 'options': {'sslmode': 'require'} if source_type == 'POSTGRESQL' else {'encryption': 'require'}})
    assert created.status_code == 201, created.text
    connection = created.json()
    url = f'/api/v1/connections/{connection["id"]}'
    body = {'name': 'SQL async', 'schema_name': 'sales', 'object_name': 'orders'}
    response = authenticated.post(url + '/acquisitions', json=body, headers={'Idempotency-Key': 'source-retry'})
    assert response.status_code == 202, response.text
    run = response.json()['acquisition']
    assert run['status'] == 'QUEUED' and run['source_snapshot']['object_kind'] == 'VIEW'
    assert run['source_snapshot']['connection_version'] == 1
    assert 'password' not in response.text and 'first-secret' not in response.text
    assert authenticated.post(url + '/acquisitions', json=body, headers={'Idempotency-Key': 'source-retry'}).json()['acquisition']['id'] == run['id']
    assert authenticated.patch(url, json={'version': 1, 'password': 'second-secret-password'}).status_code == 200
    assert acquisition.process_once('sql-worker')
    finished = authenticated.get(f'/api/v1/acquisitions/{run["id"]}').json()
    assert finished['status'] == 'SUCCESS', finished
    assert sql_source.settings[-1].password == 'first-secret-password'
    with database() as db:
        version = db.get(DatasetVersion, finished['output_version_id'])
        assert version.row_count == 2 and version.ingestion_metadata['source']['connection_version'] == 1
        assert version.profile['columns'][1]['min'] == '1234567890123456.12345678'
    refreshed = authenticated.post(f'/api/v1/datasets/{run["dataset_id"]}/acquisitions/refresh', headers={'Idempotency-Key': 'refresh-retry'})
    assert refreshed.status_code == 202, refreshed.text
    assert refreshed.json()['source_snapshot']['connection_version'] == 2
    sql_source.error = SourceError('SOURCE_TIMEOUT', 'La fuente excedió su tiempo máximo.')
    assert acquisition.process_once('sql-worker')
    failed = authenticated.get(f'/api/v1/acquisitions/{refreshed.json()["id"]}').json()
    assert failed['status'] == 'FAILED' and failed['error_code'] == 'SOURCE_TIMEOUT'
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DatasetVersion).where(DatasetVersion.dataset_id == run['dataset_id'])) == 1


@pytest.mark.parametrize('source_type', ['POSTGRESQL', 'SQLSERVER'])
def test_native_source_batches_bound_driver_fetch_and_close_on_cancellation(monkeypatch, source_type):
    settings = ConnectionSettings(source_type=source_type, host='example.test', port=5432, database='db', username='reader', password='secret')
    source = (PostgreSQLDatasetSource if source_type == 'POSTGRESQL' else SQLServerDatasetSource)(settings, 'schema', 'orders')
    connection, cursor = MagicMock(), MagicMock()
    cursor.fetchmany.side_effect = [[('001', '  José Ω  ', False)], [('2', '', False)], []]
    @contextmanager
    def connect():
        yield connection
    monkeypatch.setattr(source, '_connection', connect)
    monkeypatch.setattr(source, '_checked_columns', lambda *_: [_column('id', 'varchar', False), _column('value', 'text', True)])
    monkeypatch.setattr(source, '_select_volume', lambda *_: (cursor, True))
    batches = source.read_batches(limits=replace(AcquisitionLimits(), batch_rows=1, batch_bytes=65536))
    assert next(batches).frame.row(0) == ('001', '  José Ω  ')
    batches.close()
    cursor.close.assert_called_once()
    assert all(call.args[0] == 1 for call in cursor.fetchmany.call_args_list)


def test_source_guard_queries_never_send_arbitrarily_large_driver_cells():
    connection = MagicMock()
    settings = ConnectionSettings(source_type='POSTGRESQL', host='example.test', port=5432, database='db', username='reader', password='secret')
    pg = PostgreSQLDatasetSource(settings)
    pg._select_volume(connection, 's"name', 't"name', [_column('c"name', 'text', True)], 1000001)
    query, arguments = connection.cursor.return_value.execute.call_args.args
    assert 'octet_length' in query.as_string() and 'THEN NULL ELSE "c""name" END' in query.as_string()
    assert arguments == (1000001,)
    ms = SQLServerDatasetSource(replace(settings, source_type='SQLSERVER'))
    ms._select_volume(connection, 's]name', 't]name', [_column('c]name', 'nvarchar(max)', True)], 1000001)
    statement = connection.cursor.return_value.execute.call_args.args[0]
    assert 'DATALENGTH' in statement and 'THEN NULL ELSE [c]]name] END' in statement
    assert 'TOP (1000001)' in statement and 'AS __tv_oversize' in statement
