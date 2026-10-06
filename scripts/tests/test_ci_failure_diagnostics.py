"""Failure artifacts preserve facts without exporting child payloads or secrets."""
import ast
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


def test_preflight_codes_match_only_trusted_source_literals_and_closed_resource_kinds():
    source = Path(__file__).resolve().parents[1] / 'ci/compose_preflight.py'
    codes = set()
    for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            argument = (node.args[1] if node.func.id == 'require' and len(node.args) > 1 else
                        node.args[0] if node.func.id == 'ComposePreflightError' and node.args else None)
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                codes.add(argument.value)
    assert diagnostics.PREFLIGHT_CODES == codes | {'SHARED_VOLUMES', 'SHARED_NETWORKS'}


def download_oracle(rows=100000, **changes):
    return {'actual': {'rows': rows, 'sha256': 'a' * 64}, 'expected': {'rows': rows, 'sha256': 'a' * 64},
            'status': 'PASS', **changes}


@pytest.mark.parametrize('format,rows', [('CSV', 100000), ('XLSX', 50000)])
def test_download_oracle_keeps_only_complete_count_checksum_pass_or_actual_mismatch(tmp_path, format, rows):
    passed = download_oracle(rows)
    failed = download_oracle(rows, actual={'rows': 1000000, 'sha256': 'b' * 64}, status='FAIL')
    assert diagnostics.sanitize_summary({'download_oracles': {format: passed}})['download_oracles'] == {format: passed}
    assert diagnostics.sanitize_summary({'download_oracles': {format: failed}})['download_oracles'] == {format: failed}
    nested = diagnostics.sanitize_summary({'tiers': [{'rows_per_source': 400000, 'download_oracles': {format: failed}}]})
    assert nested['tiers'][0]['download_oracles'][format] == failed


@pytest.mark.parametrize('oracle', [
    {'actual': {'rows': 1, 'sha256': 'a' * 64}, 'status': 'PASS'},
    download_oracle(status='SECRET_TOKEN'), download_oracle(message='PRIVATE_ROW'),
    download_oracle(actual={'rows': True, 'sha256': 'a' * 64}),
    download_oracle(actual={'rows': -1, 'sha256': 'a' * 64}),
    download_oracle(actual={'rows': 1000001, 'sha256': 'a' * 64}),
    download_oracle(actual={'rows': 1.0, 'sha256': 'a' * 64}),
    download_oracle(actual={'rows': 1, 'sha256': 'A' * 64}),
    download_oracle(actual={'rows': 1, 'sha256': 'SECRET_TOKEN'}),
    download_oracle(actual={'rows': 1, 'sha256': 'a' * 64, 'query': 'PRIVATE_ROW'}),
    download_oracle(expected={'rows': 100001, 'sha256': 'a' * 64}),
    download_oracle(expected={'rows': False, 'sha256': 'a' * 64}),
    download_oracle(actual={'rows': 1, 'sha256': 'b' * 64}, status='PASS'),
    download_oracle(status='FAIL'),
])
def test_malformed_partial_or_falsely_labelled_download_oracle_is_dropped(oracle):
    result = diagnostics.sanitize_summary({'download_oracles': {'CSV': oracle}, 'message': 'SECRET_TOKEN'})
    assert result == {} and all(secret not in json.dumps(result) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW'))


def test_download_oracle_roles_and_role_limits_are_closed():
    result = diagnostics.sanitize_summary({'download_oracles': {'SECRET_TOKEN': download_oracle(),
        'CSV': download_oracle(), 'XLSX': download_oracle(50001)}})
    assert result == {'download_oracles': {'CSV': download_oracle()}}
    assert diagnostics.sanitize_download_oracles(['PRIVATE_ROW']) == {}


def test_download_oracle_failure_attachment_is_hashed_without_row_values(tmp_path, monkeypatch):
    context = tmp_path / '.codex-local/v080/trackvance-v080-test-catalog-reports-012345abcdef'
    context.mkdir(parents=True)
    (context / 'isolation.json').write_text('{}')
    oracle = download_oracle(actual={'rows': 1, 'sha256': 'b' * 64}, status='FAIL')
    (context / 'result.json').write_text(json.dumps({'status': 'FAIL', 'download_oracles': {'CSV': oracle},
        'query': 'SECRET_TOKEN', 'rows': ['PRIVATE_ROW']}))
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('catalog-reports', output, source_sha=SHA, ci=CI, phases=[],
        error=RuntimeError('SECRET_TOKEN'), phase='catalog', before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    attached = output / 'evidence' / record['evidence'][0]['path']
    assert json.loads(attached.read_text())['result']['download_oracles'] == {'CSV': oracle}
    assert diagnostics.hashlib.sha256(attached.read_bytes()).hexdigest() == record['evidence'][0]['sha256']
    assert record['status'] == 'FAIL' and record['certifies_final'] is False
    assert all(secret not in attached.read_text() for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'query'))


@pytest.mark.parametrize('prefix', ['', 'ci.compose_preflight.', 'scripts.ci.compose_preflight.'])
def test_preflight_qualified_error_keeps_known_code_and_source_frame_without_values(tmp_path, prefix):
    log = tmp_path / 'private.log'
    log.write_text('  File "/app/scripts/ci/compose_preflight.py", line 88, in _environment\n'
                   + prefix + 'ComposePreflightError: LIVE_CREDENTIAL_INHERITANCE password=SECRET_TOKEN row=PRIVATE_ROW\n'
                   + prefix + 'ComposePreflightError: unknown-private-code-SECRET_TOKEN\n')
    facts = diagnostics.extract_log_facts(log, {'scripts/ci/compose_preflight.py'})
    assert facts['error_types'] == ['ComposePreflightError']
    assert facts['error_codes'] == ['LIVE_CREDENTIAL_INHERITANCE']
    assert facts['frames'] == [{'file': 'scripts/ci/compose_preflight.py', 'line': 88, 'function': '_environment'}]
    assert all(secret not in json.dumps(facts) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'password', 'unknown-private-code'))


def test_preflight_unknown_module_or_code_cannot_create_diagnostic_code(tmp_path):
    log = tmp_path / 'private.log'
    log.write_text('secret_module.ComposePreflightError: APP_PROJECT_MARKER\n'
                   'ci.compose_preflight.ComposePreflightError: SECRET_TOKEN\n')
    facts = diagnostics.extract_log_facts(log, set())
    assert facts['error_types'] == ['ComposePreflightError'] and facts['error_codes'] == []
    assert 'SECRET_TOKEN' not in json.dumps(facts) and 'secret_module' not in json.dumps(facts)


@pytest.mark.parametrize('group,phase', [('compose-critical', 'clean-demo'), ('identity-sso', 'identity'),
    ('connections', 'connections'), ('backup-restore', 'native'), ('async-volume-100', 'volume'),
    ('catalog-reports', 'catalog')])
def test_phase_log_preflight_facts_survive_for_every_group_without_child_summary(tmp_path, monkeypatch, group, phase):
    output = tmp_path / 'ci'
    log = output / 'diagnostics' / group / (phase + '.private.log')
    log.parent.mkdir(parents=True)
    log.write_text('  File "/app/scripts/ci/compose_preflight.py", line 88, in _environment\n'
                   'ci.compose_preflight.ComposePreflightError: APP_PROJECT_MARKER env=SECRET_TOKEN\n')
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: {'scripts/ci/compose_preflight.py'})
    path = diagnostics.publish_failure(group, output, source_sha=SHA, ci={**CI, 'job_id': 'suite-' + group},
        phases=[{'name': phase, 'status': 'FAIL', 'exit_code': 1, 'timed_out': False}],
        error=RuntimeError('SECRET_TOKEN'), phase=phase, before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    assert record['status'] == 'FAIL' and record['certifies_final'] is False
    assert record['log_facts']['error_types'] == ['ComposePreflightError']
    assert record['log_facts']['error_codes'] == ['APP_PROJECT_MARKER']
    assert record['log_facts']['frames'] and record['evidence'] == []
    assert 'SECRET_TOKEN' not in path.read_text() and not list((output / 'evidence').glob('scenario-*.json'))


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


def recovery_result(root, profile='native', **changes):
    suffix = '012345abcdef'
    if profile == 'native':
        source = f'trackvance-v070-test-recovery-src-{suffix}'
        target = f'trackvance-v070-test-recovery-dst-{suffix}'
        directory = root / '.codex-local/recovery' / f'{source}-to-{target}'
        version = {}
    else:
        source = f'trackvance-v070-test-{profile}-src-{suffix}'
        target = f'trackvance-v070-test-{profile}-dst-{suffix}'
        base = '.codex-local/v070' if profile == 'legacy061' else '.codex-local/recovery'
        directory = root / base / (f'legacy061-{suffix}' if profile == 'legacy061' else f'identity-legacy-{suffix}')
        version = {'source_version': {'legacy051': '0.5.1', 'legacy060': '0.6.0', 'legacy061': '0.6.1'}[profile],
                   'target_version': '0.8.0'}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'result.json'
    value = {'status': 'FAIL', 'source_project': source, 'target_project': target,
             'failed_stage': 'source_trackvance_fixtures', 'error_type': 'RecoveryCheckError',
             'error_code': 'SOURCE_CONNECTION_TEST_FAILED', 'main_inventory': 'UNCHANGED',
             'source_last_request': {'body': 'PRIVATE_ROW', 'url': 'SECRET_TOKEN'},
             'env': {'PASSWORD': 'SECRET_TOKEN'}, 'message': 'PRIVATE_ROW', **version, **changes}
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


@pytest.mark.parametrize('profile', ['native', 'legacy051', 'legacy060', 'legacy061'])
def test_native_and_all_authentic_legacy_failures_keep_closed_facts_only(tmp_path, profile):
    path = recovery_result(tmp_path, profile)
    result = diagnostics.collect_child_failure('backup-restore', set(), root=tmp_path, sources=set())
    assert len(result) == 1 and result[0]['profile'] == profile
    assert result[0]['result']['failed_stage'] == 'source_trackvance_fixtures'
    assert result[0]['result']['error_type'] == 'RecoveryCheckError'
    assert result[0]['result']['error_code'] == 'SOURCE_CONNECTION_TEST_FAILED'
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'source_last_request',
                                                    'source_project', 'PASSWORD', 'env', 'message'))
    assert diagnostics.collect_child_failure('backup-restore', {path.resolve()}, root=tmp_path, sources=set()) == []


@pytest.mark.parametrize('changes', [{'source_project': 'trackvance-certification'},
                                    {'target_project': 'SECRET_TOKEN'}, {'status': 'PASS'}])
def test_recovery_collection_rejects_unowned_or_successful_results(tmp_path, changes):
    recovery_result(tmp_path, **changes)
    assert diagnostics.collect_child_failure('backup-restore', set(), root=tmp_path, sources=set()) == []


def test_recovery_summary_unknown_phases_codes_types_categories_and_payloads_are_dropped():
    result = diagnostics.sanitize_recovery_summary({'status': 'FAIL', 'failed_stage': 'SECRET_TOKEN',
        'error_type': ['SECRET_TOKEN'], 'error_code': 'PRIVATE_ROW', 'error_category': 'SECRET_TOKEN',
        'exit_code': True, 'source_version': 'SECRET_TOKEN', 'duration_seconds': float('nan'),
        'source_last_request': {'row': 'PRIVATE_ROW'}, 'runtime_diagnostics': {'env': 'SECRET_TOKEN'}})
    assert result == {'status': 'FAIL'}
    assert diagnostics.sanitize_recovery_summary({'status': 'FAIL', 'error_type': 'RecoveryCommandError',
        'error_category': 'UNHEALTHY', 'exit_code': 1}) == {'status': 'FAIL',
            'error_type': 'RecoveryCommandError', 'error_category': 'UNHEALTHY', 'exit_code': 1}


@pytest.mark.parametrize('unsafe', ['context-link', 'result-link', 'oversized', 'malformed', 'baseline'])
def test_recovery_collection_drops_links_oversized_malformed_and_archived_results(tmp_path, monkeypatch, unsafe):
    path = recovery_result(tmp_path)
    if unsafe.endswith('-link'):
        original = Path.is_symlink
        linked = path.parent if unsafe == 'context-link' else path
        monkeypatch.setattr(Path, 'is_symlink', lambda self: self == linked or original(self))
    elif unsafe == 'oversized':
        path.write_text('x' * (diagnostics.MAX_BYTES + 1))
    elif unsafe == 'malformed':
        path.write_text('{bad-json')
    else:
        archived = path.parent / 'baseline' / 'result.json'
        archived.parent.mkdir()
        path.replace(archived)
    assert diagnostics.collect_child_failure('backup-restore', set(), root=tmp_path, sources=set()) == []


def test_recovery_phase_and_code_allowlists_cover_actual_runner_literals():
    scripts = Path(__file__).resolve().parent
    phases, codes = set(), set()
    for name, function in [('docker_backup_cycle.py', 'main'), ('identity_legacy_restore_cycle.py', 'main'),
                           ('v070_recovery.py', 'authentic_061_cycle'), ('v070_recovery.py', 'native_cycle')]:
        tree = ast.parse((scripts / name).read_text(encoding='utf-8'))
        body = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
        for node in ast.walk(body):
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'stage'
                                                    for target in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    phases.add(node.value.value)
            elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple)
                  and any(isinstance(target, ast.Tuple) and isinstance(target.elts[0], ast.Name)
                          and target.elts[0].id == 'stage' for target in node.targets)):
                phases.add(node.value.elts[0].value)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                codes.update(keyword.value.value for keyword in node.keywords if keyword.arg == 'code'
                             and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str))
                if isinstance(node.func, ast.Name) and node.func.id == 'DeliveryEvidenceError':
                    codes.add(node.args[0].value)
    assert phases <= diagnostics.RECOVERY_PHASES
    assert codes <= diagnostics.RECOVERY_CODES


def corrections_native_sidecar(root, **changes):
    context = root / '.codex-local/v070/trackvance-v070-test-corrections-recovery-012345abcdef'
    context.mkdir(parents=True)
    (context / 'isolation.json').write_text(json.dumps({'project': context.name, 'main_before': 'SECRET_TOKEN'}))
    path = context / 'native-recovery-diagnostic.json'
    value = {'schema_version': 1, 'kind': 'RECOVERY_PARTIAL_SUMMARY', 'profile': 'native',
             'summary_present': True, 'result': {'status': 'FAIL', 'failed_stage': 'fresh_restore',
                 'error_type': 'RecoveryCheckError', 'message': 'PRIVATE_ROW', 'env': 'SECRET_TOKEN'},
             'raw': 'SECRET_TOKEN', **changes}
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_corrections_recovery_preserves_closed_sidecar_even_after_child_result_removed(tmp_path):
    path = corrections_native_sidecar(tmp_path)
    result = diagnostics.collect_child_failure('corrections-recovery', set(), root=tmp_path, sources=set())
    assert result == [{'kind': 'RECOVERY_PARTIAL_SUMMARY', 'profile': 'native', 'summary_present': True,
                      'result': {'status': 'FAIL', 'failed_stage': 'fresh_restore', 'error_type': 'RecoveryCheckError'}}]
    assert all(secret not in json.dumps(result) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'main_before', 'message', 'env'))
    assert diagnostics.collect_child_failure('corrections-recovery', {path.resolve()}, root=tmp_path) == []
    assert diagnostics.collect_child_failure('corrections-recovery', {(path.parent / 'isolation.json').resolve()}, root=tmp_path) == []


@pytest.mark.parametrize('changes', [{'schema_version': True}, {'schema_version': 2}, {'profile': 'legacy061'},
                                    {'kind': 'PRIVATE_ROW'}, {'summary_present': 'SECRET_TOKEN'},
                                    {'result': {'status': 'PRIVATE_ROW'}}])
def test_corrections_recovery_rejects_malformed_sidecar_identity(tmp_path, changes):
    corrections_native_sidecar(tmp_path, **changes)
    assert diagnostics.collect_child_failure('corrections-recovery', set(), root=tmp_path) == []


@pytest.mark.parametrize('unsafe', ['context-link', 'isolation-link', 'sidecar-link', 'oversized',
                                  'malformed', 'baseline', 'main-project'])
def test_corrections_recovery_sidecar_rejects_unsafe_stale_or_unowned_sources(tmp_path, monkeypatch, unsafe):
    path = corrections_native_sidecar(tmp_path)
    if unsafe.endswith('-link'):
        linked = {'context-link': path.parent, 'isolation-link': path.parent / 'isolation.json', 'sidecar-link': path}[unsafe]
        original = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda self: self == linked or original(self))
    elif unsafe == 'oversized':
        path.write_text('x' * (diagnostics.MAX_BYTES + 1))
    elif unsafe == 'malformed':
        path.write_text('{bad-json')
    elif unsafe == 'main-project':
        (path.parent / 'isolation.json').write_text(json.dumps({'project': 'trackvance-certification'}))
    else:
        baseline = path.parent / 'baseline'
        baseline.mkdir()
        path.replace(baseline / path.name)
    assert diagnostics.collect_child_failure('corrections-recovery', set(), root=tmp_path) == []


@pytest.mark.parametrize('summary_present,status', [(False, 'FAIL'), (True, 'PASS')])
def test_corrections_native_missing_or_pass_sidecar_never_certifies_group_failure(tmp_path, monkeypatch, summary_present, status):
    corrections_native_sidecar(tmp_path, summary_present=summary_present, result={'status': status})
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('corrections-recovery', output, source_sha=SHA,
        ci={**CI, 'job_id': 'suite-corrections-recovery'}, phases=[], error=RuntimeError('SECRET_TOKEN'),
        phase='corrections-recovery', before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    assert record['status'] == 'FAIL' and record['certifies_final'] is False
    assert len(record['evidence']) == 1
    attachment = output / 'evidence' / record['evidence'][0]['path']
    child = json.loads(attachment.read_text())
    assert child['summary_present'] is summary_present and child['result'] == {'status': status}
    assert diagnostics.hashlib.sha256(attachment.read_bytes()).hexdigest() == record['evidence'][0]['sha256']
    assert 'SECRET_TOKEN' not in attachment.read_text() and not list((output / 'evidence').glob('scenario-*.json'))


def test_backup_failure_publishes_hashed_safe_child_and_cannot_certify_pass(tmp_path, monkeypatch):
    recovery_result(tmp_path)
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('backup-restore', output, source_sha=SHA,
        ci={**CI, 'job_id': 'suite-backup-restore'},
        phases=[{'name': 'native', 'status': 'FAIL', 'exit_code': 1, 'timed_out': False, 'duration_seconds': 125}],
        error=RuntimeError('SECRET_TOKEN'), phase='native', before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    assert record['status'] == 'FAIL' and record['certifies_final'] is False
    assert record['kind'] == 'FAILURE_DIAGNOSTIC' and not path.name.startswith('scenario-')
    assert len(record['evidence']) == 1 and record['evidence'][0]['kind'] == 'RECOVERY_PARTIAL_SUMMARY'
    attached = output / 'evidence' / record['evidence'][0]['path']
    assert diagnostics.hashlib.sha256(attached.read_bytes()).hexdigest() == record['evidence'][0]['sha256']
    assert all(secret not in path.read_text() + attached.read_text() for secret in ('SECRET_TOKEN', 'PRIVATE_ROW'))


def compose_failure_result(root, group, **changes):
    prefix, folder = {'compose-critical': ('trackvance-v070-test-e2e-', 'v070'),
                      'identity-sso': ('trackvance-v070-test-identity-', 'v070'),
                      'connections': ('trackvance-connections-e2e-1234-', 'connections-e2e')}[group]
    project = prefix + '012345abcdef'
    context = root / '.codex-local' / folder / project
    context.mkdir(parents=True)
    runtime = [{'service': 'report-worker', 'state': 'exited', 'health': 'unhealthy', 'exit_code': 137,
                'oom_killed': True, 'memory_limit_bytes': 268435456, 'nano_cpus': 250000000,
                'env': {'TOKEN': 'SECRET_TOKEN'}, 'probes': [{'Output': 'PRIVATE_ROW'}], 'id': 'SECRET_TOKEN'}]
    value = {'status': 'FAIL', 'project': project, 'failed_stage': 'docker', 'error_type': 'RuntimeError',
             'compose_failure': 'CONTAINER_UNHEALTHY', 'failed_exit_code': 1, 'cleanup': 'PASS',
             'main_inventory': 'UNCHANGED', 'error': 'SECRET_TOKEN PRIVATE_ROW',
             'browser_failures': [{'title': 'PRIVATE_ROW'}], 'runtime_diagnostics': runtime, **changes}
    path = context / 'result.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


@pytest.mark.parametrize('group', ['compose-critical', 'identity-sso', 'connections'])
def test_compose_identity_connections_partial_facts_exclude_all_payloads(tmp_path, group):
    path = compose_failure_result(tmp_path, group)
    results = diagnostics.collect_child_failure(group, set(), root=tmp_path, sources=set())
    assert len(results) == 1 and results[0]['profile'] == group
    summary = results[0]['result']
    assert summary['failed_stage'] == 'docker' and summary['compose_failure'] == 'CONTAINER_UNHEALTHY'
    assert summary['runtime'] == [{'service': 'report-worker', 'state': 'exited', 'health': 'unhealthy',
                                  'oom_killed': True, 'exit_code': 137, 'memory_limit_bytes': 268435456,
                                  'nano_cpus': 250000000}]
    assert all(secret not in json.dumps(results) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', '"env":', '"probes":', '"id":'))
    assert diagnostics.collect_child_failure(group, {path.resolve()}, root=tmp_path, sources=set()) == []


def test_connections_reads_only_new_safe_runtime_sidecar(tmp_path):
    path = compose_failure_result(tmp_path, 'connections', runtime_diagnostics=None)
    sidecar = path.parent / 'runtime-diagnostics.json'
    sidecar.write_text(json.dumps([{'service': 'api', 'state': 'running', 'health': 'healthy',
                                   'exit_code': 0, 'oom_killed': False, 'raw_output': 'SECRET_TOKEN'}]))
    result = diagnostics.collect_child_failure('connections', set(), root=tmp_path, sources=set())
    assert result[0]['result']['runtime'] == [{'service': 'api', 'state': 'running', 'health': 'healthy',
                                             'oom_killed': False, 'exit_code': 0}]
    old = diagnostics.collect_child_failure('connections', {sidecar.resolve()}, root=tmp_path, sources=set())
    assert 'runtime' not in old[0]['result']
    assert 'SECRET_TOKEN' not in json.dumps(result)


@pytest.mark.parametrize('changes', [{'project': 'trackvance-certification'}, {'status': 'PASS'}])
def test_compose_partial_rejects_main_or_success(tmp_path, changes):
    compose_failure_result(tmp_path, 'compose-critical', **changes)
    assert diagnostics.collect_child_failure('compose-critical', set(), root=tmp_path, sources=set()) == []


def test_compose_partial_unknown_stage_category_type_and_runtime_are_not_published(tmp_path):
    compose_failure_result(tmp_path, 'identity-sso', failed_stage='SECRET_TOKEN',
                           compose_failure='PRIVATE_ROW', error_type=['SECRET_TOKEN'],
                           runtime_diagnostics=[{'service': 'SECRET_TOKEN', 'health': 'unhealthy'}])
    result = diagnostics.collect_child_failure('identity-sso', set(), root=tmp_path, sources=set())[0]['result']
    assert all(secret not in json.dumps(result) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW'))
    assert 'failed_stage' not in result and 'compose_failure' not in result and 'error_type' not in result
    assert 'runtime' not in result


@pytest.mark.parametrize('unsafe', ['linked-context', 'linked-result', 'malformed', 'oversized', 'baseline'])
def test_compose_partial_drops_unsafe_or_baseline_sources(tmp_path, monkeypatch, unsafe):
    path = compose_failure_result(tmp_path, 'compose-critical')
    if unsafe.startswith('linked'):
        original = Path.is_symlink
        linked = path.parent if unsafe == 'linked-context' else path
        monkeypatch.setattr(Path, 'is_symlink', lambda self: self == linked or original(self))
    elif unsafe == 'malformed':
        path.write_text('{invalid-json')
    elif unsafe == 'oversized':
        path.write_text('x' * (diagnostics.MAX_BYTES + 1))
    else:
        archived = path.parent / 'baseline' / 'result.json'
        archived.parent.mkdir()
        path.replace(archived)
    assert diagnostics.collect_child_failure('compose-critical', set(), root=tmp_path, sources=set()) == []


def test_compose_partial_attachment_is_hashed_and_remains_failed_gate(tmp_path, monkeypatch):
    compose_failure_result(tmp_path, 'identity-sso')
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('identity-sso', output, source_sha=SHA,
        ci={**CI, 'job_id': 'suite-identity-sso'}, phases=[], error=RuntimeError('SECRET_TOKEN'),
        phase='identity', before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    assert record['status'] == 'FAIL' and record['certifies_final'] is False
    assert len(record['evidence']) == 1 and record['evidence'][0]['kind'] == 'COMPOSE_PARTIAL_SUMMARY'
    attachment = output / 'evidence' / record['evidence'][0]['path']
    assert diagnostics.hashlib.sha256(attachment.read_bytes()).hexdigest() == record['evidence'][0]['sha256']
    assert 'SECRET_TOKEN' not in attachment.read_text() and not list((output / 'evidence').glob('scenario-*.json'))
