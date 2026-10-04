from datetime import UTC, datetime
from decimal import Decimal

from trackvance.api import bounded_profile_sample
from trackvance.dataset_scans import sample_paths


class Records:
    def __init__(self, rows):
        self.rows = rows
        self.visited = 0

    def head(self, limit):
        for row in self.rows[:limit]:
            self.visited += 1
            yield row


def test_presentation_sample_stops_on_encoded_utf8_budget_without_population_materialization():
    records = Records([{"text": "界" * 30}] * 100)
    sample, size, limited = bounded_profile_sample(records, byte_limit=210)
    assert len(sample) == 2 and size == 205 and limited
    assert records.visited == 3


def test_sample_accounts_for_json_escaping_and_can_omit_one_oversized_record():
    records = Records([{"text": "\u0000" * 30}])
    sample, size, limited = bounded_profile_sample(records, byte_limit=100)
    assert sample == [] and size == 2 and limited


def test_sample_preserves_native_values_and_row_limit():
    row = {"money": Decimal("0.00000000000000000001"), "when": datetime(2026, 10, 3, tzinfo=UTC)}
    records = Records([row] * 100)
    sample, size, limited = bounded_profile_sample(records)
    assert sample == [row] * 20 and size > 0 and not limited
    assert isinstance(sample[0]["money"], Decimal)
    assert records.visited == 20


def test_parquet_sample_preserves_unicode_null_empty_decimal_and_date_across_parts(tmp_path):
    from datetime import date

    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([('text', pa.string()), ('money', pa.decimal128(24, 20)),
                        ('when', pa.date32())])
    rows = [{'text': value, 'money': Decimal('0.00000000000000000001'),
             'when': date(2026, 10, 3)} for value in [None, '', '界😀é']]
    paths = [tmp_path / 'first.parquet', tmp_path / 'second.parquet']
    for path in paths:
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    sample, _, limited = bounded_profile_sample(sample_paths(paths, schema.names, limit=5))
    assert sample == (rows + rows)[:5]
    assert not limited and isinstance(sample[2]['money'], Decimal)


def test_parquet_sample_does_not_open_population_parts_after_its_row_limit(tmp_path):
    import polars as pl

    path = tmp_path / 'first.parquet'
    pl.DataFrame({'text': ['first', 'second']}).write_parquet(path)
    # The second path is intentionally unavailable. A presentation sample must
    # never scan the complete population; provider verification precedes it.
    assert list(sample_paths([path, tmp_path / 'unopened.parquet'], ['text'], limit=1)) == [{'text': 'first'}]


def test_parquet_sample_rejects_oversized_first_record_without_accumulating_rows(tmp_path):
    import polars as pl

    path = tmp_path / 'wide.parquet'
    pl.DataFrame({'text': ['界' * 30] * 30}).write_parquet(path)
    sample, size, limited = bounded_profile_sample(sample_paths([path], ['text'], byte_limit=100), byte_limit=100)
    assert sample == [] and size == 2 and limited


def test_profile_endpoint_verifies_multipart_and_preserves_global_profile_without_analytical_connection(
        authenticated, database, tmp_path, monkeypatch):
    import duckdb
    import polars as pl

    from trackvance.artifactstore import storage_provider
    from trackvance.dataset_scans import publish_materialized_version
    from trackvance.models import Artifact, Dataset, User

    paths = [tmp_path / 'first.parquet', tmp_path / 'second.parquet']
    for path in paths:
        pl.DataFrame({'id': ['A', 'B'], 'text': [None, '界😀é']}).write_parquet(path)
    with database() as db:
        dataset = Dataset(name='Verified presentation', organization_id=db.get(User, 'test-user').organization_id)
        db.add(dataset)
        db.flush()
        version = publish_materialized_version(db, dataset, paths, filename='sample.parquet')
        identifier, global_profile = version.id, version.profile
        physical_paths = storage_provider.dataset_paths(db.get(Artifact, version.canonical_artifact_id))
        db.commit()

    def analytical_connection_forbidden(*args, **kwargs):
        raise AssertionError('A presentation sample must not open an analytical connection')

    monkeypatch.setattr(duckdb, 'connect', analytical_connection_forbidden)
    response = authenticated.get(f'/api/v1/dataset-versions/{identifier}/profile')
    assert response.status_code == 200
    body = response.json()
    assert body['profile'] == global_profile and body['profile']['row_count'] == 4
    assert body['sampled_rows'] == 4 and body['sample'][1]['text'] == '界😀é'
    assert not body['sample_limited']

    # Verification covers all parts, including those outside a requested sample.
    physical_paths[-1].write_bytes(b'tampered')
    assert authenticated.get(f'/api/v1/dataset-versions/{identifier}/profile').status_code == 409
