"""Preserve failed full-population checkpoints before owned fixture cleanup."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import catalog_reports_api_cycle as api
import catalog_reports_cycle as cycle


def test_failure_retains_partial_full_population_without_private_message(tmp_path, monkeypatch):
    original = AssertionError("/private/token/path: HTTP 422, code=REPORT_TIMEOUT secret=synthetic-row")

    def failed(rows, directory, progress):
        assert rows == 1000000
        progress.result["joins"].append({"type": "INNER", "status": "PASS", "rows": 500000})
        progress.phase("CSV_DOWNLOAD", {"status": "FAILED", "error_code": "REPORT_TIMEOUT",
                                       "message": "secret", "rows": ["secret"], "decision": {"secret": 1}})
        raise original

    monkeypatch.setattr(api, "_certify", failed)
    with pytest.raises(AssertionError) as caught:
        api.certify(1000000, tmp_path)
    assert caught.value is original
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "FAIL" and result["rows_per_source"] == 1000000
    assert result["active_phase"] == "CSV_DOWNLOAD"
    assert result["error"] == {"type": "AssertionError", "code": "REPORT_TIMEOUT"}
    assert result["last_terminal"] == {"status": "FAILED", "error_code": "REPORT_TIMEOUT"}
    assert result["joins"] == [{"type": "INNER", "status": "PASS", "rows": 500000}]
    assert result["duration_seconds"] >= 0 and result["completed_at"]
    assert "secret" not in json.dumps(result) and "/private" not in json.dumps(result)
    assert not (tmp_path / "result.partial.json").exists()


@pytest.mark.parametrize("error", [AssertionError("secret rows"), KeyboardInterrupt("secret rows")])
def test_interruption_or_assertion_preserves_original_and_closed_error(tmp_path, monkeypatch, error):
    def failed(rows, directory, progress):
        progress.phase("FULL_DATASET_WAIT")
        raise error

    monkeypatch.setattr(api, "_certify", failed)
    with pytest.raises(type(error)) as caught:
        api.certify(1000000, tmp_path)
    assert caught.value is error
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["active_phase"] == "FULL_DATASET_WAIT" and result["status"] == "FAIL"
    assert result["error"]["code"] in {"CERTIFICATION_ASSERTION_FAILED", "CERTIFICATION_EXCEPTION"}
    assert "secret" not in json.dumps(result)


def test_evidence_write_error_does_not_mask_failed_assertion(tmp_path, monkeypatch):
    original = AssertionError("original private assertion")

    def failed(*arguments):
        raise original

    def unavailable(*arguments):
        raise PermissionError("private evidence path")

    monkeypatch.setattr(api, "_certify", failed)
    monkeypatch.setattr(api.CertificationProgress, "save", unavailable)
    with pytest.raises(AssertionError) as caught:
        api.certify(1000000, tmp_path)
    assert caught.value is original


def test_live_checkpoint_is_atomic_and_complete_status_is_not_invented(tmp_path, capsys):
    progress = api.CertificationProgress(1000000, tmp_path)
    progress.phase("INNER_DATASET_WAIT", {"status": ["secret"], "error_code": {"secret": 1}})
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "RUNNING" and result["last_terminal"] == {}
    assert not (tmp_path / "result.partial.json").exists()
    assert "secret" not in capsys.readouterr().out and "secret" not in json.dumps(result)


@pytest.mark.parametrize('format', ['CSV', 'XLSX'])
@pytest.mark.parametrize('damage', [None, 'short_population', 'same_count_different_content'])
def test_download_oracle_preserves_counts_and_hashes_and_still_rejects_any_mismatch(tmp_path, format, damage):
    progress = api.CertificationProgress(400000, tmp_path)
    progress.phase(format + '_FULL_ORACLE')
    expected = api.rows_hash([['synthetic-one'], ['synthetic-two']])
    actual = expected if damage is None else api.rows_hash(
        [['synthetic-one']] if damage == 'short_population' else [['synthetic-one'], ['different-two']])
    if damage is None:
        api.assert_download_oracle(progress, format, actual, expected)
    else:
        with pytest.raises(AssertionError):
            api.assert_download_oracle(progress, format, actual, expected)
    checkpoint = json.loads((tmp_path / 'result.json').read_text())
    assert checkpoint['status'] == 'RUNNING'
    assert checkpoint['active_phase'] == format + '_FULL_ORACLE'
    assert checkpoint['download_oracles'][format] == {
        'actual': actual, 'expected': expected, 'status': 'PASS' if damage is None else 'FAIL'}
    assert 'synthetic-one' not in json.dumps(checkpoint) and 'different-two' not in json.dumps(checkpoint)


@pytest.mark.parametrize('unavailable', [False, True])
def test_stream_mismatch_retains_durable_failure_without_masking_comparison(tmp_path, unavailable):
    progress = api.CertificationProgress(400000, tmp_path)
    actual, expected = api.rows_hash([]), api.rows_hash([['synthetic']])
    calls = []

    class Client:
        def call(self, path):
            calls.append(path)
            if unavailable:
                raise RuntimeError('private diagnostic unavailable')
            return {'status': 'FAILED', 'generation_status': 'FAILED', 'transmission_status': 'INTERRUPTED',
                    'error_code': 'REPORT_TIMEOUT', 'message': 'private rows', 'rows': ['private rows']}

    with pytest.raises(AssertionError):
        api.assert_download_oracle(progress, 'CSV', actual, expected, client=Client(), execution_id='synthetic-id')
    assert calls == ['/reports/executions/synthetic-id']
    checkpoint = json.loads((tmp_path / 'result.json').read_text())
    assert checkpoint['download_oracles']['CSV']['status'] == 'FAIL'
    if not unavailable:
        assert checkpoint['last_terminal'] == {'status': 'FAILED', 'generation_status': 'FAILED',
            'transmission_status': 'INTERRUPTED', 'error_code': 'REPORT_TIMEOUT'}
    assert 'private' not in json.dumps(checkpoint) and 'synthetic-id' not in json.dumps(checkpoint)


def test_parent_copies_failed_million_checkpoint_before_raising(tmp_path, monkeypatch):
    original = RuntimeError("child exit=1; private log available")
    calls = []
    checkpoint = {"status": "FAIL", "rows_per_source": 1000000, "active_phase": "CSV_DOWNLOAD",
                  "error": {"type": "AssertionError", "code": "REPORT_TIMEOUT"}, "duration_seconds": 81.5}

    def run(arguments, directory, name):
        calls.append(name)
        if name == "reports-1000000":
            raise original
        Path(arguments[-1]).write_text(json.dumps(checkpoint), encoding="utf-8")

    monkeypatch.setattr(cycle, "run", run)
    monkeypatch.setattr(cycle.guard, "compose_args", lambda *arguments: ["fixture-compose"])
    summary = {"status": "FAIL", "tiers": [{"rows_per_source": 400000, "status": "PASS"}]}
    with pytest.raises(RuntimeError) as caught:
        cycle.report_tier(tmp_path, {}, 1000000, summary)
    assert caught.value is original and calls == ["reports-1000000", "copy-1000000"]
    assert json.loads((tmp_path / "reports-1000000.json").read_text(encoding="utf-8")) == checkpoint
    assert summary["failed_stage"] == "reports-1000000"
    assert summary["failed_tier"]["diagnostics_copied"] is True
    assert summary["failed_tier"]["active_phase"] == "CSV_DOWNLOAD"
    assert summary["error"]["code"] == "REPORT_TIMEOUT" and len(summary["tiers"]) == 1
    assert summary["status"] == "FAIL"


def test_failed_copy_does_not_mask_original_child_failure(tmp_path, monkeypatch):
    original = RuntimeError("original child failure")
    calls = []

    def run(arguments, directory, name):
        calls.append(name)
        raise original if name == "reports-1000000" else RuntimeError("copy failed")

    monkeypatch.setattr(cycle, "run", run)
    monkeypatch.setattr(cycle.guard, "compose_args", lambda *arguments: ["fixture-compose"])
    summary = {"status": "FAIL", "tiers": []}
    with pytest.raises(RuntimeError) as caught:
        cycle.report_tier(tmp_path, {}, 1000000, summary)
    assert caught.value is original and calls == ["reports-1000000", "copy-1000000"]
    assert summary["failed_tier"]["diagnostics_copied"] is False
    assert summary["error"]["code"] == "CATALOG_FIXTURE_EVIDENCE_UNAVAILABLE"


@pytest.mark.parametrize("status", ["PASS", "FAIL", "RUNNING"])
def test_successful_process_still_requires_pass_checkpoint(tmp_path, monkeypatch, status):
    def run(arguments, directory, name):
        if name == "copy-1000000":
            Path(arguments[-1]).write_text(json.dumps({"status": status, "rows_per_source": 1000000}), encoding="utf-8")

    monkeypatch.setattr(cycle, "run", run)
    monkeypatch.setattr(cycle.guard, "compose_args", lambda *arguments: ["fixture-compose"])
    summary = {"status": "FAIL", "tiers": []}
    if status == "PASS":
        assert cycle.report_tier(tmp_path, {}, 1000000, summary)["status"] == "PASS"
        assert "failed_tier" not in summary
    else:
        with pytest.raises(RuntimeError, match="no terminó PASS"):
            cycle.report_tier(tmp_path, {}, 1000000, summary)
        assert summary["failed_tier"]["status"] == "FAIL" and summary["status"] == "FAIL"
