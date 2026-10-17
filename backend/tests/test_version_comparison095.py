import sqlite3

import pytest

from trackvance.operations_common import OperationError
from trackvance.version_comparison import compare_rows, report_rows, scope_warnings, tagged


def schema(**columns):
    return [{"name": key, "logical_type": value} for key, value in columns.items()]


def run_comparison(tmp_path, before, after, columns, *, keys=None, after_columns=None):
    path = tmp_path / "evidence.sqlite"
    with sqlite3.connect(path) as connection:
        summary = compare_rows(connection, before, after, columns, after_columns or columns,
                               keys=keys or [])
    return summary, report_rows(path, limit=200)


def test_exact_compound_keys_before_after_and_count_reconciliation(tmp_path):
    columns = schema(tenant="STRING", id="STRING", amount="DECIMAL")
    summary, details = run_comparison(tmp_path,
        [{"tenant": "A", "id": "000001", "amount": "12345678901234567890.000001"},
         {"tenant": "B", "id": "000001", "amount": "1.0"},
         {"tenant": "C", "id": "000003", "amount": None}],
        [{"tenant": "B", "id": "000001", "amount": "1.00"},
         {"tenant": "A", "id": "000001", "amount": "12345678901234567890.000002"},
         {"tenant": "D", "id": "000004", "amount": "0"}], columns, keys=["tenant", "id"])
    assert summary["mode"] == "KEYED"
    assert summary["counts"] == {"ADDED": 1, "ABSENT": 1, "MODIFIED": 1, "UNCHANGED": 1}
    changed = next(row for row in details["items"] if row["kind"] == "MODIFIED")
    assert changed["before"]["id"] == "000001"
    assert changed["after"]["amount"] == "12345678901234567890.000002"
    assert changed["changed_columns"] == ["amount"]


@pytest.mark.parametrize("keys", [[], ["id"], ["missing"]])
def test_ambiguous_or_absent_keys_preserve_real_multiplicity(tmp_path, keys):
    summary, details = run_comparison(tmp_path,
        [{"id": "A", "value": "x"}, {"id": "A", "value": "x"}, {"id": "B", "value": "old"}],
        [{"id": "A", "value": "x"}, {"id": "B", "value": "new"}],
        schema(id="STRING", value="STRING"), keys=keys)
    assert summary["mode"] == "MULTISET"
    assert summary["counts"] == {"ADDED": 1, "ABSENT": 2, "MODIFIED": 0, "UNCHANGED": 1}
    assert details["records"] == 4
    if keys == ["id"]:
        assert any("AMBIGUOUS_KEYS" in warning for warning in summary["warnings"])


def test_null_empty_identifiers_and_schema_are_distinct(tmp_path):
    summary, _ = run_comparison(tmp_path,
        [{"id": "000001", "value": None}, {"id": "000002", "value": ""}],
        [{"id": "000001", "value": "", "extra": None}, {"id": "000002", "value": None, "extra": None}],
        schema(id="STRING", value="STRING"), keys=["id"],
        after_columns=schema(id="STRING", value="STRING", extra="STRING"))
    assert summary["counts"]["MODIFIED"] == 2
    assert summary["schema_changes"] == [{"column": "extra", "before": None, "after": "STRING", "kind": "ADDED"}]


def test_typed_equality_includes_timezone_and_exact_decimal(tmp_path):
    summary, _ = run_comparison(tmp_path,
        [{"amount": "100.000", "time": "2026-10-16T12:00:00-05:00", "flag": "true"}],
        [{"amount": "1E2", "time": "2026-10-16T17:00:00Z", "flag": "1"}],
        schema(amount="DECIMAL", time="TIMESTAMP", flag="BOOLEAN"))
    assert summary["counts"]["UNCHANGED"] == 1


def test_timestamp_never_silently_truncates_fractional_precision_or_invents_zone():
    for value in ("2026-10-16T17:00:00.1234567Z", "2026-10-16T17:00:00"):
        with pytest.raises(OperationError) as error:
            tagged(value, "TIMESTAMP")
        assert error.value.code == "COMPARISON_INVALID_VALUE"
    assert tagged("2026-10-16T17:00:00.123456Z", "TIMESTAMP") != tagged("2026-10-16T17:00:00.123457Z", "TIMESTAMP")


def test_complete_stream_limits_cancel_and_invalid_value_are_errors(tmp_path):
    with sqlite3.connect(tmp_path / "limit.sqlite") as connection, pytest.raises(OperationError, match="filas"):
        compare_rows(connection, ({"id": str(i)} for i in range(1001)), [], schema(id="STRING"),
                     schema(id="STRING"), keys=[], max_rows=1000)
    with sqlite3.connect(tmp_path / "cancel.sqlite") as connection:
        def cancel():
            raise OperationError(409, "COMPARISON_CANCELLED", "cancelled")
        with pytest.raises(OperationError, match="cancelled"):
            compare_rows(connection, [{"id": "1"}], [], schema(id="STRING"), schema(id="STRING"),
                         keys=[], checkpoint=cancel)
    with sqlite3.connect(tmp_path / "invalid.sqlite") as connection:
        with pytest.raises(OperationError) as error:
            compare_rows(connection, [{"amount": "no"}], [], schema(amount="DECIMAL"),
                         schema(amount="DECIMAL"), keys=[])
        assert error.value.code == "COMPARISON_INVALID_VALUE"


def test_pagination_filters_search_and_scope_evidence(tmp_path):
    path = tmp_path / "evidence.sqlite"
    with sqlite3.connect(path) as connection:
        compare_rows(connection, [{"id": "0001"}, {"id": "0002"}], [{"id": "0001"}, {"id": "0003"}],
                     schema(id="STRING"), schema(id="STRING"), keys=["id"])
    assert report_rows(path, kind="ADDED", search="0003")["records"] == 1
    page = report_rows(path, limit=1, offset=1)
    assert len(page["items"]) == 1 and page["total"] == 3
    assert any("INDEPENDENT_SCOPES" in warning for warning in scope_warnings(
        {"document_batch_id": "lot-A"}, {"document_batch_id": "lot-B"}))


def test_native_queue_report_and_both_source_authorizations(authenticated, database, tmp_path):
    from datetime import timedelta

    from sqlalchemy import func, select

    from trackvance.comparison_models import VersionComparison
    from trackvance.comparison_worker import process_once
    from trackvance.db import utcnow
    from trackvance.models import Dataset, Job
    from trackvance.services import create_version

    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text("id,amount\n000001,1\n000002,2\n", encoding="utf-8")
    second.write_text("id,amount\n000001,3\n000003,4\n", encoding="utf-8")
    with database() as db:
        dataset = Dataset(name="Comparison exact sources")
        db.add(dataset)
        db.flush()
        before = create_version(db, dataset, first, first.name)
        after = create_version(db, dataset, second, second.name)
        db.commit()
        dataset_id, before_id, after_id = dataset.id, before.id, after.id
        initial_count = db.scalar(select(func.count()).select_from(Dataset))
    response = authenticated.post(f"/api/v1/datasets/{dataset_id}/comparisons", json={
        "before_version_id": before_id, "after_version_id": after_id, "keys": ["id"]})
    assert response.status_code == 202, response.text
    identity = response.json()["id"]
    cancelled = authenticated.post(f"/api/v1/datasets/{dataset_id}/comparisons", json={
        "before_version_id": before_id, "after_version_id": after_id, "keys": ["id"]}).json()
    assert authenticated.post(f"/api/v1/version-comparisons/{cancelled['id']}/cancel").json()["status"] == "CANCELLED"
    # Recovery retains one logical report and rejects the lost worker's fence.
    with database() as db:
        job = db.scalar(select(Job).where(Job.comparison_id == identity))
        job.status, job.lease_owner, job.lease_until, job.attempts = "RUNNING", "lost-worker", utcnow() - timedelta(seconds=1), 1
        item = db.get(VersionComparison, identity)
        item.status, item.started_at = "RUNNING", utcnow() - timedelta(seconds=3)
        db.commit()
    assert process_once()
    assert not process_once()
    with database() as db:
        assert db.scalar(select(Job).where(Job.comparison_id == identity)).attempts == 2
    detail = authenticated.get(f"/api/v1/version-comparisons/{identity}")
    assert detail.json()["status"] == "SUCCESS", detail.text
    assert detail.json()["summary"]["counts"] == {"ADDED": 1, "ABSENT": 1, "MODIFIED": 1, "UNCHANGED": 0}
    assert authenticated.get(f"/api/v1/version-comparisons/{identity}/rows?kind=MODIFIED").json()["total"] == 1
    assert authenticated.get(f"/api/v1/version-comparisons/{identity}/download").status_code == 200
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == initial_count
    blocked = authenticated.post(f"/api/v1/catalog/datasets/{dataset_id}/blocks", json={
        "scope": "CONTENT", "reason": "Restricted comparison source"})
    assert blocked.status_code in {200, 201}, blocked.text
    for suffix in ("", "/rows", "/download"):
        denied = authenticated.get(f"/api/v1/version-comparisons/{identity}{suffix}")
        assert denied.status_code == 403, denied.text
    artifact_id = detail.json()["artifact_id"]
    assert authenticated.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 403
