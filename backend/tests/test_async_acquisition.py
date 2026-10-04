import hashlib
import importlib.util
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import func, select

from trackvance import acquisition
from trackvance.acquisition_config import AcquisitionLimits
from trackvance.acquisition_models import AcquisitionRun, AcquisitionUpload
from trackvance.artifactstore import (
    ArtifactIntegrityError,
    artifact_store,
    file_hash,
    storage_provider,
)
from trackvance.batch_readers import RECORD_NUMBER_COLUMN, FileBatchReader, inspect_file
from trackvance.dataset_scans import iter_version_batches, profile_paths, version_paths
from trackvance.db import utcnow
from trackvance.models import Artifact, ArtifactLink, Dataset, DatasetVersion, Job, User
from trackvance.processing import ProcessingError, profile_frame


def test_volume_fixture_semantic_hash_matches_all_real_streaming_readers(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'tests' / 'volume_cycle.py'
    spec = importlib.util.spec_from_file_location('volume_fixture_certification', script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.generate(tmp_path, 1, rows=1003)
    for item in fixture['formats'].values():
        path = Path(item['path'])
        digest, nulls, empty, rows = hashlib.sha256(), 0, 0, 0
        for batch in FileBatchReader(path, path.name):
            for row in batch.frame.iter_rows(named=True):
                digest.update(module.canonical_bytes(row))
                nulls += row['observed'] is None
                empty += row['observed'] == ''
                rows += 1
        assert digest.hexdigest() == fixture['canonical_rows_sha256']
        assert rows == 1003 and nulls == fixture['expected_observed_nulls']
        assert empty == fixture['expected_observed_empty']
    assert fixture['row_width_bytes']['min'] < fixture['row_width_bytes']['max']
    assert fixture['generation_max_batch_rows'] == 2048


def test_volume_metrics_do_not_subtract_counters_across_worker_restarts(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'tests' / 'volume_cycle.py'
    spec = importlib.util.spec_from_file_location('volume_metrics_certification', script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    measured = module.Measurements(tmp_path, {})
    measured.started = time.monotonic()
    measured.samples = [{'disk_free_bytes': 10**12, 'services': {'worker': {
        'container_id': identity, 'cpu': {'usage_usec': counter},
        'memory_events': {'oom_kill': oom}, 'memory.current': 1024}}}
        for identity, counter, oom in [('first', 10_000_000, 0), ('first', 13_000_000, 1),
                                      ('restarted', 1_000_000, 0), ('restarted', 3_000_000, 0)]]
    record = measured.report()['services']['worker']
    assert record['cpu_seconds'] == 5
    assert record['cpu_counter_resets'] == 1
    assert record['cpu_measurement_complete'] is False
    assert record['memory_events_measurement_complete'] is False
    assert record['memory_events_delta']['oom_kill'] == 1


def register_file(database, content=b'id,value\n001,10.00\n2,20.00\n3,10.00\n', *, overrides=None):
    path = storage_provider.temporary_path('.csv')
    path.write_bytes(content)
    with database() as db:
        user = db.get(User, 'test-user')
        dataset = Dataset(name='Async source ' + path.stem, organization_id=user.organization_id)
        db.add(dataset)
        db.flush()
        upload = AcquisitionUpload(organization_id=user.organization_id, user_id=user.id,
            filename='source.csv', path=str(path), sha256=file_hash(path), size_bytes=path.stat().st_size,
            source_format='CSV', expires_at=utcnow() + timedelta(hours=1))
        db.add(upload)
        db.flush()
        run = acquisition.register_upload(db, user, dataset, upload.id,
            column_overrides=overrides, idempotency_key=upload.id)
        db.commit()
        return run.id, dataset.id, upload.id


def test_csv_batches_keep_null_empty_unicode_and_physical_multiline_positions(tmp_path):
    path = tmp_path / 'source.csv'
    path.write_bytes('id,value\n001,\n2,""\n3,"  José Ω  "\n4,"first\nsecond"\n\n5,é\n'.encode())
    batches = list(FileBatchReader(path, path.name, limits=replace(AcquisitionLimits(), batch_rows=2)))
    assert [batch.frame.height for batch in batches] == [2, 2, 1]
    assert [number for batch in batches for number in batch.record_numbers] == [2, 3, 4, 5, 8]
    assert pl.concat([batch.frame for batch in batches]).to_dicts() == [
        {'id': '001', 'value': None}, {'id': '2', 'value': ''},
        {'id': '3', 'value': '  José Ω  '}, {'id': '4', 'value': 'first\nsecond'},
        {'id': '5', 'value': 'é'}]


def test_json_lines_complete_schema_union_and_native_decimal(tmp_path):
    path = tmp_path / 'source.ndjson'
    path.write_text('{"id":"001","a":1.20,"nested":{"x":"é"}}\n\n'
                    '{"id":"2","late":true,"nested":null}\n', encoding='utf-8')
    batches = list(FileBatchReader(path, path.name, limits=replace(AcquisitionLimits(), batch_rows=1)))
    assert batches[0].frame.columns == ['id', 'a', 'nested.x', 'late']
    assert batches[0].native_schema['a'] == 'Decimal'
    assert batches[0].frame['a'][0] == '1.20'
    assert [number for batch in batches for number in batch.record_numbers] == [1, 2]
    assert batches[1].frame.row(0) == ('2', None, None, 'true')


@pytest.mark.parametrize('kind', ['CSV', 'JSON_LINES', 'PARQUET'])
def test_streaming_readers_fail_the_complete_population_limit(tmp_path, kind):
    if kind == 'CSV':
        path = tmp_path / 'source.csv'
        path.write_text('id\n1\n2\n3\n')
    elif kind == 'JSON_LINES':
        path = tmp_path / 'source.ndjson'
        path.write_text('{"id":"1"}\n{"id":"2"}\n{"id":"3"}\n')
    else:
        path = tmp_path / 'source.parquet'
        pl.DataFrame({'id': ['1', '2', '3']}).write_parquet(path)
    with pytest.raises(ProcessingError, match='LIMIT'):
        list(FileBatchReader(path, path.name, limits=replace(AcquisitionLimits(), max_rows=2, batch_rows=1)))


def test_cell_and_explicit_small_format_limits_are_not_truncation(tmp_path):
    path = tmp_path / 'oversize.csv'
    path.write_text('value\n' + 'é' * 32769 + '\n', encoding='utf-8')
    with pytest.raises(ProcessingError, match='CELL_LIMIT'):
        list(FileBatchReader(path, path.name))
    path = tmp_path / 'ordinary.json'
    path.write_text('[{"value":"' + 'x' * 1024 + '"}]')
    with pytest.raises(ProcessingError, match='FORMAT_LIMIT'):
        list(FileBatchReader(path, path.name, limits=replace(AcquisitionLimits(), bounded_format_bytes=1024)))


def test_profile_is_global_exact_and_identical_to_legacy_observed_semantics(tmp_path):
    frame = pl.DataFrame({'id': ['001', '2', '3', '4', '5'],
        'amount': ['123456789012345678901234567890.123456789', '0.000000001', '-1.20', '0.000000001', None],
        'value': [None, '', '  José Ω  ', 'é', 'é'],
        'day': ['2026-01-02', '2026-01-01', None, '2026-01-02', '2026-01-03'],
        'at': ['2026-01-01T12:00:00Z', None, '2026-01-02T12:00:00-05:00', None, None]}, schema={
        'id': pl.String, 'amount': pl.String, 'value': pl.String, 'day': pl.String, 'at': pl.String})
    paths = []
    for index, part in enumerate(frame.iter_slices(2)):
        path = tmp_path / f'part-{index}.parquet'
        part.with_columns(pl.int_range(index * 2 + 1, index * 2 + part.height + 1, eager=True).alias(RECORD_NUMBER_COLUMN)).write_parquet(path)
        paths.append(path)
    schema, profile, digest = profile_paths(paths)
    bound = profile.pop('observed_record_bytes_upper_bound')
    assert (schema, profile, digest) == profile_frame(frame)
    assert bound == sum(32 + 4 * max(len(value or '') for value in frame[name]) for name in frame.columns)
    assert profile_paths(paths)[1]['columns'][1]['distinct_count'] == 3
    assert profile_paths(paths)[1]['columns'][1]['uniqueness_ratio'] == 0.5


def test_null_physical_column_profiles_fully_and_supplies_width_bound(tmp_path):
    path = tmp_path / 'all-null.parquet'
    pl.DataFrame({'nullable': [None, None]}, schema={'nullable': pl.Null}).write_parquet(path)
    schema, profile, _ = profile_paths([path])
    assert schema[0]['logical_type'] == 'STRING' and schema[0]['nullable'] is True
    assert profile['columns'][0]['null_count'] == 2
    assert profile['observed_record_bytes_upper_bound'] == 32


def test_override_validation_covers_values_beyond_inspection(tmp_path):
    path = tmp_path / 'source.csv'
    path.write_text('value\n' + '12.25\n' * 100 + 'invalid\n')
    inspection = inspect_file(path, path.name)
    assert inspection['sampled_rows'] == 100 and inspection['columns'][0]['logical_type'] == 'DECIMAL'
    parts = []
    for index, batch in enumerate(FileBatchReader(path, path.name)):
        part = tmp_path / f'part-{index}.parquet'
        batch.physical_frame().write_parquet(part)
        parts.append(part)
    with pytest.raises(ProcessingError, match='OVERRIDE_INVALID'):
        profile_paths(parts, column_overrides={'value': {'logical_type': 'DECIMAL'}})


def test_successful_job_publishes_verified_multipart_and_lineage_once(database, monkeypatch):
    monkeypatch.setenv('TRACKVANCE_ACQUISITION_BATCH_ROWS', '2')
    identity, dataset_id, upload_id = register_file(database)
    assert acquisition.process_once('owner-one')
    with database() as db:
        run = db.get(AcquisitionRun, identity)
        assert run.status == 'SUCCESS', (run.error_code, run.error_message)
        assert run.processed_rows == 3 and run.attempts == 1
        version = db.get(DatasetVersion, run.output_version_id)
        assert version.profile_status == 'READY' and version.row_count == 3
        assert len(version_paths(db, version)) == 2
        assert version.ingestion_metadata['canonical_size_bytes'] > 0
        assert version.schema_json[0]['logical_type'] == 'STRING'
        assert RECORD_NUMBER_COLUMN not in [column['name'] for column in version.schema_json]
        batches = list(iter_version_batches(db, version, include_record_numbers=True))
        assert pl.concat(batches)[RECORD_NUMBER_COLUMN].to_list() == [2, 3, 4]
        assert pl.concat(batches)['id'].to_list() == ['001', '2', '3']
        assert db.get(AcquisitionUpload, upload_id).status == 'CONSUMED'
        assert db.scalar(select(func.count()).select_from(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)) == 1
        assert db.scalar(select(func.count()).select_from(ArtifactLink).where(ArtifactLink.relation == 'DATASET_PART')) == 2
        assert not acquisition.process_once('owner-two')


def test_failed_global_override_and_cancel_never_publish_partial_version(database):
    identity, dataset_id, _ = register_file(database, b'value\n12\ninvalid\n', overrides={'value': {'logical_type': 'DECIMAL'}})
    assert acquisition.process_once('owner-one')
    with database() as db:
        run = db.get(AcquisitionRun, identity)
        assert run.status == 'FAILED' and run.error_code == 'ACQUISITION_OVERRIDE_INVALID'
        assert run.output_version_id is None
        assert db.scalar(select(func.count()).select_from(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)) == 0
    identity, _, _ = register_file(database)
    with database() as db:
        acquisition.cancel_acquisition(db, db.get(User, 'test-user'), identity)
        db.commit()
    with database() as db:
        assert db.get(AcquisitionRun, identity).status == 'CANCELLED'
        assert db.scalar(select(Job).where(Job.acquisition_id == identity)).status == 'CANCELLED'
    assert not acquisition.process_once('owner-one')


def test_same_idempotency_request_returns_the_existing_job_and_different_payload_conflicts(database):
    identity, dataset_id, upload_id = register_file(database)
    with database() as db:
        user, dataset = db.get(User, 'test-user'), db.get(Dataset, dataset_id)
        assert acquisition.register_upload(db, user, dataset, upload_id, idempotency_key=upload_id).id == identity
        with pytest.raises(acquisition.AcquisitionOperationError, match='otra adquisición'):
            acquisition.register_upload(db, user, dataset, upload_id, idempotency_key=upload_id, reader_options={'delimiter': ';'})
        assert db.scalar(select(func.count()).select_from(Job).where(Job.acquisition_id == identity)) == 1


def test_reclaimed_lease_gets_new_attempt_and_stale_owner_cannot_publish(database):
    identity, _, _ = register_file(database)
    with database() as db:
        run = db.get(AcquisitionRun, identity)
        job = db.scalar(select(Job).where(Job.acquisition_id == identity))
        run.status, run.attempt_id, run.attempts = 'RUNNING', 'old-attempt', 1
        job.status, job.attempts, job.lease_owner = 'RUNNING', 1, 'dead-owner'
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    with pytest.raises(acquisition.AcquisitionStopped, match='lease'):
        acquisition.execute_acquisition(identity, 'dead-owner')
    assert acquisition.process_once('recovery-owner')
    with database() as db:
        run = db.get(AcquisitionRun, identity)
        assert run.status == 'SUCCESS', run.error_message
        assert run.attempts == 2 and run.attempt_id != 'old-attempt'


def test_part_tamper_and_descriptor_path_escape_fail_closed(database):
    identity, _, _ = register_file(database)
    assert acquisition.process_once('owner-one')
    with database() as db:
        version = db.get(DatasetVersion, db.get(AcquisitionRun, identity).output_version_id)
        canonical = db.get(Artifact, version.canonical_artifact_id)
        paths = storage_provider.dataset_paths(canonical)
        paths[0].write_bytes(b'tampered')
        with pytest.raises(ArtifactIntegrityError):
            storage_provider.dataset_paths(canonical)


def test_upload_gc_excludes_registered_and_unexpired_uploads(database):
    _, _, upload_id = register_file(database)
    with database() as db:
        upload = db.get(AcquisitionUpload, upload_id)
        upload.expires_at = utcnow() - timedelta(days=1)
        db.commit()
        assert acquisition.cleanup_expired_uploads(db) == 0
        assert Path(upload.path).is_file()


def test_abandoned_gc_removes_only_expired_private_attempts_and_failed_upload_staging(database):
    import os
    import time
    identity, _, upload_id = register_file(database)
    with database() as db:
        run, upload = db.get(AcquisitionRun, identity), db.get(AcquisitionUpload, upload_id)
        job = db.scalar(select(Job).where(Job.acquisition_id == identity))
        work = artifact_store.create_directory('tmp', 'acquisitions', identity, 'dead-attempt')
        (work / 'private.parquet').write_bytes(b'unpublished')
        old = time.time() - 7200
        os.utime(work, (old, old))
        run.status, run.finished_at = 'FAILED', utcnow() - timedelta(hours=2)
        job.status = 'FAILED'
        upload.expires_at = utcnow() - timedelta(hours=1)
        db.commit()
        assert acquisition.cleanup_abandoned_acquisitions(db, grace_seconds=60) == 2
        db.commit()
        assert not work.exists() and not Path(upload.path).exists()
        assert upload.status == 'EXPIRED'
    identity, _, upload_id = register_file(database)
    with database() as db:
        run = db.get(AcquisitionRun, identity)
        job = db.scalar(select(Job).where(Job.acquisition_id == identity))
        run.status = job.status = 'RUNNING'
        job.lease_until = utcnow() + timedelta(minutes=5)
        db.commit()
        assert acquisition.cleanup_abandoned_acquisitions(db, grace_seconds=60) == 0
        assert Path(db.get(AcquisitionUpload, upload_id).path).exists()
