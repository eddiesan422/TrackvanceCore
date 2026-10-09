"""Failure artifacts preserve facts without exporting child payloads or secrets."""
import ast
import hashlib
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
    (context / 'isolation.json').write_text(json.dumps({'project': context.name}))
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
    (context / 'isolation.json').write_text(json.dumps({'project': context.name}))
    (context / 'reports-1000000.private.log').write_text('AssertionError: REPORT_TIMEOUT password=secret\n')
    result = diagnostics.collect_child_failure('catalog-reports', set(), root=tmp_path, sources=set())
    assert result[0]['result'] == {'status': 'FAIL', 'summary_present': False}
    assert result[1]['rows'] == 1000000 and result[1]['facts']['error_codes'] == ['REPORT_TIMEOUT']
    assert 'secret' not in json.dumps(result)
    assert diagnostics.collect_child_failure('catalog-reports', {(context / 'isolation.json').resolve()}, root=tmp_path, sources=set()) == []


def test_published_child_attachments_are_normalized_hashed_and_cannot_count_as_pass(tmp_path, monkeypatch):
    context = tmp_path / '.codex-local/v080/trackvance-v080-test-catalog-reports-012345abcdef'
    context.mkdir(parents=True)
    (context / 'isolation.json').write_text(json.dumps({'project': context.name}))
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
    monkeypatch.setattr(sys, 'argv', ['run_suite.py', '--profile', 'deep', '--group', 'catalog-reports', '--output-dir', str(tmp_path / 'ci')])
    for key, value in {'CI_SOURCE_SHA': SHA, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                       'CI_JOB_ID': 'suite-catalog-reports'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('TRACKVANCE_CI_IMAGE_MANIFEST', raising=False)
    monkeypatch.setattr(run_suite.subprocess, 'run', lambda *_a, **_k: type('Git', (), {'stdout': SHA})())
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    monkeypatch.setattr(run_suite, 'commands_for', lambda *_a, **_k: [('catalog', ['unused'], root)])
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
                   'target_version': '0.8.5'}
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


COMPOSE_BROWSER_SOURCE = 'frontend/tests-e2e/identity-sso.spec.ts'


def compose_browser_failure(root, group='identity-sso'):
    path = compose_failure_result(root, group)
    isolation = path.parent / 'isolation.json'
    isolation.write_text(json.dumps({'project': path.parent.name, 'environment': 'SECRET_TOKEN'}))
    browser = path.parent / 'browser-summary.json'
    browser.write_text(json.dumps({'status': 'FAIL', 'expected': 1, 'unexpected': 1, 'skipped': 0, 'flaky': 0,
        'duration': 1234, 'credential_privacy_probes': [{'raw': 'SECRET_TOKEN'}],
        'failures': [{'test': 'PRIVATE_ROW', 'status': 'timedOut', 'message': 'SECRET_TOKEN',
                      'location': {'file': '/checkout/' + COMPOSE_BROWSER_SOURCE, 'line': 1, 'column': 1}}]}))
    return path, isolation, browser


@pytest.mark.parametrize('group', ['compose-critical', 'identity-sso', 'connections'])
def test_compose_browser_failure_keeps_only_fresh_owned_counters_and_verified_source_location(tmp_path, group):
    compose_browser_failure(tmp_path, group)
    value = diagnostics.collect_child_failure(group, set(), root=tmp_path, sources={COMPOSE_BROWSER_SOURCE})
    browser = value[0]['result']['browser_failure']
    assert browser == {'status': 'FAIL', 'expected': 1, 'unexpected': 1, 'skipped': 0, 'flaky': 0, 'duration': 1234,
        'failures': [{'location_present': True, 'status': 'timedOut',
                      'location': {'file': COMPOSE_BROWSER_SOURCE, 'line': 1, 'column': 1}}],
        'failure_location_present': True}
    assert all(secret not in json.dumps(value) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'credential_privacy_probes'))


@pytest.mark.parametrize('damage', ['old-browser', 'old-isolation', 'linked-browser', 'junction-browser',
    'linked-isolation', 'wrong-owner', 'missing-isolation', 'browser-malformed', 'browser-oversized',
    'isolation-malformed', 'browser-pass', 'archived'])
def test_compose_browser_summary_cannot_import_old_linked_foreign_or_unbounded_evidence(tmp_path, monkeypatch, damage):
    _, isolation, browser = compose_browser_failure(tmp_path)
    before = set()
    if damage.startswith('old-'):
        before.add((browser if damage == 'old-browser' else isolation).resolve())
    elif damage.startswith('linked-'):
        linked = browser if damage == 'linked-browser' else isolation
        original = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda self: self == linked or original(self))
    elif damage == 'junction-browser':
        monkeypatch.setattr(Path, 'is_junction', lambda self: self == browser, raising=False)
    elif damage == 'wrong-owner':
        isolation.write_text(json.dumps({'project': 'trackvance-certification'}))
    elif damage == 'missing-isolation':
        isolation.unlink()
    elif damage.endswith('-malformed'):
        (browser if damage == 'browser-malformed' else isolation).write_text('{invalid')
    elif damage == 'browser-oversized':
        browser.write_text('x' * (diagnostics.MAX_BYTES + 1))
    elif damage == 'browser-pass':
        browser.write_text(json.dumps({'status': 'PASS', 'unexpected': 0}))
    else:
        archive = browser.parent / 'baseline'
        archive.mkdir()
        browser.replace(archive / browser.name)
    value = diagnostics.collect_child_failure('identity-sso', before, root=tmp_path, sources={COMPOSE_BROWSER_SOURCE})
    assert len(value) == 1 and 'browser_failure' not in value[0]['result']


@pytest.mark.parametrize('damage', ['untracked-source', 'foreign-path', 'out-of-range-line', 'linked-source'])
def test_compose_browser_drops_unverified_failure_locations_without_publishing_messages(tmp_path, monkeypatch, damage):
    _, _, path = compose_browser_failure(tmp_path)
    value = json.loads(path.read_text())
    location = value['failures'][0]['location']
    sources = {COMPOSE_BROWSER_SOURCE}
    if damage == 'untracked-source':
        sources = set()
    elif damage == 'foreign-path':
        location['file'] = '/outside/PRIVATE_ROW.ts'
    elif damage == 'out-of-range-line':
        location['line'] = 1000000
    else:
        source = diagnostics.ROOT / COMPOSE_BROWSER_SOURCE
        original = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda self: self == source or original(self))
    path.write_text(json.dumps(value))
    result = diagnostics.collect_child_failure('identity-sso', set(), root=tmp_path, sources=sources)
    assert result[0]['result']['browser_failure']['failure_location_present'] is False
    assert all(secret not in json.dumps(result) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW'))


def test_identity_browser_failure_is_hashed_in_failed_diagnostic_only(tmp_path, monkeypatch):
    compose_browser_failure(tmp_path)
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: {COMPOSE_BROWSER_SOURCE})
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('identity-sso', output, source_sha=SHA,
        ci={**CI, 'job_id': 'suite-identity-sso'}, phases=[], error=RuntimeError('SECRET_TOKEN'),
        phase='identity', before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    ref = record['evidence'][0]
    attached = output / 'evidence' / ref['path']
    assert diagnostics.hashlib.sha256(attached.read_bytes()).hexdigest() == ref['sha256']
    assert json.loads(attached.read_text())['result']['browser_failure']['failure_location_present'] is True
    assert record['status'] == 'FAIL' and record['certifies_final'] is False
    assert all(secret not in attached.read_text() for secret in ('SECRET_TOKEN', 'PRIVATE_ROW'))
    assert not list((output / 'evidence').glob('scenario-*.json'))


def async_failure_context(root, tier=100):
    context = root / '.codex-local/v070' / f'trackvance-v070-test-volume-{tier}-012345abcdef'
    context.mkdir(parents=True)
    (context / 'isolation.json').write_text(json.dumps({'project': context.name, 'main_before': 'SECRET_TOKEN'}))
    cycle = context / 'cycle-summary.json'
    cycle.write_text(json.dumps({'status': 'FAIL', 'project': context.name, 'tier_mib': tier, 'rows': 1000000,
        'volume': 'PASS', 'automation': 'PASS', 'browser': 'PASS', 'cleanup': 'PASS',
        'error_type': 'RuntimeError', 'message': 'SECRET_TOKEN', 'source_rows': ['PRIVATE_ROW']}))
    native = context / 'native-recovery/result.json'
    native.parent.mkdir()
    native.write_text(json.dumps({'status': 'FAIL', 'source_project': context.name,
        'target_project': 'trackvance-v070-test-recovery-fedcba987654', 'failed_stage': 'fresh_restore',
        'error_type': 'ComposePreflightError', 'error_code': 'LIVE_CREDENTIAL_INHERITANCE',
        'main_inventory': 'UNCHANGED', 'duration_seconds': 125, 'exit_code': 1,
        'env': {'TOKEN': 'SECRET_TOKEN'}, 'sql': 'PRIVATE_ROW', 'source_state_sha256': 'SECRET_TOKEN'}))
    return context, cycle, native


@pytest.mark.parametrize('tier', [100, 500, 1024])
def test_async_failure_keeps_exact_closed_native_result_without_payloads(tmp_path, tier):
    async_failure_context(tmp_path, tier)
    result = diagnostics.collect_child_failure(f'async-volume-{tier}', set(), root=tmp_path)
    assert result == [{'kind': 'RECOVERY_PARTIAL_SUMMARY', 'profile': 'native', 'summary_present': True,
        'result': {'status': 'FAIL', 'failed_stage': 'fresh_restore', 'error_type': 'ComposePreflightError',
            'error_code': 'LIVE_CREDENTIAL_INHERITANCE', 'main_inventory': 'UNCHANGED', 'exit_code': 1, 'duration_seconds': 125}}]
    assert all(value not in json.dumps(result) for value in ('SECRET_TOKEN', 'PRIVATE_ROW', 'source_project', 'source_state_sha256'))


def test_async_missing_evidence_is_explicit_without_inventing_phase_or_pass(tmp_path):
    _, _, native = async_failure_context(tmp_path)
    native.unlink()
    result = diagnostics.collect_child_failure('async-volume-100', set(), root=tmp_path)
    assert result == [{'kind': 'RECOVERY_PARTIAL_SUMMARY', 'profile': 'native', 'summary_present': False, 'result': {}}]


@pytest.mark.parametrize('damage', ['isolation-project',
    'native-source', 'native-target-main', 'native-target-incomplete', 'unknown-status', 'wrong-tier-context'])
def test_async_failure_rejects_foreign_malformed_or_wrong_tier_identity(tmp_path, damage):
    context, _, native = async_failure_context(tmp_path, 500 if damage == 'wrong-tier-context' else 100)
    if damage == 'wrong-tier-context':
        assert diagnostics.collect_child_failure('async-volume-100', set(), root=tmp_path) == []
        return
    path = context / 'isolation.json' if damage.startswith('isolation') else native
    raw = json.loads(path.read_text())
    key, value = {'isolation-project': ('project', 'trackvance-certification'),
        'native-source': ('source_project', 'trackvance-certification'),
        'native-target-main': ('target_project', 'trackvance-certification'),
        'native-target-incomplete': ('target_project', 'trackvance-v070-test-recovery-01234'),
        'unknown-status': ('status', 'SECRET_TOKEN')}[damage]
    raw[key] = value
    path.write_text(json.dumps(raw))
    assert diagnostics.collect_child_failure('async-volume-100', set(), root=tmp_path) == []


@pytest.mark.parametrize('unsafe', ['base-link', 'context-link', 'isolation-link', 'native-dir-link',
    'native-link', 'native-junction', 'isolation-old', 'native-old', 'oversized', 'malformed', 'baseline'])
def test_async_failure_drops_linked_stale_unbounded_or_archived_evidence(tmp_path, monkeypatch, unsafe):
    context, _, native = async_failure_context(tmp_path)
    if unsafe.endswith('-link'):
        linked = {'base-link': context.parent, 'context-link': context, 'isolation-link': context / 'isolation.json',
                  'native-dir-link': native.parent, 'native-link': native}[unsafe]
        original = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda self: self == linked or original(self))
    elif unsafe == 'native-junction':
        monkeypatch.setattr(Path, 'is_junction', lambda self: self == native.parent, raising=False)
    elif unsafe.endswith('-old'):
        old = {'isolation-old': context / 'isolation.json', 'native-old': native}[unsafe]
        assert diagnostics.collect_child_failure('async-volume-100', {old.resolve()}, root=tmp_path) == []
        return
    elif unsafe == 'oversized':
        native.write_text('x' * (diagnostics.MAX_BYTES + 1))
    elif unsafe == 'malformed':
        native.write_text('{bad-json')
    else:
        baseline = context / 'baseline'
        baseline.mkdir()
        native.replace(baseline / native.name)
        result = diagnostics.collect_child_failure('async-volume-100', set(), root=tmp_path)
        assert result[0]['summary_present'] is False and result[0]['result'] == {}
        return
    assert diagnostics.collect_child_failure('async-volume-100', set(), root=tmp_path) == []


def test_async_failure_publishes_hashed_closed_attachments_and_never_certifies(tmp_path, monkeypatch):
    async_failure_context(tmp_path)
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: set())
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('async-volume-100', output, source_sha=SHA,
        ci={**CI, 'job_id': 'suite-async-volume-100'}, phases=[], error=RuntimeError('SECRET_TOKEN'),
        phase='volume', before=set(), root=tmp_path)
    record = json.loads(path.read_text())
    assert record['status'] == 'FAIL' and record['certifies_final'] is False
    assert len(record['evidence']) == 1 and not list((output / 'evidence').glob('scenario-*.json'))
    for reference in record['evidence']:
        attachment = output / 'evidence' / reference['path']
        assert diagnostics.hashlib.sha256(attachment.read_bytes()).hexdigest() == reference['sha256']
        assert all(secret not in attachment.read_text() for secret in ('SECRET_TOKEN', 'PRIVATE_ROW'))


def catalog_nested_failure(root):
    context = root / '.codex-local/v080/trackvance-v080-test-catalog-reports-012345abcdef'
    context.mkdir(parents=True)
    (context / 'isolation.json').write_text(json.dumps({'project': context.name, 'env': 'SECRET_TOKEN'}))
    (context / 'result.json').write_text(json.dumps({'status': 'FAIL', 'failed_stage': 'recovery'}))
    child = context / 'catalog-recovery-fedcba987654'
    child.mkdir()
    (child / 'result.json').write_text(json.dumps({'status': 'FAIL', 'mode': 'both',
        'source_project': context.name, 'failed_stage': 'legacy', 'error_type': 'ComposePreflightError',
        'error_code': 'LIVE_CREDENTIAL_INHERITANCE', 'duration_seconds': 125, 'main_inventory': 'UNCHANGED',
        'native': {'status': 'PASS', 'source_version': '0.8.5', 'target_version': '0.8.5',
                   'source_state_sha256': 'SECRET_TOKEN', 'fixture': {'rows': ['PRIVATE_ROW']}},
        'legacy': {'status': 'FAIL', 'source_version': '0.7.0', 'target_version': '0.8.5'},
        'sql': 'PRIVATE_ROW', 'message': 'SECRET_TOKEN'}))
    (child / 'diagnostic.private.log').write_text(
        '  File "/app/scripts/tests/catalog_reports_recovery.py", line 91, in compose_adapter\n'
        'ci.compose_preflight.ComposePreflightError: LIVE_CREDENTIAL_INHERITANCE token=SECRET_TOKEN row=PRIVATE_ROW\n')
    http = context / 'reports-ephemeral-http.json'
    http.write_text(json.dumps({'status': 'FAIL', 'project': 'trackvance-v080-test-reports-http-abcdef012345',
        'stage': 'EXECUTE_HTTP_CASE', 'current_case': 'XLSX_RESOURCE_FAILURE', 'api_error_code': 'REPORT_RESULT_LIMIT',
        'error_type': 'AssertionError', 'main_unchanged': True, 'duration_seconds': 15,
        'cases': [{'name': 'SECRET_TOKEN', 'rows': ['PRIVATE_ROW']}], 'raw_trace': 'SECRET_TOKEN'}))
    return context, child, http


def test_catalog_nested_recovery_and_http_preserve_closed_facts_not_payloads(tmp_path):
    catalog_nested_failure(tmp_path)
    result = diagnostics.collect_child_failure('catalog-reports', set(), root=tmp_path,
        sources={'scripts/tests/catalog_reports_recovery.py'})
    nested = next(item for item in result if item['kind'] == 'CATALOG_RECOVERY_PARTIAL_SUMMARY')['result']
    assert nested == {'status': 'FAIL', 'mode': 'both', 'failed_stage': 'legacy',
        'error_type': 'ComposePreflightError', 'error_code': 'LIVE_CREDENTIAL_INHERITANCE',
        'duration_seconds': 125, 'main_inventory': 'UNCHANGED',
        'native': {'status': 'PASS', 'source_version': '0.8.5', 'target_version': '0.8.5'},
        'legacy': {'status': 'FAIL', 'source_version': '0.7.0', 'target_version': '0.8.5'}}
    facts = next(item for item in result if item['kind'] == 'CATALOG_RECOVERY_LOG_FACTS')['facts']
    assert facts['frames'] == [{'file': 'scripts/tests/catalog_reports_recovery.py', 'line': 91, 'function': 'compose_adapter'}]
    assert facts['error_codes'] == ['LIVE_CREDENTIAL_INHERITANCE']
    http = next(item for item in result if item['kind'] == 'CATALOG_HTTP_PARTIAL_SUMMARY')['result']
    assert http['stage'] == 'EXECUTE_HTTP_CASE' and http['current_case'] == 'XLSX_RESOURCE_FAILURE'
    assert http['api_error_code'] == 'REPORT_RESULT_LIMIT'
    assert all(secret not in json.dumps(result) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'source_project', 'raw_trace'))


@pytest.mark.parametrize('unsafe', ['base-link', 'context-link', 'isolation-link', 'isolation-junction',
    'recovery-dir-link', 'recovery-dir-junction', 'recovery-result-link', 'recovery-result-junction',
    'recovery-log-link', 'http-link', 'http-junction', 'isolation-old', 'recovery-old', 'http-old',
    'isolation-project', 'recovery-source', 'recovery-mode', 'http-project',
    'recovery-oversized', 'recovery-malformed', 'recovery-archived', 'recovery-name'])
def test_catalog_nested_rejects_foreign_stale_linked_or_unbounded_sources(tmp_path, monkeypatch, unsafe):
    context, child, http = catalog_nested_failure(tmp_path)
    nested = child / 'result.json'
    isolation = context / 'isolation.json'
    before = set()
    if unsafe.endswith(('-link', '-junction')):
        targets = {'base-link': context.parent, 'context-link': context, 'isolation-link': isolation,
            'isolation-junction': isolation, 'recovery-dir-link': child, 'recovery-dir-junction': child,
            'recovery-result-link': nested, 'recovery-result-junction': nested,
            'recovery-log-link': child / 'diagnostic.private.log', 'http-link': http, 'http-junction': http}
        method = 'is_junction' if unsafe.endswith('-junction') else 'is_symlink'
        original = getattr(Path, method, lambda _self: False)
        monkeypatch.setattr(Path, method, lambda self: self == targets[unsafe] or original(self), raising=False)
    elif unsafe.endswith('-old'):
        before.add({'isolation-old': isolation, 'recovery-old': nested, 'http-old': http}[unsafe].resolve())
    elif unsafe in {'isolation-project', 'recovery-source', 'recovery-mode', 'http-project'}:
        path, key, value = {'isolation-project': (isolation, 'project', 'trackvance-certification'),
            'recovery-source': (nested, 'source_project', 'trackvance-certification'),
            'recovery-mode': (nested, 'mode', 'SECRET_TOKEN'),
            'http-project': (http, 'project', 'trackvance-certification')}[unsafe]
        document = json.loads(path.read_text()); document[key] = value
        path.write_text(json.dumps(document))
    elif unsafe == 'recovery-oversized':
        nested.write_text('x' * (diagnostics.MAX_BYTES + 1))
    elif unsafe == 'recovery-malformed':
        nested.write_text('{bad-json')
    elif unsafe == 'recovery-archived':
        archived = child / 'baseline'; archived.mkdir()
        nested.replace(archived / nested.name)
    else:
        child.rename(context / 'catalog-recovery-short')
    result = diagnostics.collect_child_failure('catalog-reports', before, root=tmp_path,
        sources={'scripts/tests/catalog_reports_recovery.py'})
    if unsafe in {'base-link', 'context-link', 'isolation-link', 'isolation-junction', 'isolation-old', 'isolation-project'}:
        assert result == []
    elif unsafe.startswith('http-'):
        assert not any(item['kind'] == 'CATALOG_HTTP_PARTIAL_SUMMARY' for item in result)
    elif unsafe == 'recovery-log-link':
        assert not any(item['kind'] == 'CATALOG_RECOVERY_LOG_FACTS' for item in result)
        assert any(item['kind'] == 'CATALOG_RECOVERY_PARTIAL_SUMMARY' for item in result)
    else:
        assert not any(item['kind'].startswith('CATALOG_RECOVERY') for item in result)


def test_catalog_nested_unknown_codes_stages_cases_and_versions_are_not_published(tmp_path):
    _, child, http = catalog_nested_failure(tmp_path)
    path = child / 'result.json'; value = json.loads(path.read_text())
    value.update(failed_stage='SECRET_TOKEN', error_type='PrivateSecretError', error_code='PRIVATE_ROW')
    value['native']['source_version'] = '0.7.0'
    path.write_text(json.dumps(value))
    value = json.loads(http.read_text())
    value.update(stage='SECRET_TOKEN', current_case='PRIVATE_ROW', api_error_code='SECRET_TOKEN')
    http.write_text(json.dumps(value))
    result = diagnostics.collect_child_failure('catalog-reports', set(), root=tmp_path, sources=set())
    nested = next(item for item in result if item['kind'] == 'CATALOG_RECOVERY_PARTIAL_SUMMARY')['result']
    assert 'failed_stage' not in nested and 'error_type' not in nested and 'error_code' not in nested and 'native' not in nested
    assert all(secret not in json.dumps(result) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'PrivateSecretError'))


def test_catalog_nested_failure_publishes_hashed_diagnostics_never_scenario_pass(tmp_path, monkeypatch):
    catalog_nested_failure(tmp_path)
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: {'scripts/tests/catalog_reports_recovery.py'})
    output = tmp_path / 'ci'
    path = diagnostics.publish_failure('catalog-reports', output, source_sha=SHA, ci=CI,
        phases=[{'name': 'catalog', 'status': 'FAIL', 'exit_code': 1, 'timed_out': False}],
        error=RuntimeError('SECRET_TOKEN'), phase='catalog', before=set(), root=tmp_path)
    value = json.loads(path.read_text())
    assert value['status'] == 'FAIL' and value['certifies_final'] is False
    assert {item['kind'] for item in value['evidence']} == {'CATALOG_PARTIAL_SUMMARY',
        'CATALOG_RECOVERY_PARTIAL_SUMMARY', 'CATALOG_RECOVERY_LOG_FACTS', 'CATALOG_HTTP_PARTIAL_SUMMARY'}
    for reference in value['evidence']:
        attachment = output / 'evidence' / reference['path']
        assert diagnostics.hashlib.sha256(attachment.read_bytes()).hexdigest() == reference['sha256']
        assert all(secret not in attachment.read_text() for secret in ('SECRET_TOKEN', 'PRIVATE_ROW'))
    assert not list((output / 'evidence').glob('scenario-*.json'))


BROWSER_CI = {'run_id': '123', 'run_attempt': '2', 'job_id': 'suite-corrections-browser'}
BROWSER_SOURCE = 'frontend/tests-e2e/corrections-volume.spec.ts'


@pytest.fixture
def browser_root(tmp_path):
    # Long receipt filenames are required by the real schema. Windows unit
    # fixtures use the extended form only for their own pytest temporary root.
    return Path('\\\\?\\' + str(tmp_path)) if sys.platform == 'win32' else tmp_path


def browser_failure_context(root, rows=1000000):
    context = root / '.codex-local/v070/trackvance-v070-test-corrections-browser-012345abcdef'
    context.mkdir(parents=True)
    isolation = context / 'isolation.json'
    isolation.write_text(json.dumps({'project': context.name, 'env': 'SECRET_TOKEN'}))
    directory = context / 'xlsx-scenarios-corrections-browser-fedcba987654'; directory.mkdir()
    scenario = f'browser-{rows}-shared'
    receipt = directory / ('scenario-' + scenario + '-' + 'a' * 32 + '.json')
    receipt.write_text(json.dumps({'schema_version': 1, 'kind': 'XLSX_SCENARIO', 'group': 'corrections-browser',
        'source_sha': SHA, 'ci': BROWSER_CI, 'scenario_id': scenario, 'rows': rows, 'variant': 'shared',
        'status': 'FAIL', 'result': {'message': 'SECRET_TOKEN'}}))
    group = context / 'corrections-evidence.json'
    group.write_text(json.dumps({'schema_version': 1, 'kind': 'XLSX_GROUP', 'group': 'corrections-browser',
        'source_sha': SHA, 'ci': BROWSER_CI, 'status': 'FAIL',
        'required_scenarios': ['browser-400000-shared', 'browser-1000000-shared'],
        'scenario_results': [{'kind': 'XLSX_SCENARIO', 'scenario_id': scenario,
            'path': receipt.relative_to(context).as_posix(), 'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()}]}))
    summary = context / 'browser-summary.json'
    summary.write_text(json.dumps({'status': 'FAIL', 'expected': 0, 'unexpected': 1, 'skipped': 0, 'flaky': 0,
        'duration': 1175198.667, 'startTime': 'SECRET_TOKEN', 'url': 'https://SECRET_TOKEN', 'env': {'TOKEN': 'SECRET_TOKEN'},
        'failures': [{'test': 'PRIVATE_ROW', 'status': 'failed', 'message': 'SECRET_TOKEN',
            'location': {'file': '/app/' + BROWSER_SOURCE, 'line': 1, 'column': 5, 'stack': 'SECRET_TOKEN'}}]}))
    return context, isolation, group, receipt, summary


def collect_browser(root, before=None, *, sha=SHA, ci=BROWSER_CI):
    return diagnostics.collect_child_failure('corrections-browser', set() if before is None else before,
        root=root, sources={BROWSER_SOURCE}, source_sha=sha, ci=ci)


@pytest.mark.parametrize('rows', [400000, 1000000])
def test_corrections_browser_failed_population_keeps_only_verified_location_and_counters(browser_root, rows):
    browser_failure_context(browser_root, rows)
    result = collect_browser(browser_root)
    assert len(result) == 1 and result[0]['scenario_id'] == f'browser-{rows}-shared' and result[0]['rows'] == rows
    assert result[0]['kind'] == 'CORRECTIONS_BROWSER_PARTIAL_SUMMARY' and result[0]['summary_present'] is True
    assert result[0]['result'] == {'status': 'FAIL', 'expected': 0, 'unexpected': 1, 'skipped': 0, 'flaky': 0,
        'duration': 1175198.667, 'failure_location_present': True,
        'failures': [{'status': 'failed', 'location_present': True, 'location': {'file': BROWSER_SOURCE, 'line': 1, 'column': 5}}]}
    assert all(value not in json.dumps(result) for value in ('SECRET_TOKEN', 'PRIVATE_ROW', 'https:', '"test":', '"env":'))


@pytest.mark.parametrize('change', ['file-unknown', 'file-url', 'file-traversal', 'line-bool', 'line-zero',
    'line-overflow', 'column-bool', 'column-overflow', 'location-missing', 'failures-missing'])
def test_corrections_browser_location_missing_is_explicit_without_untrusted_text(browser_root, change):
    *_, summary = browser_failure_context(browser_root)
    value = json.loads(summary.read_text())
    location = value['failures'][0]['location']
    if change.startswith('file-'):
        location['file'] = {'file-unknown': '/SECRET_TOKEN/unknown.spec.ts', 'file-url': 'https://SECRET_TOKEN/' + BROWSER_SOURCE,
            'file-traversal': '../' + BROWSER_SOURCE}[change]
    elif change.startswith('line-'):
        location['line'] = {'line-bool': True, 'line-zero': 0, 'line-overflow': 1000000}[change]
    elif change.startswith('column-'):
        location['column'] = True if change == 'column-bool' else 1000000
    elif change == 'location-missing':
        value['failures'][0].pop('location')
    else:
        value.pop('failures')
    summary.write_text(json.dumps(value))
    result = collect_browser(browser_root)[0]['result']
    if change.startswith('column-'):
        assert result['failure_location_present'] is True and 'column' not in result['failures'][0]['location']
    else:
        assert result['failure_location_present'] is False
    assert 'SECRET_TOKEN' not in json.dumps(result)


@pytest.mark.parametrize('damage', ['isolation-project', 'group-sha', 'group-ci', 'group-pass', 'group-schema',
    'group-scenarios', 'receipt-sha', 'receipt-ci', 'receipt-pass', 'receipt-kind', 'receipt-schema',
    'receipt-rows', 'receipt-variant', 'receipt-id', 'reference-path', 'reference-hash', 'duplicate-failed'])
def test_corrections_browser_rejects_foreign_or_mismatched_group_scenario_identity(browser_root, damage):
    _, isolation, group, receipt, _ = browser_failure_context(browser_root)
    target = isolation if damage.startswith('isolation') else group if damage.startswith(('group-', 'reference-', 'duplicate')) else receipt
    value = json.loads(target.read_text())
    changes = {'isolation-project': ('project', 'trackvance-certification'), 'group-sha': ('source_sha', '2' * 40),
        'group-ci': ('ci', {**BROWSER_CI, 'run_attempt': '3'}), 'group-pass': ('status', 'PASS'), 'group-schema': ('schema_version', True),
        'group-scenarios': ('required_scenarios', []), 'receipt-sha': ('source_sha', '2' * 40),
        'receipt-ci': ('ci', {**BROWSER_CI, 'run_id': '456'}), 'receipt-pass': ('status', 'PASS'), 'receipt-kind': ('kind', 'XLSX_PROGRESS'),
        'receipt-schema': ('schema_version', True), 'receipt-rows': ('rows', 400000), 'receipt-variant': ('variant', 'inline'),
        'receipt-id': ('scenario_id', 'browser-400000-shared')}
    if damage in changes:
        key, replacement = changes[damage]; value[key] = replacement
    elif damage == 'reference-path':
        value['scenario_results'][0]['path'] = '../SECRET_TOKEN/scenario-browser-1000000-shared-' + 'a' * 32 + '.json'
    elif damage == 'reference-hash':
        value['scenario_results'][0]['sha256'] = 'b' * 64
    else:
        value['scenario_results'].append(value['scenario_results'][0])
    target.write_text(json.dumps(value))
    if target == receipt:
        envelope = json.loads(group.read_text())
        envelope['scenario_results'][0]['sha256'] = hashlib.sha256(receipt.read_bytes()).hexdigest()
        group.write_text(json.dumps(envelope))
    assert collect_browser(browser_root) == []


@pytest.mark.parametrize('unsafe', ['base-link', 'context-link', 'isolation-link', 'group-link', 'scenario-dir-link',
    'receipt-link', 'summary-link', 'scenario-dir-junction', 'summary-junction', 'isolation-old', 'group-old',
    'receipt-old', 'summary-old', 'summary-oversized', 'summary-malformed', 'summary-pass'])
def test_corrections_browser_rejects_linked_stale_or_unbounded_sources(browser_root, monkeypatch, unsafe):
    context, isolation, group, receipt, summary = browser_failure_context(browser_root)
    before = set()
    targets = {'base-link': context.parent, 'context-link': context, 'isolation-link': isolation, 'group-link': group,
        'scenario-dir-link': receipt.parent, 'receipt-link': receipt, 'summary-link': summary,
        'scenario-dir-junction': receipt.parent, 'summary-junction': summary}
    if unsafe in targets:
        method = 'is_junction' if unsafe.endswith('junction') else 'is_symlink'
        original = getattr(Path, method, lambda _self: False)
        monkeypatch.setattr(Path, method, lambda self: self == targets[unsafe] or original(self), raising=False)
    elif unsafe.endswith('-old'):
        before.add({'isolation-old': isolation, 'group-old': group, 'receipt-old': receipt, 'summary-old': summary}[unsafe].resolve())
    elif unsafe == 'summary-oversized':
        summary.write_text('x' * (diagnostics.MAX_BYTES + 1))
    elif unsafe == 'summary-malformed':
        summary.write_text('{bad-json')
    else:
        value = json.loads(summary.read_text()); value['status'] = 'PASS'; summary.write_text(json.dumps(value))
    assert collect_browser(browser_root, before) == []


def test_corrections_browser_absent_summary_and_unknown_counters_never_invent_a_location(browser_root):
    *_, summary = browser_failure_context(browser_root)
    summary.unlink()
    result = collect_browser(browser_root)
    assert result[0]['summary_present'] is False
    assert result[0]['result'] == {'status': 'FAIL', 'failure_location_present': False}
    assert collect_browser(browser_root, sha='2' * 40) == []
    assert collect_browser(browser_root, ci={**BROWSER_CI, 'job_id': 'suite-other'}) == []
    value = diagnostics.sanitize_browser_failure({'expected': True, 'unexpected': -1, 'skipped': 'SECRET_TOKEN',
        'flaky': 1000001, 'duration': float('nan'), 'failures': [{'status': 'SECRET_TOKEN', 'test': 'PRIVATE_ROW'}]}, {BROWSER_SOURCE})
    assert value == {'status': 'FAIL', 'failures': [{'location_present': False}], 'failure_location_present': False}


def test_corrections_browser_failure_attachment_is_hashed_failed_and_not_a_scenario_pass(browser_root, monkeypatch):
    browser_failure_context(browser_root)
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: {BROWSER_SOURCE})
    output = browser_root / 'ci'
    path = diagnostics.publish_failure('corrections-browser', output, source_sha=SHA, ci=BROWSER_CI,
        phases=[{'name': 'corrections', 'status': 'FAIL', 'exit_code': 1, 'timed_out': False}],
        error=RuntimeError('SECRET_TOKEN'), phase='corrections', before=set(), root=browser_root)
    result = json.loads(path.read_text())
    assert result['status'] == 'FAIL' and result['certifies_final'] is False
    assert len(result['evidence']) == 1 and result['evidence'][0]['kind'] == 'CORRECTIONS_BROWSER_PARTIAL_SUMMARY'
    reference = result['evidence'][0]; attachment = output / 'evidence' / reference['path']
    assert hashlib.sha256(attachment.read_bytes()).hexdigest() == reference['sha256']
    assert all(value not in attachment.read_text() for value in ('SECRET_TOKEN', 'PRIVATE_ROW', '"test":'))
    assert not list((output / 'evidence').glob('scenario-*.json'))
