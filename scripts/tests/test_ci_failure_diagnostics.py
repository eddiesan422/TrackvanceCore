"""Failure artifacts preserve facts without exporting child payloads or secrets."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import failure_diagnostics as diagnostics
from ci import run_suite
from ci.common import EvidenceError

SHA = '1' * 40
CI = {'run_id': '123', 'run_attempt': '2', 'job_id': 'suite-catalog-reports'}


def test_summary_copies_only_known_phase_counts_error_codes_and_terminal_states():
    raw = {'status': 'FAIL', 'failed_stage': 'reports-1000000', 'failed_tier': 1000000,
           'error': {'type': 'AssertionError', 'code': 'REPORT_MEMORY_LIMIT', 'message': 'secret'},
           'main_unchanged': True, 'env': {'TOKEN': 'secret'}, 'sql': 'secret', 'rows': [{'value': 'secret'}],
           'tiers': [{'rows_per_source': 400000, 'status': 'PASS', 'project': 'secret'}]}
    result = diagnostics.sanitize_summary(raw)
    assert result['failed_tier'] == 1000000 and result['failed_stage'] == 'reports-1000000'
    assert result['error'] == {'type': 'AssertionError', 'code': 'REPORT_MEMORY_LIMIT'}
    assert 'secret' not in json.dumps(result) and result['tiers'] == [{'status': 'PASS', 'rows_per_source': 400000}]


def test_unknown_codes_types_phases_malformed_values_and_nonfinite_times_are_dropped():
    raw = {'status': ['secret'], 'failed_stage': 'https://token@host/private', 'failed_tier': True,
           'error': {'type': ['secret'], 'code': 'REPORT_SECRET_API_KEY'}, 'duration_seconds': float('nan'),
           'joins': [{'type': ['secret'], 'status': 'PASS'}], 'failure': {'error_code': 'secret'}}
    assert diagnostics.sanitize_summary(raw) == {'joins': [], 'failure': {}}
    recursive = {}; recursive['failure'] = recursive
    assert diagnostics.sanitize_summary(recursive) == {'failure': {'failure': {}}}


def test_catalog_nested_failed_tier_and_closed_child_phase_terminal_state_are_preserved():
    value = {'status': 'FAIL', 'failed_stage': 'reports-1000000', 'failed_tier': {'rows_per_source': 1000000,
        'status': 'FAIL', 'diagnostics_copied': True, 'active_phase': 'SOURCE_C_INTAKE_ASSERT',
        'phase_started_at': '2026-10-06T01:00:00+00:00', 'last_terminal': {'status': 'FAILED',
            'decision': 'REJECTED', 'error_code': 'REPORT_MEMORY_LIMIT', 'message': 'secret'}, 'payload': 'secret'}}
    result = diagnostics.sanitize_summary(value)
    assert result['failed_tier']['active_phase'] == 'SOURCE_C_INTAKE_ASSERT'
    assert result['failed_tier']['last_terminal'] == {'status': 'FAILED', 'decision': 'REJECTED', 'error_code': 'REPORT_MEMORY_LIMIT'}
    assert 'secret' not in json.dumps(result)


def test_log_parser_keeps_verified_source_frame_but_never_messages_data_urls_or_parameters(tmp_path):
    log = tmp_path / 'private.log'
    log.write_text('Traceback (most recent call last):\n'
                   '  File "/app/scripts/tests/catalog_reports_api_cycle.py", line 262, in certify\n'
                   '    print(secret_values, SECRET_TOKEN, url)\n'
                   '  File "/home/SECRET_TOKEN/private.py", line 1, in secret_function\n'
                   'AssertionError: REPORT_MEMORY_LIMIT token=SECRET_TOKEN data=private-row\n'
                   'FAILED scripts/tests/test_ci_run_suite.py::test_failed_phase_leaves_incremental_failure_before_raising[SECRET_TOKEN] - private-row\n'
                   '{"stage":"SOURCE_APPROVED","alias":"a","rows":1000000,"token":"SECRET_TOKEN"}\n')
    facts = diagnostics.extract_log_facts(log, {'scripts/tests/catalog_reports_api_cycle.py', 'scripts/tests/test_ci_run_suite.py'})
    assert facts['frames'] == [{'file': 'scripts/tests/catalog_reports_api_cycle.py', 'line': 262, 'function': 'certify'}]
    assert facts['test_names'] == ['scripts/tests/test_ci_run_suite.py::test_failed_phase_leaves_incremental_failure_before_raising']
    assert facts['progress'] == [{'stage': 'SOURCE_APPROVED', 'alias': 'a', 'rows': 1000000}]
    assert facts['error_types'] == ['AssertionError'] and facts['error_codes'] == ['REPORT_MEMORY_LIMIT']
    encoded = json.dumps(facts)
    assert all(secret not in encoded for secret in ('SECRET_TOKEN', 'private-row', 'secret_function', 'private.py'))


def test_unknown_or_fabricated_source_function_is_not_retained(tmp_path):
    log = tmp_path / 'private.log'
    log.write_text('  File "/app/scripts/tests/catalog_reports_api_cycle.py", line 1, in secret_payload\n'
                   '  File "/outside/unknown.py", line 1, in certify\nTokenSecretError: password=secret\n')
    facts = diagnostics.extract_log_facts(log, {'scripts/tests/catalog_reports_api_cycle.py'})
    assert not facts['frames'] and not facts['error_types'] and 'secret' not in json.dumps(facts)


def test_partial_child_without_final_summary_preserves_facts_after_external_timeout(tmp_path):
    context = tmp_path / '.codex-local/v080/trackvance-v080-test-catalog-reports-012345abcdef'
    context.mkdir(parents=True)
    (context / 'isolation.json').write_text('{}')
    (context / 'reports-1000000.private.log').write_text('AssertionError: REPORT_TIMEOUT password=secret\n')
    result = diagnostics.collect_child_failure('catalog-reports', set(), root=tmp_path, sources=set())
    assert result[0]['result'] == {'status': 'FAIL', 'summary_present': False}
    assert result[1]['rows'] == 1000000 and result[1]['facts']['error_codes'] == ['REPORT_TIMEOUT']
    assert 'secret' not in json.dumps(result)
    assert diagnostics.collect_child_failure('catalog-reports', {(context / 'isolation.json').resolve()}, root=tmp_path, sources=set()) == []


def test_published_child_attachments_are_normalized_hashed_and_cannot_count_as_pass(tmp_path, monkeypatch):
    context = tmp_path / '.codex-local/v080/trackvance-v080-test-catalog-reports-012345abcdef'
    context.mkdir(parents=True)
    (context / 'isolation.json').write_text('{}')
    (context / 'result.json').write_text(json.dumps({'status': 'FAIL', 'failed_stage': 'reports-1000000',
                                                    'failed_tier': 1000000, 'token': 'secret'}))
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('catalog-reports', output, source_sha=SHA, ci=CI,
        phases=[{'name': 'catalog', 'status': 'FAIL', 'exit_code': 1, 'timed_out': False, 'duration_seconds': 5}],
        error=RuntimeError('password=secret'), phase='catalog', before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    assert record['kind'] == 'FAILURE_DIAGNOSTIC' and record['status'] == 'FAIL' and record['certifies_final'] is False
    assert not path.name.startswith('scenario-') and record['source_sha'] == SHA and record['ci'] == CI
    assert len(record['evidence']) == 1
    attached = output / 'evidence' / record['evidence'][0]['path']
    assert diagnostics.hashlib.sha256(attached.read_bytes()).hexdigest() == record['evidence'][0]['sha256']
    assert 'secret' not in path.read_text() + attached.read_text()


@pytest.mark.parametrize('field,value', [('job_id', 'secret'), ('run_id', '0'), ('run_attempt', 'secret')])
def test_failure_identity_cannot_be_relabelled_or_leak_untrusted_job_text(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: pytest.fail('Untrusted identity read sources'))
    with pytest.raises(EvidenceError, match='INVALID_FAILURE_CI_IDENTITY'):
        diagnostics.publish_failure('catalog-reports', tmp_path, source_sha=SHA, ci={**CI, field: value},
                                    phases=[], error=RuntimeError('secret'), phase='catalog', before=set())


def test_failed_execute_exception_preserves_the_terminal_phase_for_group_timing(tmp_path, monkeypatch):
    class Process:
        def wait(self, **kwargs):
            return 1
    monkeypatch.setattr(run_suite.subprocess, 'Popen', lambda *_a, **_k: Process())
    with pytest.raises(run_suite.PhaseFailed) as failed:
        run_suite.execute(['unused'], tmp_path, 'catalog', 5)
    assert failed.value.record == json.loads((tmp_path / 'catalog.json').read_text())
    assert failed.value.record['status'] == 'FAIL'


def test_failed_group_still_uploads_failure_diagnostic_and_failed_phase_timing(tmp_path, monkeypatch):
    root = tmp_path / 'root'; root.mkdir()
    monkeypatch.setattr(run_suite, 'ROOT', root)
    monkeypatch.setattr(sys, 'argv', ['run_suite.py', '--group', 'catalog-reports', '--output-dir', str(tmp_path / 'ci')])
    for key, value in {'CI_SOURCE_SHA': SHA, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                       'CI_JOB_ID': 'suite-catalog-reports'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('TRACKVANCE_CI_IMAGE_MANIFEST', raising=False)
    monkeypatch.setattr(run_suite.subprocess, 'run', lambda *_a, **_k: type('Git', (), {'stdout': SHA})())
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    monkeypatch.setattr(run_suite, 'commands_for', lambda *_a: [('catalog', ['unused'], root)])
    record = {'name': 'catalog', 'status': 'FAIL', 'exit_code': 1, 'timed_out': False, 'duration_seconds': 5}

    def failed(*_args, **_kwargs):
        raise run_suite.PhaseFailed(record)

    monkeypatch.setattr(run_suite, 'execute', failed)
    with pytest.raises(run_suite.PhaseFailed):
        run_suite.main()
    timing = json.loads((tmp_path / 'ci/diagnostics/catalog-reports/group-timing.json').read_text())
    assert timing['phases'] == [record]
    evidence = list((tmp_path / 'ci/evidence').glob('failure-*.json'))
    assert len(evidence) == 1 and json.loads(evidence[0].read_text())['status'] == 'FAIL'
    assert not list((tmp_path / 'ci/evidence').glob('scenario-*.json'))
