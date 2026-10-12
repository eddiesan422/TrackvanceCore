"""Identity diagnostics preserve authenticated locations, never issued credentials."""
import ast
import hashlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from ci import failure_diagnostics as diagnostics

SHA = '1' * 40
CI = {'run_id': '123', 'run_attempt': '2', 'job_id': 'suite-identity-sso'}
SOURCE = 'scripts/tests/identity_sso_cycle.py'


@pytest.fixture
def identity_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, 'ROOT', tmp_path)
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: {SOURCE})
    monkeypatch.setattr(diagnostics.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=SHA))
    source = tmp_path / SOURCE
    source.parent.mkdir(parents=True)
    source.write_text("def fail():\n    raise RuntimeError('SECRET_PASSWORD SELECT private_row')\n")
    project = 'trackvance-v070-test-identity-012345abcdef'
    directory = tmp_path / '.codex-local/v070' / project
    directory.mkdir(parents=True)
    (directory / 'private-compose.json').write_text(json.dumps({'services': {
        name: {'environment': {'TRACKVANCE_CERTIFICATION_PROJECT': project}}
        for name in ('api', 'worker', 'delivery-worker')}}))
    (directory / 'private.empty.env').write_text('')
    environment = {'CI_SOURCE_SHA': SHA, 'GITHUB_RUN_ID': CI['run_id'], 'GITHUB_RUN_ATTEMPT': CI['run_attempt'], 'CI_JOB_ID': CI['job_id']}
    context = diagnostics.identity_diagnostic_context(directory, project, environment)
    assert context is not None
    spec = importlib.util.spec_from_file_location('synthetic_identity_failure', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.fail()
    except RuntimeError as error:
        diagnostics.write_identity_failure(error, context, 'TEMPORARY_CREDENTIALS')
    return tmp_path, directory, context, directory / 'identity-failure-safe.json'


def collect(root, before=None):
    return diagnostics.collect_child_failure('identity-sso', before or set(), root=root, sources={SOURCE}, source_sha=SHA, ci=CI)


def test_identity_internal_exception_preserves_phase_location_hash_without_browser_claim(identity_failure):
    root, directory, _context, receipt = identity_failure
    frames = [{'file': SOURCE, 'line': 2, 'function': 'fail', 'source_sha256': hashlib.sha256((root / SOURCE).read_bytes()).hexdigest()}]
    assert collect(root) == [{'kind': 'IDENTITY_FAILURE_SAFE', 'phase': 'TEMPORARY_CREDENTIALS', 'error_type': 'RuntimeError', 'frames': frames}]
    (directory / 'result.json').write_text('{"error":"SECRET_PASSWORD"}')
    (directory / 'application.log').write_text('SECRET_PASSWORD SELECT private_row')
    assert 'SECRET_PASSWORD' not in json.dumps(collect(root)) and str(root) not in receipt.read_text()
    assert 'browser' not in collect(root)[0]


@pytest.mark.parametrize('damage', ['source', 'run', 'attempt', 'profile', 'stale', 'phase', 'secret-field', 'path'])
def test_identity_source_execution_profile_freshness_and_privacy_are_fail_closed(identity_failure, damage):
    root, directory, context, receipt = identity_failure
    identity = json.loads(context.read_text()); value = json.loads(receipt.read_text()); before = set()
    if damage == 'source':identity['source_sha'] = '2' * 40
    elif damage == 'run':identity['ci']['run_id'] = '999'
    elif damage == 'attempt':identity['ci']['run_attempt'] = '3'
    elif damage == 'profile':
        path = directory / 'private-compose.json'
        profile = json.loads(path.read_text())
        profile['services']['api']['environment']['TRACKVANCE_CERTIFICATION_PROJECT'] = 'trackvance-certification'
        path.write_text(json.dumps(profile))
        identity['isolation'][path.name] = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    elif damage == 'stale':before.add(context.resolve())
    elif damage == 'phase':value['phase'] = 'SECRET_PASSWORD'
    elif damage == 'secret-field':value['locals'] = {'password': 'SECRET_PASSWORD'}
    else:value['frames'][0]['file'] = '../SECRET_PASSWORD.py'
    context.write_text(json.dumps(identity))
    value['context_sha256'] = hashlib.sha256(context.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(value))
    assert collect(root, before) == []


def test_identity_failure_phase_enum_matches_literal_runner_assignments_and_only_two_runners_are_supported():
    tree = ast.parse((diagnostics.ROOT / SOURCE).read_text(encoding='utf-8'))
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'diagnostic_phase' for target in node.targets)]
    assert assignments and all(isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) for node in assignments)
    assert {node.value.value for node in assignments} == diagnostics.IDENTITY_FAILURE_PHASES
    assert set(diagnostics.SAFE_FAILURE_RUNNERS) == {'delivery', 'identity-sso'}
    assert diagnostics._collect_runner_failure('unknown-runner', set()) == []
