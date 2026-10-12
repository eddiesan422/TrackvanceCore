"""Delivery failure locations are bound and public artifacts contain no payloads."""
import hashlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from ci import failure_diagnostics as diagnostics

SHA = '1' * 40
CI = {'run_id': '123', 'run_attempt': '2', 'job_id': 'suite-delivery'}
SOURCE = 'scripts/tests/delivery_cycle.py'


@pytest.fixture
def delivery_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, 'ROOT', tmp_path)
    monkeypatch.setattr(diagnostics, 'trusted_sources', lambda: {SOURCE})
    monkeypatch.setattr(diagnostics.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=SHA))
    source = tmp_path / SOURCE
    source.parent.mkdir(parents=True)
    source.write_text("def fail():\n    raise RuntimeError('SECRET_TOKEN SELECT private_row')\n")
    project = 'trackvance-delivery-e2e-012345abcdef'
    directory = tmp_path / '.codex-local/delivery-e2e' / project
    directory.mkdir(parents=True)
    profile = directory / 'private-compose.json'
    profile.write_text(json.dumps({'services': {name: {'environment': {'TRACKVANCE_CERTIFICATION_PROJECT': project}}
                                               for name in ('api', 'worker', 'delivery-worker')}}))
    (directory / 'private.empty.env').write_text('')
    environment = {'CI_SOURCE_SHA': SHA, 'GITHUB_RUN_ID': CI['run_id'], 'GITHUB_RUN_ATTEMPT': CI['run_attempt'], 'CI_JOB_ID': CI['job_id']}
    context = diagnostics.delivery_diagnostic_context(directory, project, environment)
    assert context is not None
    spec = importlib.util.spec_from_file_location('synthetic_delivery_failure', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.fail()
    except RuntimeError as error:
        diagnostics.write_delivery_failure(error, context)
    return tmp_path, directory, context, directory / 'delivery-failure-safe.json'


def collect(root, before=None):
    return diagnostics.collect_child_failure('delivery', before or set(), root=root, sources={SOURCE}, source_sha=SHA, ci=CI)


def test_delivery_exception_retains_only_verified_source_location_and_hash(delivery_failure):
    root, directory, _context, receipt = delivery_failure
    value = json.loads(receipt.read_text())
    frames = [{'file': SOURCE, 'line': 2, 'function': 'fail', 'source_sha256': hashlib.sha256((root / SOURCE).read_bytes()).hexdigest()}]
    assert value['error_type'] == 'RuntimeError' and value['frames'] == frames
    assert collect(root) == [{'kind': 'DELIVERY_FAILURE_SAFE', 'error_type': 'RuntimeError', 'frames': frames}]
    # Raw child files must never be read or copied by this collector.
    (directory / 'result.json').write_text('{"error":"SECRET_TOKEN"}')
    (directory / 'application.log').write_text('SECRET_TOKEN SELECT private_row')
    assert 'SECRET_TOKEN' not in json.dumps(collect(root)) and str(root) not in receipt.read_text()


@pytest.mark.parametrize('damage', ['secret-field', 'forged-path', 'source-hash', 'stale-receipt', 'context-sha', 'wrong-ci', 'non-dict-service', 'non-dict-environment'])
def test_delivery_forged_secret_location_stale_or_malformed_context_is_not_published(delivery_failure, damage):
    root, directory, context, receipt = delivery_failure
    value = json.loads(receipt.read_text()); identity = json.loads(context.read_text())
    before = set()
    if damage == 'secret-field':value['message'] = 'SECRET_TOKEN'
    elif damage == 'forged-path':value['frames'][0]['file'] = '../SECRET_TOKEN.py'
    elif damage == 'source-hash':value['frames'][0]['source_sha256'] = '0' * 64
    elif damage == 'stale-receipt':before.add(receipt.resolve())
    elif damage == 'context-sha':identity['source_sha'] = '2' * 40
    elif damage == 'wrong-ci':identity['ci']['run_attempt'] = '3'
    else:
        profile_path = directory / 'private-compose.json'
        profile = json.loads(profile_path.read_text())
        if damage == 'non-dict-service':profile['services']['api'] = []
        else:profile['services']['api']['environment'] = 'SECRET_TOKEN'
        profile_path.write_text(json.dumps(profile))
        identity['isolation']['private-compose.json'] = {'sha256': hashlib.sha256(profile_path.read_bytes()).hexdigest(), 'bytes': profile_path.stat().st_size}
    context.write_text(json.dumps(identity))
    value['context_sha256'] = hashlib.sha256(context.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(value))
    assert collect(root, before) == []


def test_delivery_browser_summary_reuses_existing_message_free_sanitizer(delivery_failure):
    root, directory, _context, _receipt = delivery_failure
    summary = directory / 'browser-summary.json'
    summary.write_text(json.dumps({'unexpected': 1, 'expected': 0, 'message': 'SECRET_TOKEN', 'failures': [
        {'test': 'SECRET_TOKEN', 'status': 'failed', 'error': 'SELECT private_row'}]}))
    result = collect(root)
    assert result[0]['browser'] == {'status': 'FAIL', 'expected': 0, 'unexpected': 1, 'failures': [
        {'location_present': False, 'status': 'failed'}], 'failure_location_present': False}
    assert 'SECRET_TOKEN' not in json.dumps(result) and 'SELECT' not in json.dumps(result)
